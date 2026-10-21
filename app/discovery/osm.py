"""OpenStreetMap discovery via the Overpass API.

Data is (c) OpenStreetMap contributors, licensed ODbL: commercial use is allowed
with attribution (shown in the app footer and README). Usage policy for the public
instance: keep queries modest, one at a time; it allows roughly 10,000 queries/day
and ~1 GB/day per user, and answers HTTP 429 when its per-IP slots are busy. We query
sequentially, wait OVERPASS_MIN_INTERVAL between queries, back off on 429/504, and
re-scan incrementally with Overpass's `newer:` filter.

Overpass has no pagination: one query returns every match inside the area, so large
areas are split into smaller named areas (config.AREAS) instead.
"""
import asyncio
import logging
import re
import time

import httpx

from .. import config
from .base import DiscoveryAdapter, SourceError

log = logging.getLogger(__name__)

# Tags that mean the business is a national/regional chain or franchise
# location: they rarely buy local web services, so skip them.
CHAIN_TAGS = ("brand", "brand:wikidata", "operator:wikidata")

_query_lock = asyncio.Lock()
_last_query = 0.0


def build_query(selectors: list[str], bbox: tuple, since: str | None = None) -> str:
    b = ",".join(str(x) for x in bbox)
    newer = f'(newer:"{since}")' if since else ""
    parts = "".join(f"nwr{sel}{newer}({b});" for sel in selectors)
    return f"[out:json][timeout:90];({parts});out center tags;"


_CITY_IN_ADDRESS = re.compile(r",\s*([A-Za-z .'-]+?),\s*(?:FL|Florida)\b(?:\s+(\d{5}))?", re.I)
WEBSITE_KEYS = ("website", "contact:website", "url", "contact:url")
PHONE_KEYS = ("phone", "contact:phone", "mobile", "contact:mobile")
ALT_NAME_KEYS = ("alt_name", "short_name", "official_name", "loc_name", "old_name", "brand")
SOCIAL_KEYS = {"facebook": ("contact:facebook", "facebook"), "instagram": ("contact:instagram", "instagram"),
               "twitter": ("contact:twitter", "twitter"), "yelp": ("contact:yelp",)}


def _first(tags: dict, keys: tuple) -> str | None:
    for key in keys:
        if tags.get(key):
            return tags[key].split(";")[0].strip()
    return None


def _address(tags: dict) -> tuple[str | None, str | None, str | None]:
    """(address, city, postcode). Some mappers put the whole address in addr:full or
    addr:street ("56th Ct N #604, Clearwater, FL 33760"), so city/ZIP are parsed from it."""
    city, postcode = tags.get("addr:city"), tags.get("addr:postcode")
    if tags.get("addr:full"):
        address = tags["addr:full"]
    else:
        street = " ".join(x for x in (tags.get("addr:housenumber"), tags.get("addr:street")) if x)
        address = ", ".join(p for p in (street, city, tags.get("addr:state"), postcode) if p) or None
    if address and not city:
        m = _CITY_IN_ADDRESS.search(address)
        if m:
            city, postcode = m.group(1).strip(), postcode or m.group(2)
    return address, city, postcode


def _website(tags: dict) -> str | None:
    return _first(tags, WEBSITE_KEYS)


def contributed_fields(tags: dict) -> dict:
    """Every business field this OSM record provides (for provenance and lead details)."""
    address, city, postcode = _address(tags)
    phones = sorted({p.strip() for k in PHONE_KEYS for p in (tags.get(k) or "").split(";") if p.strip()})
    out = {
        "name": tags.get("name"),
        "alt_names": [tags[k] for k in ALT_NAME_KEYS if tags.get(k)],
        "address": address, "city": city, "postcode": postcode,
        "phone": phones[0] if phones else None, "phones": phones,
        "email": _first(tags, ("email", "contact:email")),
        "website": _website(tags),
        "social": {name: _first(tags, keys) for name, keys in SOCIAL_KEYS.items() if _first(tags, keys)},
        "opening_hours": tags.get("opening_hours"),
        "description": tags.get("description") or tags.get("description:en"),
        "operator": tags.get("operator"),
        "start_date": tags.get("start_date"),
    }
    return {k: v for k, v in out.items() if v}


def element_to_business(el: dict, industry_key: str) -> dict | None:
    tags = el.get("tags", {})
    name = tags.get("name")
    if not name or any(t in tags for t in CHAIN_TAGS):
        return None
    if tags.get("disused") or tags.get("abandoned") or "disused:shop" in tags:
        return None
    lat = el.get("lat") or el.get("center", {}).get("lat")
    lon = el.get("lon") or el.get("center", {}).get("lon")
    fields = contributed_fields(tags)
    osm_url = f"https://www.openstreetmap.org/{el['type']}/{el['id']}"
    return {
        "source": "osm",
        "source_id": f"{el['type']}/{el['id']}",
        "source_url": osm_url,
        "name": name,
        "industry": industry_key,
        "address": fields.get("address"),
        "city": fields.get("city"),
        "lat": lat,
        "lon": lon,
        "phone": fields.get("phone"),
        "email": fields.get("email"),
        "website": fields.get("website"),
        "fields": fields,
        "raw": {"osm_url": osm_url, "tags": tags},
    }


class OverpassAdapter(DiscoveryAdapter):
    name = "osm"
    supports_incremental = True

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None, retry_delay: float = 15.0,
                 min_interval: float | None = None) -> None:
        self.transport = transport  # tests inject httpx.MockTransport
        self.retry_delay = retry_delay
        self.min_interval = config.OVERPASS_MIN_INTERVAL if min_interval is None else min_interval

    async def _post(self, query: str) -> dict:
        global _last_query
        async with _query_lock:
            wait = _last_query + self.min_interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                async with httpx.AsyncClient(headers={"User-Agent": config.USER_AGENT}, timeout=120,
                                             transport=self.transport) as client:
                    for attempt in range(3):
                        resp = await client.post(config.OVERPASS_URL, data={"data": query})
                        if resp.status_code in (429, 504):
                            await asyncio.sleep(self.retry_delay * (attempt + 1))
                            continue
                        break
            except httpx.HTTPError as e:
                raise SourceError(f"Overpass request failed: {type(e).__name__}: {e}") from e
            finally:
                _last_query = time.monotonic()
        if resp.status_code != 200:
            raise SourceError(f"Overpass returned HTTP {resp.status_code}" + (" (rate limited)" if resp.status_code == 429 else ""))
        try:
            data = resp.json()
        except ValueError as e:
            raise SourceError("Overpass returned a non-JSON response") from e
        # Timeouts and memory limits come back as HTTP 200 with a "remark" and partial data.
        remark = data.get("remark") or ""
        if "error" in remark.lower():
            raise SourceError(f"Overpass: {remark[:200]}")
        return data

    async def discover(self, industry_key: str, area_key: str, since: str | None = None) -> list[dict]:
        query = build_query(config.INDUSTRIES[industry_key]["selectors"], config.AREAS[area_key]["bbox"], since)
        data = await self._post(query)
        out = [b for el in data.get("elements", []) if (b := element_to_business(el, industry_key))]
        log.info("osm %s/%s%s: %d businesses", area_key, industry_key, f" since {since}" if since else "", len(out))
        return out
