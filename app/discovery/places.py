"""Optional Google Places API (New) website finder. Disabled unless GOOGLE_PLACES_API_KEY
is set; never called by default.

- Text Search (POST places:searchText) with an explicit field mask, so only the fields
  we need are requested. websiteUri and phone numbers are Enterprise-tier fields, which
  set the billing SKU for the call.
- Every call is counted per month in api_usage; GOOGLE_PLACES_MONTHLY_LIMIT stops calls
  before the free allowance is exceeded.
- Storage: Google allows place IDs to be stored indefinitely and restricts caching of
  other Places content. This finder hands back Places data for in-memory matching only;
  app/websites.py persists the place ID, plus a website URL only after our own fetch of
  that website independently identifies the business (phone/name/address on its page).
- A place counts as this business only on a phone match or a name match at the same
  location/city, never on a similar name alone.
"""
from datetime import datetime, timezone

import httpx

from .. import config, db
from ..dedup import distance_m, normalize_name, normalize_phone
from .base import SourceError, WebsiteFinder

ENDPOINT = "https://places.googleapis.com/v1/places:searchText"
FIELD_MASK = ",".join(["places.id", "places.displayName", "places.formattedAddress", "places.location",
                       "places.nationalPhoneNumber", "places.internationalPhoneNumber", "places.websiteUri",
                       "places.businessStatus"])


def match_place(place: dict, profile: dict, lat: float | None, lon: float | None) -> tuple[str | None, list[str]]:
    """How confidently a Places result is this business, with reasons."""
    ev = []
    phones = {normalize_phone(place.get("nationalPhoneNumber")), normalize_phone(place.get("internationalPhoneNumber"))} - {None}
    if phones & set(profile["phones"]):
        ev.append("Google Places record has the same phone number")
    pname = normalize_name((place.get("displayName") or {}).get("text"))
    names = {normalize_name(n) for n in profile["names"]} - {None}
    # The business's full name must equal or appear within the place name (not the reverse:
    # a place called "Plumbing" must not match "Ace Plumbing").
    name_match = bool(pname) and any(n == pname or f" {n} " in f" {pname} " for n in names)
    if name_match:
        ev.append(f'Google Places name "{(place.get("displayName") or {}).get("text")}" matches')
    near = None
    loc = place.get("location") or {}
    if lat is not None and loc.get("latitude") is not None:
        near = distance_m(lat, lon, loc["latitude"], loc["longitude"])
        if near <= 300:
            ev.append(f"Google Places location is {near:.0f} m from the listed location")
    same_city = any(c.lower() in (place.get("formattedAddress") or "").lower() for c in profile["cities"])
    if same_city:
        ev.append("same city in Google Places address")
    if "same phone" in " ".join(ev):
        return "high", ev
    if name_match and near is not None and near <= 300:
        return "high", ev
    if name_match and same_city:
        return "medium", ev
    return None, ev


class GooglePlacesFinder(WebsiteFinder):
    name = "google_places"

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None, lat: float | None = None, lon: float | None = None):
        self.transport, self.lat, self.lon = transport, lat, lon

    async def find_candidates(self, profile: dict) -> dict:
        period = datetime.now(timezone.utc).strftime("%Y-%m")
        if db.api_calls(self.name, period) >= config.GOOGLE_PLACES_MONTHLY_LIMIT:
            raise SourceError(f"Google Places monthly limit reached ({config.GOOGLE_PLACES_MONTHLY_LIMIT} calls); not calling")
        query = " ".join(x for x in (profile["name"], profile.get("city"), "FL") if x)
        body: dict = {"textQuery": query, "pageSize": 5, "regionCode": "US"}
        lat, lon = profile.get("lat", self.lat), profile.get("lon", self.lon)
        if lat is not None and lon is not None:
            body["locationBias"] = {"circle": {"center": {"latitude": lat, "longitude": lon}, "radius": 3000.0}}
        try:
            async with httpx.AsyncClient(timeout=20, transport=self.transport) as client:
                db.count_api_call(self.name, period)  # count before sending: a sent request may be billed
                resp = await client.post(ENDPOINT, json=body, headers={
                    "X-Goog-Api-Key": config.GOOGLE_PLACES_API_KEY, "X-Goog-FieldMask": FIELD_MASK})
        except httpx.HTTPError as e:
            raise SourceError(f"Google Places request failed: {type(e).__name__}") from e
        if resp.status_code != 200:
            try:
                err = resp.json().get("error", {})
            except ValueError:
                err = {}
            hint = {"PERMISSION_DENIED": " (check billing is enabled and the key allows Places API (New))",
                    "RESOURCE_EXHAUSTED": " (quota exceeded)"}.get(err.get("status"), "")
            raise SourceError(f"Google Places HTTP {resp.status_code} {err.get('status', '')}: "
                              f"{(err.get('message') or '')[:160]}{hint}")
        candidates = []
        places = resp.json().get("places", [])
        for place in places:
            level, ev = match_place(place, profile, lat, lon)
            if not level or not place.get("websiteUri"):
                continue
            candidates.append({"url": place["websiteUri"], "source": self.name, "source_record_id": place.get("id"),
                               "evidence": ev, "base_identity": level, "transient": True})
        return {"candidates": candidates, "queries": [query], "checked": [p.get("id") for p in places], "directories": []}
