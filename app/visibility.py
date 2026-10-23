"""How well known is a business? A free estimate from signals we already collect.

This is an estimate, not a review count. The real Google review count, when a Google
Places key is configured, comes from app/reviews.py and is labeled as Google data.

Signals that a business is well known:
  - it has a Wikipedia/Wikidata entry (OSM wikidata/wikipedia tags)
  - its site embeds a review widget (Birdeye, Podium, Trustindex...): it has reviews to show
  - it links three or more social profiles
  - its homepage links to many pages (a large site)
  - it has held a state contractor license for over 10 years
Signals that it isn't:
  - registered or licensed within the last year
  - no website, or a small site (under 10 internal links)
  - no social profiles linked (when the site was checked)
"""
from datetime import date, timedelta

LEVEL_LABEL = {"low": "Not well known", "medium": "Somewhat known", "high": "Well known", "unknown": "Not enough information"}


def estimate(business: dict, facts: dict, sources: list[dict]) -> dict:
    tags = (business.get("raw") or {}).get("tags") or {}
    fields = [s.get("fields") or {} for s in sources]
    known, unknown = [], []

    if tags.get("wikidata") or tags.get("wikipedia"):
        known.append(("Has a Wikipedia/Wikidata entry", 3))
    if facts.get("review_widgets"):
        known.append((f"Website shows a review widget ({', '.join(facts['review_widgets'])})", 2))
    social = set(facts.get("social_links") or []) | {k.title() for f in fields for k in (f.get("social") or {})}
    if len(social) >= 3:
        known.append((f"Links {len(social)} social profiles", 1))
    links = facts.get("internal_link_count")
    if links is not None and links >= 30:
        known.append((f"Large website ({links}+ internal links)", 1))

    year_ago = (date.today() - timedelta(days=365)).isoformat()
    decade_ago = (date.today() - timedelta(days=3650)).isoformat()
    filed = [f.get("filing_date") for f in fields if f.get("filing_date")]
    licensed = [f.get("licensed_since") for f in fields if f.get("licensed_since")]
    if any(d >= year_ago for d in filed + licensed):
        unknown.append(("Registered or licensed within the last year", 2))
    if licensed and min(licensed) < decade_ago:
        known.append((f"State-licensed since {min(licensed)[:4]}", 1))
    checked_site = "internal_link_count" in facts
    state = business.get("website_state") or facts.get("website_state")
    if state in ("none_found", "unknown") and not business.get("website"):
        unknown.append(("No website found", 1))
    elif links is not None and links < 10:
        unknown.append(("Small website", 1))
    if checked_site and not social:
        unknown.append(("No social profiles linked", 1))

    k, u = sum(p for _, p in known), sum(p for _, p in unknown)
    if not known and not unknown:
        level = "unknown"
    elif k >= 3 or k - u >= 2:
        level = "high"
    elif k == 0 and u >= 1:
        level = "low"
    else:
        level = "medium"
    return {"level": level, "label": LEVEL_LABEL[level],
            "reasons": [r for r, _ in known] + [r for r, _ in unknown], "estimate": True}
