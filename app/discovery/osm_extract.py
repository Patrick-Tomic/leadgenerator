"""OpenStreetMap discovery from Geofabrik state extracts (.osm.pbf), read locally.

Why: the public Overpass server says it's "intended for browser-based and human-operated
tools"; statewide, all-category discovery is high-volume automated use. Geofabrik
publishes free daily extracts of the same OSM data (ODbL, attribution required, as
before) for exactly this. One download per state replaces thousands of queries.

How (built for small machines, e.g. 3 GB RAM):
  pass 1  C++-side filter to objects with a business key (shop/amenity/office/...) and a
          name; Python classifies each into a category using the same selectors as the
          Overpass adapter. Nodes carry coordinates; for ways we note one node id.
  pass 2  read only those node ids to get the ways' coordinates.
No full node-location index is built, so memory stays at roughly the size of the matches.

Chains: brand-tagged objects are skipped as before, and so is any name that appears at
CHAIN_MIN_LOCATIONS or more places in the state (many chain locations lack brand tags).
Not covered: businesses mapped only as relations, or tagged with nothing but a name.
"""
import logging
import os
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import httpx
import osmium
import osmium.filter

from .. import config
from ..dedup import normalize_name
from .base import DiscoveryAdapter, SourceError
from .osm import CHAIN_TAGS, element_to_business

log = logging.getLogger(__name__)
BUSINESS_KEYS = ("shop", "amenity", "office", "craft", "healthcare", "tourism", "leisure")
_SEL = re.compile(r'\["([^"]+)"(?:(=|~|!~)"([^"]*)"(,i)?)?\]')


def compile_selector(sel: str):
    """Turn an Overpass filter like ["shop"~"^(a|b)$"]["amenity"!~"."] into a tag predicate."""
    conds = []
    for key, op, value, ci in _SEL.findall(sel):
        flags = re.I if ci else 0
        if not op:
            conds.append(lambda t, k=key: k in t)
        elif op == "=":
            conds.append(lambda t, k=key, v=value: t.get(k) == v)
        elif op == "~":
            rx = re.compile(value, flags)
            conds.append(lambda t, k=key, rx=rx: k in t and bool(rx.search(t[k])))
        else:  # !~
            rx = re.compile(value, flags)
            conds.append(lambda t, k=key, rx=rx: not (k in t and rx.search(t[k])))
    return lambda tags: all(c(tags) for c in conds)


MATCHERS = [(key, [compile_selector(s) for s in spec["selectors"]]) for key, spec in config.INDUSTRIES.items()]


def classify(tags: dict) -> str | None:
    """Most specific category for a tagged object (catalog order: trades first, catch-alls last)."""
    for key, preds in MATCHERS:
        if any(p(tags) for p in preds):
            return key
    return None


def extract_path(area: str) -> Path:
    return config.OSM_EXTRACT_DIR / f"{config.AREAS[area]['extract'].rsplit('/', 1)[-1]}-latest.osm.pbf"


