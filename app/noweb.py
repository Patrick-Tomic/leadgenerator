"""Businesses with no web presence: no website from any source, and (when checked) no
Google listing. These are prospects for a first website.

Honesty rules: "no website" here means no source listed one and no website candidate was
found. It's confirmed only when a website search ran (state none_found). "No Google
listing" is known only after a Google Places check (needs a key); otherwise it's
"not checked". Both are labeled that way in the UI.
"""
from . import config, db

HAS_EMAIL = "(l.email IS NOT NULL OR EXISTS (SELECT 1 FROM business_emails e WHERE e.lead_id = l.id))"
HAS_OWNER = "EXISTS (SELECT 1 FROM business_people p WHERE p.lead_id = l.id)"
# Most reachable first: an email, then a phone, then a name to ask for.
ORDER = f"""ORDER BY {HAS_EMAIL} DESC, (l.phone IS NOT NULL) DESC, {HAS_OWNER} DESC,
                   CASE WHEN l.google_listing = 0 THEN 0 WHEN l.google_listing IS NULL THEN 1 ELSE 2 END,
                   (l.website_state = 'none_found') DESC, l.discovered_at DESC"""


def _scope(profile: dict) -> tuple[str, list]:
    areas = sorted(a for a in config.AREAS if config.area_ancestors(a) & set(profile["areas"]))
    industries = list(profile["industries"])
    sql = (f"EXISTS (SELECT 1 FROM lead_sources s WHERE s.lead_id = l.id AND s.area IN ({','.join('?' * len(areas))})) "
           f"AND l.industry IN ({','.join('?' * len(industries))})")
    return sql, [*areas, *industries]


def query(customer: dict, industry: str | None = None, area: str | None = None, google: str = "any",
          limit: int = 100, offset: int = 0, has_owner: bool = False, has_email: bool = False) -> dict:
    """google: any | not_found | unchecked | found."""
    scope, args = _scope(customer["profile"])
    where = [scope,
             "l.website IS NULL",
             "COALESCE(l.website_state, 'unknown') IN ('unknown', 'none_found')",
             "NOT EXISTS (SELECT 1 FROM website_candidates w WHERE w.lead_id = l.id AND w.identity IN ('high', 'medium'))"]
    if industry:
        where.append("l.industry = ?")
        args.append(industry)
    if area:
        child = sorted(a for a in config.AREAS if area in config.area_ancestors(a))
        where.append(f"EXISTS (SELECT 1 FROM lead_sources s2 WHERE s2.lead_id = l.id AND s2.area IN ({','.join('?' * len(child))}))")
        args += child
    if has_owner:
        where.append(HAS_OWNER)
    if has_email:
        where.append(HAS_EMAIL)
    if google == "not_found":
        where.append("l.google_listing = 0")
    elif google == "unchecked":
        where.append("l.google_listing IS NULL")
    elif google == "found":
        where.append("l.google_listing = 1")
    sql_where = " AND ".join(where)
    c = db.conn()
    total = c.execute(f"SELECT COUNT(*) FROM leads l WHERE {sql_where}", args).fetchone()[0]
    rows = c.execute(
        f"""SELECT l.id, l.name, l.industry, l.city, l.address, l.phone, l.email, l.website_state, l.google_listing,
                   l.google_checked_at, l.discovered_at, cl.status, cl.notes
            FROM leads l LEFT JOIN customer_leads cl ON cl.lead_id = l.id AND cl.customer_id = ?
            WHERE {sql_where} {ORDER} LIMIT ? OFFSET ?""",
        [customer["id"], *args, limit, offset]).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["industry_label"] = config.INDUSTRIES.get(d["industry"], {}).get("label", d["industry"])
        d["website_label"] = "No website found (searched)" if d["website_state"] == "none_found" else "No website listed (not confirmed)"
        d["google_label"] = {None: "Not checked", 0: "No Google listing found", 1: "Has a Google listing"}[d["google_listing"]]
        d["social"] = _social(d["id"])
        from .emails import emails_for
        from .owners import people_for
        d["owners"] = [{"name": p["name"], "title": p["title"], "confidence": p["match_confidence"]} for p in people_for(d["id"])[:3]]
        found = emails_for(d["id"])
        d["emails"] = ([{"email": d["email"], "evidence": "listed on the business's record", "confidence": "high"}]
                       if d["email"] and d["email"] not in {e["email"] for e in found} else []) + found
        out.append(d)
    return {"total": total, "rows": out, "limit": limit, "offset": offset}


def _social(lead_id: int) -> dict:
    """Social profiles from the business's source records (a web presence of sorts)."""
    merged = {}
    for s in db.lead_sources(lead_id):
        merged.update((s.get("fields") or {}).get("social") or {})
    return merged
