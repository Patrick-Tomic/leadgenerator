"""Turn audit findings into a customer's opportunities and a 0-100 lead score.

Discovery and the website audit are customer-independent; this module is the
customer-specific part. Offerings, weights and exclusions come from the customer's
profile (profiles/*.json). Points come only from observed findings, weighted by
severity and confidence, so a small business with a decent site scores low instead
of being assumed needy.
"""
import re
from functools import lru_cache

from . import config
from .dedup import normalize_domain
from .profiles import default_profile

# Every finding gets a kind so the UI and reports never present a guess as fact:
#   confirmed   - a problem we directly observed with high confidence (mailto: form, no viewport)
#   potential   - absence of an optional feature, or an observation below high confidence
#                 (e.g. a dead domain: the business may simply have moved)
#   unavailable - we couldn't check (bot protection, robots.txt, unreadable page)
#   info        - neutral context, not an issue
UNAVAILABLE_CODES = {"access_restricted", "robots_blocked", "unreadable_page", "analysis_error"}
POTENTIAL_CODES = {"no_booking", "no_chat", "no_schema", "no_sitemap", "no_website_unverified"}
INFO_CODES = {"website_found_by_search"}


def classify_finding(f: dict) -> str:
    if f["code"] in UNAVAILABLE_CODES:
        return "unavailable"
    if f["code"] in INFO_CODES:
        return "info"
    if f["code"] in POTENTIAL_CODES or f.get("confidence") != "high":
        return "potential"
    return "confirmed"


@lru_cache(maxsize=1)
def _default() -> dict:
    return default_profile()


def finding_points(f: dict, scoring: dict) -> float:
    if f["code"] in scoring["point_overrides"]:
        return scoring["point_overrides"][f["code"]]
    return scoring["severity_points"].get(f["severity"], 0) * scoring["confidence_mult"].get(f["confidence"], 0.5)


def score_lead(business: dict, findings: list[dict], profile: dict | None = None) -> dict:
    profile = profile or _default()
    scoring = profile["scoring"]
    findings = [f for f in findings if f.get("code") != "_facts"]
    opportunities = []
    total = 0.0
    for key, opp in profile["offerings"].items():
        codes = set(opp["codes"])
        matched = [f for f in findings if f["code"] in codes]
        if not matched:
            continue
        pts = min(opp["cap"], sum(finding_points(f, scoring) for f in matched))
        total += pts
        kinds = {classify_finding(f) for f in matched}
        opportunities.append({
            "key": key,
            "label": opp["label"],
            "pitch": opp["pitch"],
            "points": round(pts),
            "evidence": [f["evidence"] for f in matched],
            "confidence": max((f["confidence"] for f in matched), key=lambda c: scoring["confidence_mult"].get(c, 0)),
            "basis": "confirmed" if "confirmed" in kinds else "potential",
        })
    opportunities.sort(key=lambda o: -o["points"])

    has_phone, has_email = bool(business.get("phone")), bool(business.get("email"))
    if has_phone:
        total += scoring["contact_bonus"].get("phone", 0)
    if has_email:
        total += scoring["contact_bonus"].get("email", 0)
    if not (has_phone or has_email or business.get("address")):
        total *= scoring["unreachable_multiplier"]  # a lead you can't contact isn't worth much

    score = max(0, min(100, round(total)))
    t = scoring["grade_thresholds"]
    grade = "A" if score >= t["A"] else "B" if score >= t["B"] else "C" if score >= t["C"] else "D"
    return {"score": score, "grade": grade, "opportunities": opportunities}


def in_bbox(lat: float, lon: float, bbox: tuple) -> bool:
    s, w, n, e = bbox
    return s <= lat <= n and w <= lon <= e


def exclusion_reason(lead: dict, profile: dict, lead_areas: set[str] = frozenset(),
                     lead_industries: set[str] = frozenset()) -> str | None:
    """Why this lead is out of scope for the customer, or None if it's in scope.

    lead_areas / lead_industries are the scan areas and categories the business was
    found under (a business can match several); they are used alongside coordinates.
    """
    industries = set(lead_industries) | {lead["industry"]}
    if not industries & set(profile["industries"]):
        return "industry not targeted"
    if lead.get("lat") is not None and lead.get("lon") is not None:
        if not any(in_bbox(lead["lat"], lead["lon"], config.AREAS[a]["bbox"]) for a in profile["areas"]):
            return "outside service areas"
    elif not set(lead_areas) & set(profile["areas"]):
        return "outside service areas"
    ex = profile["exclusions"]
    for pat in ex["name_patterns"]:
        if re.search(pat, lead["name"], re.I):
            return f"name matches exclusion /{pat}/"
    domain = normalize_domain(lead.get("final_url") or lead.get("website"))
    if domain and domain in {d.lower() for d in ex["domains"]}:
        return f"domain {domain} excluded"
    if lead.get("website_status") in ex["website_statuses"]:
        return f"website status '{lead['website_status']}' excluded"
    return None


def summarize(business: dict, website_status: str, opportunities: list[dict], facts: dict,
              profile: dict | None = None) -> str:
    profile = profile or _default()
    industry = config.INDUSTRIES.get(business["industry"], {}).get("label", business["industry"])
    where = business.get("city") or profile["region_label"]
    lines = [f"{business['name']}: {industry}, {where}."]
    status_text = {
        "none": "No website was found for them.",
        "unverified": "No website is listed for them in public map data (not yet confirmed by search).",
        "ok": f"Their website ({facts.get('final_url', business.get('website'))}) loads"
              + (f" and is built on {facts['platform']}." if facts.get("platform") else "."),
        "down": "Their listed website is currently broken.",
        "social_only": "Their 'website' is only a social media profile.",
        "placeholder": "Their domain shows a placeholder or parked page.",
        "restricted": "Their website blocked our automated check; review it in a browser.",
        "robots_blocked": "Their site disallows automated analysis; review it manually.",
    }
    lines.append(status_text.get(website_status, ""))
    if opportunities:
        top = opportunities[0]
        lines.append(f"Best angle: {top['label'].lower()}. {top['pitch']}")
        if len(opportunities) > 1:
            lines.append("Also: " + ", ".join(o["label"].lower() for o in opportunities[1:4]) + ".")
    else:
        lines.append("No significant opportunities found; their online presence looks solid.")
    return " ".join(l for l in lines if l)
