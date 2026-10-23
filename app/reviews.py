"""Google review counts via the Google Places API (New). Optional: off unless
GOOGLE_PLACES_API_KEY is set, and only used for leads about to be shown to you, so normal
use stays inside Google's free monthly allowance (calls are counted and capped together
with the Places website finder: GOOGLE_PLACES_MONTHLY_LIMIT).

Storage: Google allows place IDs to be stored indefinitely and restricts caching other
Places content. So the place ID is saved (as a provenance record), while rating and
review counts live only in this process's memory for CACHE_HOURS and are re-fetched
after a restart. Displayed results are labeled as data from Google.
"""
import time
from datetime import datetime, timezone

import httpx

from . import config, db
from .discovery.base import SourceError
from .discovery.places import ENDPOINT, match_place
from .websites import business_profile

SOURCE = "google_places"
DETAILS_URL = "https://places.googleapis.com/v1/places/{}"
SEARCH_MASK = ",".join(["places.id", "places.displayName", "places.formattedAddress", "places.location",
                        "places.nationalPhoneNumber", "places.internationalPhoneNumber", "places.rating",
                        "places.userRatingCount"])
DETAILS_MASK = "id,rating,userRatingCount"
CACHE_HOURS = 24
_cache: dict[int, tuple[float, dict]] = {}


def enabled() -> bool:
    return bool(config.GOOGLE_PLACES_API_KEY)


def cached(lead_id: int) -> dict | None:
    hit = _cache.get(lead_id)
    return hit[1] if hit and time.time() - hit[0] < CACHE_HOURS * 3600 else None


def over_limit(limit: int | None) -> set[int]:
    """Lead ids whose (cached) Google review count is above the limit."""
    if limit is None:
        return set()
    return {lid for lid in list(_cache) if (r := cached(lid)) and r["found"] and r["count"] is not None and r["count"] > limit}


def _stored_place_id(lead_id: int) -> str | None:
    r = db.conn().execute("SELECT source_id FROM lead_sources WHERE lead_id=? AND source=?", (lead_id, SOURCE)).fetchone()
    return r[0] if r else None


def _save_place_id(lead_id: int, industry: str, place_id: str, evidence: list[str]) -> None:
    ts = db.now()
    db._write("""INSERT INTO lead_sources (lead_id, source, source_id, industry, area, raw, first_seen_at, last_seen_at, fields,
                 match_confidence, match_explanation, metadata, last_checked_at)
                 VALUES (?, ?, ?, ?, NULL, '{}', ?, ?, '{}', 'high', ?, ?, ?)
                 ON CONFLICT (source, source_id) DO UPDATE SET last_checked_at=excluded.last_checked_at""",
              (lead_id, SOURCE, place_id, industry, ts, ts, "; ".join(evidence),
               '{"note": "place ID only; Google content is not stored"}', ts))


async def check(lead: dict, transport: httpx.AsyncBaseTransport | None = None) -> dict | None:
    """Google rating and review count for a lead, or None if disabled or not found.
    Raises SourceError on API problems (callers treat that as 'not checked')."""
    if not enabled():
        return None
    if (hit := cached(lead["id"])) is not None:
        return hit
    period = datetime.now(timezone.utc).strftime("%Y-%m")
    if db.api_calls(SOURCE, period) >= config.GOOGLE_PLACES_MONTHLY_LIMIT:
        raise SourceError(f"Google Places monthly limit reached ({config.GOOGLE_PLACES_MONTHLY_LIMIT}); reviews not checked")
    headers = {"X-Goog-Api-Key": config.GOOGLE_PLACES_API_KEY}
    place_id = _stored_place_id(lead["id"])
    try:
        async with httpx.AsyncClient(timeout=20, transport=transport) as client:
            db.count_api_call(SOURCE, period)
            if place_id:
                resp = await client.get(DETAILS_URL.format(place_id), headers={**headers, "X-Goog-FieldMask": DETAILS_MASK})
                place, evidence = (resp.json() if resp.status_code == 200 else None), ["stored place ID"]
            else:
                profile = business_profile(lead)
                query = " ".join(x for x in (lead["name"], lead.get("city"), "FL" if not lead.get("city") else "") if x)
                body = {"textQuery": query, "pageSize": 5, "regionCode": "US"}
                if lead.get("lat") is not None:
                    body["locationBias"] = {"circle": {"center": {"latitude": lead["lat"], "longitude": lead["lon"]}, "radius": 3000.0}}
                resp = await client.post(ENDPOINT, json=body, headers={**headers, "X-Goog-FieldMask": SEARCH_MASK})
                place, evidence = None, []
                if resp.status_code == 200:
                    for p in resp.json().get("places", []):
                        level, ev = match_place(p, profile, lead.get("lat"), lead.get("lon"))
                        if level == "high":   # only a confident match: never another business's reviews
                            place, evidence = p, ev
                            break
    except httpx.HTTPError as e:
        raise SourceError(f"Google Places request failed: {type(e).__name__}") from e
    if resp.status_code != 200:
        err = (resp.json().get("error", {}) if resp.headers.get("content-type", "").startswith("application/json") else {})
        raise SourceError(f"Google Places HTTP {resp.status_code} {err.get('status', '')}: {(err.get('message') or '')[:120]}")
    if not place:
        result = {"found": False, "count": None, "rating": None, "source": "Google",
                  "note": "No confidently matching Google listing (matched only on phone or name at the same location)."}
    else:
        if not place_id and place.get("id"):
            _save_place_id(lead["id"], lead["industry"], place["id"], evidence)
        result = {"found": True, "count": place.get("userRatingCount", 0), "rating": place.get("rating"), "source": "Google",
                  "place_id": place.get("id") or place_id,
                  "maps_url": f"https://www.google.com/maps/place/?q=place_id:{place.get('id') or place_id}"}
    result["checked_at"] = db.now()
    # The outcome (listing found or not) is ours to keep; the counts stay in memory only.
    db._write("UPDATE leads SET google_listing=?, google_checked_at=? WHERE id=?",
              (int(result["found"]), result["checked_at"], lead["id"]))
    _cache[lead["id"]] = (time.time(), result)
    return result
