"""Lead details with provenance: every field any source provided, which source said
what, and where sources disagree. Built for the lead detail view and API so nothing a
source contributed silently disappears."""
from . import db
from .dedup import normalize_domain, normalize_phone, normalize_street
from .websites import STATE_LABEL

SOURCE_LABEL = {"osm": "OpenStreetMap", "manual": "Entered by you", "sunbiz_cor": "Florida Sunbiz (corporation/LLC)",
                "sunbiz_fic": "Florida Sunbiz (fictitious name)", "google_places": "Google Places",
                "search:brave": "Brave search", "website": "Business's own website"}
# Field, label, and how to compare values for conflicts (None: not compared).
FIELDS = [
    ("name", "Name", lambda v: v.lower()), ("alt_names", "Other names", None), ("legal_name", "Legal entity name", None),
    ("entity_type", "Entity type", None), ("filing_date", "State filing date", None), ("doc_number", "State document no.", None),
    ("address", "Address", normalize_street), ("registered_address", "Address on state filing (not confirmed as operating location)", None),
    ("city", "City", lambda v: v.lower()), ("postcode", "ZIP", None),
    ("phone", "Phone", normalize_phone), ("phones", "All phone numbers", None), ("email", "Email", lambda v: v.lower()),
    ("website", "Website", normalize_domain), ("social", "Social profiles", None), ("opening_hours", "Opening hours", None),
    ("description", "Description", None), ("operator", "Operator", None), ("start_date", "Start date", None),
]
CONFLICT_FIELDS = {"phone", "email", "website", "address"}


def label(source: str) -> str:
    return SOURCE_LABEL.get(source, source)


def build(lead: dict, facts: dict) -> dict:
    sources = db.lead_sources(lead["id"])
    rows = []
    for key, title, norm in FIELDS:
        values: dict = {}
        for s in sources:
            v = (s.get("fields") or {}).get(key)
            if v in (None, "", [], {}):
                continue
            display = ", ".join(f"{k}: {x}" for k, x in v.items()) if isinstance(v, dict) else \
                      ", ".join(v) if isinstance(v, list) else str(v)
            k = (norm(display) if norm and isinstance(v, str) else None) or display
            values.setdefault(k, {"value": display, "sources": []})["sources"].append(label(s["source"]))
        if key == "phone":
            for p in facts.get("site_phones") or []:
                k = normalize_phone(p)
                values.setdefault(k, {"value": f"({p[:3]}) {p[3:6]}-{p[6:]}", "sources": []})["sources"].append(label("website"))
        if key == "email":
            for e in facts.get("emails") or []:
                values.setdefault(e.lower(), {"value": e, "sources": []})["sources"].append(label("website"))
        if values:
            rows.append({"field": key, "label": title, "values": list(values.values()),
                         "conflict": key in CONFLICT_FIELDS and len(values) > 1})
    return {
        "fields": rows,
        "sources": [{"source": s["source"], "label": label(s["source"]), "record_id": s["source_id"], "url": s.get("source_url"),
                     "fields": sorted((s.get("fields") or {}).keys()), "match_confidence": s.get("match_confidence"),
                     "match_explanation": s.get("match_explanation"), "first_seen_at": s.get("first_seen_at"),
                     "last_checked_at": s.get("last_checked_at") or s.get("last_seen_at"),
                     "note": (s.get("metadata") or {}).get("note")} for s in sources],
        "website": {
            "state": lead.get("website_state") or "unknown",
            "label": STATE_LABEL.get(lead.get("website_state") or "unknown"),
            "conflict": bool(lead.get("website_conflict")),
            "checked_at": lead.get("website_checked_at"),
            "candidates": [{"url": c["url"], "source": label(c["source"]), "identity": c.get("identity"),
                            "check_status": c.get("check_status"), "http_status": c.get("http_status"),
                            "final_url": c.get("final_url"), "detail": c.get("detail"), "evidence": c.get("match_evidence") or [],
                            "selected": bool(c.get("selected")), "checked_at": c.get("checked_at")}
                           for c in db.website_candidates(lead["id"])],
        },
    }
