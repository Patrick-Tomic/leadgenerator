"""Website verification: does this business have a website, and which one?

Inputs are *candidates*, each from a named source:
  - the business's own source records (an OSM `website` tag, a manual correction)
  - optional finders: web search (Brave) and Google Places, when configured

Every candidate is fetched politely (robots.txt, per-host spacing), classified
(ok / blocked / robots / unreachable / dns / ssl / parked / social / directory), and
matched to the business using the page itself: the listed phone number, street
address, business name and city. A name alone is never enough to call a site theirs.

Resulting state (leads.website_state):
  verified          reachable, and the page or a person confirms it belongs to the business
  found_unverified  a site exists and a source lists it (or the match is reasonable), but
                    the page doesn't confirm the identity, or we couldn't read it
  possible_match    only weak candidates (e.g. a name-like domain with nothing identifying)
  none_found        configured searches ran and found no candidate
  unreachable       the best candidate exists but failed to load (timeout, 5xx, dead domain)
  unknown           no candidate and no search source ran. "Not listed" is not "none"
All candidates are stored, and conflicting candidates are flagged rather than discarded.
"""
import logging
import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from . import db
from .analysis.fetcher import PoliteFetcher
from .analysis.website import PARKED_SIGNS, is_social, normalize_url, phones_in
from .dedup import normalize_domain, normalize_name, normalize_phone, normalize_street
from .discovery.base import WebsiteFinder
from .discovery.brave import DIRECTORY_DOMAINS, _registrable

log = logging.getLogger(__name__)

STATES = ("verified", "found_unverified", "possible_match", "none_found", "unreachable", "unknown")
STATE_LABEL = {
    "verified": "Website verified",
    "found_unverified": "Website found, not yet verified",
    "possible_match": "Possible website, match uncertain",
    "none_found": "No website found after checks",
    "unreachable": "Website temporarily unreachable",
    "unknown": "Website status unknown",
}
# Checks meaning the site exists (even if we couldn't read it).
EXISTS = {"ok", "blocked", "robots", "ssl", "parked"}
SOURCE_PRIORITY = {"manual": 0, "osm": 1, "google_places": 2}
IDENTITY_RANK = {"high": 0, "medium": 1, "low": 2, None: 3}
MAX_CANDIDATES_CHECKED = 4


def business_profile(lead: dict) -> dict:
    """Everything known about the business across its sources, for matching."""
    names, phones, cities, addresses = {lead["name"]}, set(), set(), set()
    for src in db.lead_sources(lead["id"]) if lead.get("id") else []:
        f = src.get("fields") or {}
        names |= {f.get("name"), *(f.get("alt_names") or [])} - {None}
        phones |= set(f.get("phones") or []) | ({f["phone"]} if f.get("phone") else set())
        cities |= {f["city"]} if f.get("city") else set()
        addresses |= {f["address"]} if f.get("address") else set()
    phones |= {lead["phone"]} if lead.get("phone") else set()
    cities |= {lead["city"]} if lead.get("city") else set()
    addresses |= {lead["address"]} if lead.get("address") else set()
    return {"names": sorted(n for n in names if n), "phones": sorted({p for p in map(normalize_phone, phones) if p}),
            "cities": sorted(cities), "addresses": sorted(addresses), "name": lead["name"], "city": lead.get("city"),
            "lat": lead.get("lat"), "lon": lead.get("lon")}


def page_identity(profile: dict, html: str, final_url: str) -> tuple[str | None, list[str]]:
    """How strongly a fetched page identifies as this business, with the reasons."""
    soup = BeautifulSoup(html or "", "html.parser")
    text = soup.get_text(" ", strip=True)
    norm_text = " " + (normalize_name(text) or "") + " "
    evidence = []
    page_phones = phones_in(text, soup)
    phone_hit = sorted(set(profile["phones"]) & page_phones)
    if phone_hit:
        evidence.append(f"page shows the listed phone number ({phone_hit[0]})")
    street_hit = next((a for a in profile["addresses"] if normalize_street(a) and f" {normalize_street(a)} " in norm_text), None)
    if street_hit:
        evidence.append(f"page shows the street address ({normalize_street(street_hit)})")
    name_hit = next((n for n in profile["names"] if normalize_name(n) and f" {normalize_name(n)} " in norm_text), None)
    if name_hit:
        evidence.append(f'page mentions the business name ("{name_hit}")')
    city_hit = next((c for c in profile["cities"] if f" {normalize_name(c)} " in norm_text), None)
    if city_hit:
        evidence.append(f"page mentions {city_hit}")
    domain_word = (normalize_domain(final_url) or "").split(".")[0]
    tokens = [t for n in profile["names"] for t in (normalize_name(n) or "").split() if len(t) > 3]
    if domain_word and any(t in domain_word for t in tokens):
        evidence.append(f"domain resembles the business name ({domain_word})")
    if phone_hit or (name_hit and street_hit):
        return "high", evidence
    if name_hit and (city_hit or street_hit or "domain resembles" in " ".join(evidence)):
        return "medium", evidence
    if name_hit or evidence:
        return "low", evidence or ["nothing on the page identifies the business"]
    return "low", ["nothing on the page identifies the business"]


