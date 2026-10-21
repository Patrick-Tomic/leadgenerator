"""Turn audit findings into a customer's opportunities and a 0-100 lead score.

Discovery and the website audit are customer-independent; this module is the
customer-specific part (offerings, weights and exclusions come from the customer's
profile in profiles/*.json).

How a score is built, so it reflects evidence rather than a count of issues:
  1. Only findings that map to one of the customer's offerings count (relevance).
  2. Each finding's value = signal-strength points (app/findings.py tier, or a profile
     override) x confidence (verified / probable / unverified) x actionability.
  3. The strongest finding counts in full, the next two at a fraction (`decay`), the
     rest not at all, so ten small issues can't outrank one consequential one.
  4. Optional features (no chat widget, no booking tool, ...) add at most `optional_cap`.
  5. The total is multiplied by how well the business's contact details are verified.
Each score carries an explanation: its strongest evidence and main uncertainty.
"""
import re
from functools import lru_cache

from . import config
from .dedup import normalize_domain
from .findings import CONFIDENCE_LEVEL, TIER_ORDER, confidence_level, enrich, entry
from .profiles import default_profile

# Every finding gets a kind so the UI and reports never present a guess as fact:
#   confirmed   - a problem we directly observed (verified confidence)
#   potential   - an optional feature that's absent, or an observation below verified
#   unavailable - we couldn't check (bot protection, robots.txt, unreadable page)
#   info        - neutral context, not an issue (e.g. a form we found but didn't test)
UNAVAILABLE_CODES = {"access_restricted", "robots_blocked", "unreadable_page", "analysis_error",
                     "website_unknown", "website_match_uncertain", "no_website_unverified"}
INFO_CODES = {"website_found_by_search", "form_untested", "website_conflict", "website_identity_unconfirmed", "phone_from_website"}
# Findings about "is there a website at all"; everything else is about the audited site,
# and counts less if we couldn't confirm that site belongs to the business.
WEBSITE_EXISTENCE_CODES = {"no_website_confirmed", "no_website_unverified", "social_only", "parked_or_placeholder",
                           "new_registration"}
DEFAULT_SITE_IDENTITY_MULT = {"verified": 1.0, "found_unverified": 0.6, "unreachable": 0.8}


def classify_finding(f: dict) -> str:
    if f["code"] in UNAVAILABLE_CODES:
        return "unavailable"
    if f["code"] in INFO_CODES:
        return "info"
    if entry(f["code"])["tier"] == "optional" or f.get("confidence") != "high":
        return "potential"
    return "confirmed"


@lru_cache(maxsize=1)
def _default() -> dict:
    return default_profile()


def finding_value(f: dict, scoring: dict) -> float:
    e = entry(f["code"])
    if e["tier"] == "none":
        return 0.0
    base = scoring["point_overrides"].get(f["code"], scoring["tier_points"][e["tier"]])
    return base * scoring["confidence_mult"][confidence_level(f)] * e["actionability"]


def contact_status(business: dict, facts: dict) -> str:
    """verified: the listing's phone also appears on the business's own website, or two
    independent sources list the same phone.
    listed: phone or email from the business listing. site_only: only the website shows contact details."""
    if facts.get("contact_corroborated") or facts.get("phone_sources", 0) >= 2:
        return "verified"
    if business.get("phone") or business.get("email"):
        return "listed"
    if facts.get("site_phones") or facts.get("emails"):
        return "site_only"
    return "none"


CONTACT_NOTE = {
    "verified": None,
    "listed": "Contact details come from the map listing only; confirm the phone number before calling.",
    "site_only": "No listing contact details; use the phone/email shown on their website.",
    "none": "No phone or email found anywhere; find contact details manually.",
}


def score_lead(business: dict, findings: list[dict], profile: dict | None = None, facts: dict | None = None) -> dict:
    profile = profile or _default()
    scoring = profile["scoring"]
    facts = facts or {}
    findings = [f for f in findings if f.get("code") != "_facts"]
    offering_of = {}
    for key, opp in profile["offerings"].items():
        for code in opp["codes"]:
            offering_of.setdefault(code, key)

    # Relevant findings and their values.
    scored = []
    for f in findings:
        key = offering_of.get(f["code"])
        v = finding_value(f, scoring) if key else 0.0
        if v and f["code"] not in WEBSITE_EXISTENCE_CODES and facts.get("website_state"):
            # Problems on a site we couldn't confirm is theirs are weaker evidence.
            v *= scoring.get("site_identity_mult", DEFAULT_SITE_IDENTITY_MULT).get(facts["website_state"], 1.0)
        if v > 0:
            scored.append({"f": f, "offering": key, "value": v, "tier": entry(f["code"])["tier"]})
    main = sorted((x for x in scored if x["tier"] != "optional"), key=lambda x: -x["value"])
    optional = [x for x in scored if x["tier"] == "optional"]
    for i, x in enumerate(main):
        x["points"] = x["value"] * (scoring["decay"][i] if i < len(scoring["decay"]) else 0)
    opt_total = min(scoring["optional_cap"], sum(x["value"] for x in optional))
    opt_sum = sum(x["value"] for x in optional) or 1
    for x in optional:
        x["points"] = x["value"] * opt_total / opt_sum

    contact = contact_status(business, facts)
    mult = scoring["contact_mult"][contact]
    raw = sum(x["points"] for x in main) + opt_total
    score = max(0, min(100, round(raw * mult)))
    t = scoring["grade_thresholds"]
    grade = "A" if score >= t["A"] else "B" if score >= t["B"] else "C" if score >= t["C"] else "D"

    # Opportunities: the customer's services, each with the evidence behind it.
    opportunities = []
    for key, opp in profile["offerings"].items():
        items = [x for x in scored if x["offering"] == key]
        if not items:
            continue
        items.sort(key=lambda x: (TIER_ORDER[x["tier"]], -x["value"]))
        best = items[0]["f"]
        opportunities.append({
            "key": key,
            "label": opp["label"],
            "pitch": opp["pitch"],
            "points": round(sum(x["points"] for x in items)),
            "evidence": [x["f"]["evidence"] for x in items],
            "confidence": CONFIDENCE_LEVEL.get(best.get("confidence"), "unverified"),
            "basis": "confirmed" if any(classify_finding(x["f"]) == "confirmed" for x in items) else "potential",
            "tier": items[0]["tier"],
        })
    opportunities.sort(key=lambda o: (TIER_ORDER[o["tier"]], -o["points"]))

    explanation = explain(scored, main, findings, profile, contact, score)
    return {"score": score, "grade": grade, "opportunities": opportunities, "explanation": explanation}