def download(area: str, transport: httpx.BaseTransport | None = None) -> dict:
    """Fetch a state's extract if Geofabrik has a newer one. Streams to a temp file first."""
    url = f"{config.GEOFABRIK_BASE}/{config.AREAS[area]['extract']}-latest.osm.pbf"
    dest = extract_path(area)
    dest.parent.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": config.USER_AGENT}
    if dest.exists():
        headers["If-Modified-Since"] = datetime.fromtimestamp(dest.stat().st_mtime, timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
    try:
        with httpx.Client(timeout=600, follow_redirects=True, transport=transport) as client, \
                client.stream("GET", url, headers=headers) as r:
            if r.status_code == 304:
                return {"status": "not modified", "file": str(dest)}
            if r.status_code != 200:
                raise SourceError(f"Geofabrik returned HTTP {r.status_code} for {url}")
            tmp = dest.with_suffix(".part")
            with open(tmp, "wb") as fh:
                for chunk in r.iter_bytes(1 << 20):
                    fh.write(chunk)
            tmp.replace(dest)
            if r.headers.get("last-modified"):
                ts = datetime.strptime(r.headers["last-modified"], "%a, %d %b %Y %H:%M:%S GMT").replace(tzinfo=timezone.utc).timestamp()
                os.utime(dest, (ts, ts))
    except httpx.HTTPError as e:
        raise SourceError(f"Geofabrik download failed: {type(e).__name__}: {e}") from e
    return {"status": "downloaded", "file": str(dest), "bytes": dest.stat().st_size}


def read_businesses(path: Path, chain_min: int | None = None):
    """Yield business dicts from an extract file (see module docstring for the method)."""
    chain_min = config.CHAIN_MIN_LOCATIONS if chain_min is None else chain_min
    found, way_nodes, name_counts = [], {}, Counter()
    fp = (osmium.FileProcessor(str(path), osmium.osm.NODE | osmium.osm.WAY)
          .with_filter(osmium.filter.KeyFilter(*BUSINESS_KEYS))
          .with_filter(osmium.filter.KeyFilter("name")))
    for obj in fp:
        tags = dict(obj.tags)
        if any(t in tags for t in CHAIN_TAGS) or tags.get("disused") or tags.get("abandoned"):
            continue
        industry = classify(tags)
        if not industry:
            continue
        if obj.is_node():
            if not obj.location.valid():
                continue
            found.append(("node", obj.id, obj.location.lat, obj.location.lon, tags, industry))
        else:
            if not len(obj.nodes):
                continue
            ref = obj.nodes[len(obj.nodes) // 2].ref  # a node on the outline: close enough for proximity checks
            way_nodes[obj.id] = ref
            found.append(("way", obj.id, None, None, tags, industry))
        name_counts[normalize_name(tags["name"])] += 1
    coords = {}
    if way_nodes:
        # Not osmium.filter.IdFilter: with tens of thousands of real (10-digit) node ids it
        # allocated ~1.5 GB, too much for small machines. A Python membership test over the
        # node stream is slower (~8 min for Florida) but stays under ~60 MB. Nodes are
        # sorted by id, so stop once past the largest id needed.
        wanted = set(way_nodes.values())
        last = max(wanted)
        for n in osmium.FileProcessor(str(path), osmium.osm.NODE):
            if n.id in wanted and n.location.valid():
                coords[n.id] = (n.location.lat, n.location.lon)
            elif n.id > last:
                break
    chains = {n for n, k in name_counts.items() if k >= chain_min}
    log.info("%s: %d matches, %d chain names (%d+ locations) skipped", path.name, len(found), len(chains), chain_min)
    for kind, oid, lat, lon, tags, industry in found:
        if normalize_name(tags["name"]) in chains:
            continue
        if kind == "way":
            lat, lon = coords.get(way_nodes[oid], (None, None))
            if lat is None:
                continue
        el = {"type": kind, "id": oid, "lat": lat, "lon": lon, "tags": tags}
        b = element_to_business(el, industry)
        if b:
            yield b


class OsmExtractAdapter(DiscoveryAdapter):
    """Discovery adapter for areas with a Geofabrik extract: one scan covers every category."""
    name = "osm_extract"
    supports_incremental = False

    async def discover(self, industry_key: str, area_key: str, since: str | None = None):
        import asyncio
        import json
        import sys
        import tempfile
        info = await asyncio.to_thread(download, area_key)
        log.info("extract %s: %s", area_key, info["status"])
        # Reading takes minutes of CPU; a separate process keeps the web app responsive.
        with tempfile.NamedTemporaryFile(suffix=".jsonl", dir=config.OSM_EXTRACT_DIR, delete=False) as tmp:
            out = Path(tmp.name)
        try:
            proc = await asyncio.create_subprocess_exec(sys.executable, "-m", "app.discovery.osm_extract", area_key, str(out),
                                                        cwd=str(config.BASE_DIR))
            if await proc.wait() != 0:
                raise SourceError(f"reading the {area_key} extract failed (exit {proc.returncode})")
            with open(out, encoding="utf-8") as fh:
                return [json.loads(line) for line in fh]
        finally:
            out.unlink(missing_ok=True)


if __name__ == "__main__":  # python -m app.discovery.osm_extract <area> <out.jsonl>
    import json
    import sys
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    with open(sys.argv[2], "w", encoding="utf-8") as fh:
        for b in read_businesses(extract_path(sys.argv[1])):
            fh.write(json.dumps(b) + "\n")