async def check_candidate(url: str, fetcher: PoliteFetcher) -> dict:
    """Fetch a candidate homepage and classify the outcome. Timeouts, 403s and 5xx are
    'unreachable'/'blocked': evidence the site may exist, never that it doesn't."""
    url = normalize_url(url)
    res = await fetcher.get(url)
    if not res.ok and res.error in ("connect", "timeout", "ssl", "other"):
        alt = url.replace("http://", "https://", 1) if url.startswith("http://") else url.replace("https://", "http://", 1)
        alt_res = await fetcher.get(alt)
        if alt_res.ok:
            res = alt_res
    out = {"url": url, "http_status": res.status, "final_url": res.final_url, "html": "", "redirects": res.redirects}
    if res.error == "blocked_by_robots":
        return {**out, "check_status": "robots", "detail": "robots.txt disallows our crawler; site exists"}
    if res.error == "dns":
        return {**out, "check_status": "dns", "detail": "domain doesn't resolve"}
    if res.error == "ssl":
        return {**out, "check_status": "ssl", "detail": "certificate error"}
    if res.error == "http_error" and res.status in (401, 403, 429):
        return {**out, "check_status": "blocked", "detail": f"HTTP {res.status} (bot protection or access control)"}
    if not res.ok:
        return {**out, "check_status": "unreachable", "detail": res.error_detail or res.error}
    final = res.final_url or url
    host = _registrable(urlparse(final).netloc)
    if is_social(final):
        return {**out, "check_status": "social", "detail": f"redirects to a social profile ({host})"}
    if host in DIRECTORY_DOMAINS:
        return {**out, "check_status": "directory", "detail": f"a directory listing ({host})"}
    lower = (res.html or "").lower()
    words = len(BeautifulSoup(res.html or "", "html.parser").get_text(" ", strip=True).split())
    hit = next((s for s in PARKED_SIGNS if s in lower), None)
    if hit and words < 400:
        return {**out, "check_status": "parked", "detail": f'placeholder/parked page ("{hit}")', "html": res.html}
    detail = f"HTTP {res.status}" + (f", redirected to {final}" if res.redirects else "")
    return {**out, "check_status": "ok", "detail": detail, "html": res.html}


def _classify_url(url: str) -> str | None:
    if is_social(normalize_url(url)):
        return "social"
    if _registrable(urlparse(normalize_url(url)).netloc) in DIRECTORY_DOMAINS:
        return "directory"
    return None