def explain(scored: list, main: list, findings: list[dict], profile: dict, contact: str, score: int) -> dict:
    """Strongest evidence, principal uncertainty and what to verify by hand."""
    top = main[0] if main else (max(scored, key=lambda x: x["value"]) if scored else None)
    strongest = None
    if top:
        f = enrich(top["f"])
        strongest = {k: f.get(k) for k in ("code", "title", "evidence", "page", "confidence_level", "tier", "tier_label",
                                            "impact", "talking_point")}
        strongest["service"] = profile["offerings"][top["offering"]]["label"]
    verify: list[str] = []

    def add(note):
        if note and note not in verify:
            verify.append(note)

    if top:
        add(enrich(top["f"])["verify"])
    for x in main[1:3]:
        if x["f"].get("confidence") != "high":
            add(enrich(x["f"])["verify"])
    for f in findings:
        if f["code"] in UNAVAILABLE_CODES or f["code"] in INFO_CODES:
            add(enrich(f)["verify"])
    add(CONTACT_NOTE[contact])

    if top is None:
        uncertainty = "No problems relevant to this customer's services were observed."
    elif top["f"].get("confidence") != "high" or entry(top["f"]["code"])["actionability"] < 1:
        uncertainty = enrich(top["f"])["verify"] or "The strongest finding isn't fully verified."
    elif top["tier"] == "optional":
        uncertainty = "Only optional features are missing; there's no evidence the business needs them."
    else:
        uncertainty = next((v for v in verify[1:]), "None noted beyond a manual look at the site.")

    if top:
        text = (f"Score {score} rests mainly on: {strongest['title']} ({strongest['confidence_level']}, "
                f"{strongest['tier_label'].lower()}). Main uncertainty: {uncertainty}")
    else:
        text = f"Score {score}: {uncertainty}"
    return {"strongest": strongest, "uncertainty": uncertainty, "verify": verify[:6], "contact": contact,
            "components": [{"code": x["f"]["code"], "points": round(x["points"], 1)} for x in sorted(scored, key=lambda x: -x["points"])],
            "text": text}


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
    has_coords = lead.get("lat") is not None and lead.get("lon") is not None
    found_in = set().union(*(config.area_ancestors(a) for a in lead_areas)) if lead_areas else set()

    def covers(area: str) -> bool:
        spec = config.AREAS[area]
        if has_coords and not spec.get("extract"):
            return in_bbox(lead["lat"], lead["lon"], spec["bbox"])   # counties: exact location check
        if area in found_in:
            return True    # found by a scan of this area or of a county inside it (states: exact boundaries)
        return has_coords and not lead_areas and in_bbox(lead["lat"], lead["lon"], spec["bbox"])

    if not any(covers(a) for a in profile["areas"]):
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
              profile: dict | None = None, explanation: dict | None = None) -> str:
    """One factual paragraph: what we observed and how sure we are. No claims of lost revenue."""
    profile = profile or _default()
    industry = config.INDUSTRIES.get(business["industry"], {}).get("label", business["industry"])
    where = business.get("city") or profile["region_label"]
    lines = [f"{business['name']}: {industry}, {where}."]
    status_text = {
        "none": "Search found no website for them.",
        "unverified": "Their map listing has no website (not confirmed by search).",
        "ok": f"Their website ({facts.get('final_url', business.get('website'))}) loads"
              + (f" and is built on {facts['platform']}." if facts.get("platform") else "."),
        "down": "Their listed website failed to load on repeated attempts.",
        "social_only": "Their listed website is a social media profile.",
        "placeholder": "Their domain shows a placeholder or parked page.",
        "restricted": "Their website blocked our automated check; review it in a browser.",
        "robots_blocked": "Their site disallows automated analysis; review it manually.",
    }
    lines.append(status_text.get(website_status, ""))
    strongest = (explanation or {}).get("strongest")
    if strongest and strongest["tier"] != "optional":
        lines.append(f"Strongest evidence ({strongest['confidence_level']}): {strongest['title'].rstrip('.')}. "
                     f"Suggested service: {strongest['service'].lower()}.")
    elif opportunities:
        lines.append("Only optional features are missing; no evidence of a problem the business needs solved.")
    else:
        lines.append("No problems relevant to these services were observed.")
    return " ".join(l for l in lines if l)
