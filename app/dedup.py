"""Normalization used to recognize the same business across sources and scans.

Match order (first hit wins), most to least reliable:
  1. same source record (e.g. OSM node/123) seen before
  2. same website domain (ignoring social/directory hosts)
  3. same 10-digit phone number
  4. same normalized name within MAX_SAME_NAME_METERS (or same city when coordinates are missing)
Matching is deliberately conservative: two distinct businesses merged by mistake is
worse than an occasional duplicate a person can mark "not a fit".
"""
import math
import re
from urllib.parse import urlparse

MAX_SAME_NAME_METERS = 300

# Hosts that aren't the business's own domain, so they can't identify it.
SHARED_HOSTS = {
    "facebook.com", "fb.com", "instagram.com", "yelp.com", "nextdoor.com", "linktr.ee", "google.com",
    "business.site", "g.page", "goo.gl", "sites.google.com", "wixsite.com", "squarespace.com", "weebly.com",
    "godaddysites.com", "yellowpages.com", "bbb.org", "angi.com", "homeadvisor.com", "thumbtack.com",
    "linkedin.com", "x.com", "twitter.com", "youtube.com", "tiktok.com", "square.site", "carrd.co",
}
# Two-label public suffixes we might see; everything else uses the last two labels.
_MULTI_SUFFIXES = {"co.uk", "com.au", "co.nz", "com.br", "com.mx"}
_LEGAL = r"\b(llc|l\.l\.c|inc|incorporated|co|corp|corporation|company|ltd|pllc|pa|lp|llp|the)\b"


def normalize_name(name: str | None) -> str | None:
    if not name:
        return None
    s = name.lower().replace("&", " and ").replace("'", "").replace("’", "")
    s = re.sub(_LEGAL, " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s or None


def normalize_domain(url: str | None) -> str | None:
    """Registrable domain of a business's own site, or None for shared/social hosts."""
    if not url:
        return None
    url = url.strip()
    if not re.match(r"^[a-z]+://", url, re.I):
        url = "http://" + url
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    if not host or "." not in host:
        return None
    labels = host.split(".")
    n = 3 if ".".join(labels[-2:]) in _MULTI_SUFFIXES else 2
    domain = ".".join(labels[-n:])
    if domain in SHARED_HOSTS or any(host == h or host.endswith("." + h) for h in SHARED_HOSTS):
        return None
    return domain


def normalize_phone(phone: str | None) -> str | None:
    if not phone:
        return None
    digits = re.sub(r"\D", "", phone.split(";")[0])
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits if len(digits) == 10 else None


def distance_m(lat1, lon1, lat2, lon2) -> float:
    r = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def keys(b: dict) -> dict:
    return {
        "norm_name": normalize_name(b.get("name")),
        "norm_domain": normalize_domain(b.get("website")),
        "norm_phone": normalize_phone(b.get("phone")),
    }


def same_place(a: dict, b: dict) -> bool:
    """Same normalized name and physically close (or same city if no coordinates)."""
    if a.get("lat") is not None and b.get("lat") is not None:
        return distance_m(a["lat"], a["lon"], b["lat"], b["lon"]) <= MAX_SAME_NAME_METERS
    return bool(a.get("city")) and (a.get("city") or "").lower() == (b.get("city") or "").lower()