async def verify_website(lead: dict, fetcher: PoliteFetcher, finders: list[WebsiteFinder]) -> dict:
    """Collect, check and judge website candidates for one business; persist and return the result."""
    profile = business_profile(lead)
    stored = db.website_candidates(lead["id"]) if lead.get("id") else []
    cands = [{"url": c["url"], "source": c["source"], "source_record_id": c.get("source_record_id"),
              "evidence": list(c.get("match_evidence") or []), "base_identity": "high"} for c in stored
             if c["source"] in ("osm", "manual") or c["source"].startswith("sunbiz")]
    if not cands and lead.get("website"):  # e.g. a record created before candidates existed
        cands.append({"url": lead["website"], "source": lead.get("source") or "listing", "evidence": ["listed website"],
                      "base_identity": "high"})

    searches = []
    has_strong = any(_classify_url(c["url"]) is None for c in cands)
    for finder in finders:
        if has_strong and not finder.always:
            continue
        try:
            found = await finder.find_candidates(profile)
        except Exception as e:  # a failing finder never erases what we already know
            log.warning("%s lookup failed for %s: %s", finder.name, lead["name"], e)
            searches.append({"source": finder.name, "ran": False, "error": str(e)[:200]})
            continue
        searches.append({"source": finder.name, "ran": True, "conclusive": finder.conclusive, "queries": found.get("queries", []),
                         "results": len(found.get("checked", [])), "directories": found.get("directories", [])})
        for c in found["candidates"]:
            cands.append({**c, "base_identity": c.get("base_identity")})

    # Check each distinct URL once (by domain), strongest sources first.
    cands.sort(key=lambda c: (SOURCE_PRIORITY.get(c["source"], 3), IDENTITY_RANK[c.get("base_identity")]))
    checked_domains: dict[str, dict] = {}
    results = []
    for c in cands:
        kind = _classify_url(c["url"])
        dom = normalize_domain(c["url"]) or c["url"]
        if kind:
            check = {"check_status": kind, "detail": f"a {kind} page, not the business's own site", "final_url": c["url"]}
        elif dom in checked_domains:
            check = checked_domains[dom]
        elif len(checked_domains) >= MAX_CANDIDATES_CHECKED:
            check = {"check_status": "unchecked", "detail": "not checked (candidate limit)"}
        else:
            check = await check_candidate(c["url"], fetcher)
            checked_domains[dom] = check
        page_level, page_ev = (page_identity(profile, check.get("html", ""), check.get("final_url") or c["url"])
                               if check.get("check_status") == "ok" else (None, []))
        # Places data may only be used transiently: its identity counts only once our own
        # fetch of the site confirms it.
        if c.get("strict"):
            # Guessed addresses prove nothing by themselves: only the business's phone, or its
            # name plus street address, on the page makes it theirs. Anything else is someone else's site.
            identity = "high" if page_level == "high" else "low"
        elif c.get("transient"):
            identity = page_level or "low"
        else:
            identity = _combine(c.get("base_identity"), page_level)
        confirmed = c["source"] == "manual" or page_level in ("high", "medium")
        results.append({**c, **{k: v for k, v in check.items() if k != "html"}, "page_identity": page_level,
                        "identity": identity, "confirmed": confirmed, "match_evidence": c["evidence"] + page_ev,
                        "checked_at": db.now() if check.get("check_status") != "unchecked" else None})

    # A guessed address that isn't clearly theirs is someone else's site: drop it entirely
    # rather than showing it as a "possible website".
    results = [r for r in results if not (r.get("strict") and r["identity"] != "high")]
    state, best = _derive_state(results, searches)
    real = [r for r in results if r["identity"] in ("high", "medium") and r["check_status"] not in ("dns", "social", "directory")]
    conflict = len({normalize_domain(r.get("final_url") or r["url"]) for r in real}) > 1
    canonical = None
    if best and state in ("verified", "found_unverified", "unreachable"):
        canonical = best["url"]

    if lead.get("id"):
        for r in results:
            db.save_candidate(lead["id"], _storable(r))
        db.save_website_state(lead["id"], state, canonical, conflict)
    return {"state": state, "canonical": canonical, "best": best, "conflict": conflict, "candidates": results,
            "searches": searches, "profile": profile}


def _storable(r: dict) -> dict:
    """Google Places content isn't stored unless our own check of the website identified
    the business; otherwise keep only the place ID (allowed) as a Maps link for review."""
    if not r.get("transient") or r.get("page_identity") in ("high", "medium"):
        return r
    pid = r.get("source_record_id")
    return {"url": f"https://www.google.com/maps/place/?q=place_id:{pid}", "source": r["source"], "source_record_id": pid,
            "identity": "low", "match_evidence": ["Google Places listed a website for a matching place; not stored "
                                                  "(Google terms) because our own check didn't confirm it"],
            "check_status": "unchecked", "detail": "open the place in Google Maps to review", "checked_at": r.get("checked_at")}


def _combine(base: str | None, page: str | None) -> str:
    """Source-stated sites start high; search finds start from what the page shows."""
    levels = [x for x in (base, page) if x]
    return min(levels, key=lambda x: IDENTITY_RANK[x]) if levels else "low"


def _derive_state(results: list[dict], searches: list[dict]) -> tuple[str, dict | None]:
    def rank(r):
        return (IDENTITY_RANK[r["identity"]], not r["confirmed"], r["check_status"] != "ok", SOURCE_PRIORITY.get(r["source"], 3))
    exists = sorted((r for r in results if r["check_status"] in EXISTS), key=rank)
    if exists:
        top = exists[0]
        if top["identity"] == "high" and top["confirmed"] and top["check_status"] == "ok":
            return "verified", top
        if top["identity"] in ("high", "medium"):
            return "found_unverified", top
        return "possible_match", top
    failing = sorted((r for r in results if r["check_status"] in ("unreachable", "dns", "unchecked")
                      and r["identity"] in ("high", "medium")), key=rank)
    if failing:
        return "unreachable", failing[0]
    weak = sorted((r for r in results if r["check_status"] in ("unreachable", "unchecked")), key=rank)
    if weak:
        return "possible_match", weak[0]
    social = next((r for r in results if r["check_status"] == "social"), None)
    # Only a real search that found nothing supports "no website found"; guessing addresses
    # and coming up empty proves nothing.
    if any(s["ran"] and s.get("conclusive", True) for s in searches):
        return "none_found", social
    return "unknown", social
