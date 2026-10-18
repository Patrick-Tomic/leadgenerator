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


def _address(tags: dict) -> tuple[str | None, str | None]:
    if tags.get("addr:full"):
        return tags["addr:full"], tags.get("addr:city")
    street = " ".join(x for x in (tags.get("addr:housenumber"), tags.get("addr:street")) if x)
    city = tags.get("addr:city")
    parts = [p for p in (street, city, tags.get("addr:state"), tags.get("addr:postcode")) if p]
    return (", ".join(parts) or None), city


def _website(tags: dict) -> str | None:
    for key in ("website", "contact:website", "url"):
        if tags.get(key):
            return tags[key].split(";")[0].strip()
    return None


def element_to_business(el: dict, industry_key: str) -> dict | None:
    tags = el.get("tags", {})
    name = tags.get("name")
    if not name or any(t in tags for t in CHAIN_TAGS):
        return None
    if tags.get("disused") or tags.get("abandoned") or "disused:shop" in tags:
        return None
    lat = el.get("lat") or el.get("center", {}).get("lat")
    lon = el.get("lon") or el.get("center", {}).get("lon")
    address, city = _address(tags)
    return {
        "source": "osm",
        "source_id": f"{el['type']}/{el['id']}",
        "name": name,
        "industry": industry_key,
        "address": address,
        "city": city,
        "lat": lat,
        "lon": lon,
        "phone": (tags.get("phone") or tags.get("contact:phone") or "").split(";")[0].strip() or None,
        "email": tags.get("email") or tags.get("contact:email"),
        "website": _website(tags),
        "raw": {"osm_url": f"https://www.openstreetmap.org/{el['type']}/{el['id']}", "tags": tags},
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
