"""Per-customer qualification: decide whether an audited business is in a customer's
scope and, if so, score it with that customer's profile. Pure computation over data
already in the DB, so re-running it after a profile change costs no network requests."""
import logging

from . import db, profiles
from .scoring import exclusion_reason, score_lead, summarize

log = logging.getLogger(__name__)


def facts_of(findings: list[dict]) -> dict:
    return next((f["facts"] for f in findings or [] if f.get("code") == "_facts"), {})


def listed_email(business: dict) -> str | None:
    """Email published in the business's source listing (not scraped from its site).
    Only this counts toward the contact bonus, as in v0.1: scraped addresses are
    sometimes template placeholders (e.g. info@mysite.com)."""
    tags = (business.get("raw") or {}).get("tags", {})
    return tags.get("email") or tags.get("contact:email")


def qualify(business: dict, profile: dict, industries: set[str], areas: set[str]) -> dict | None:
    """Score one business for one customer, or None if it's out of the customer's scope."""
    if exclusion_reason(business, profile, areas, industries):
        return None
    findings = business.get("findings") or []
    scored = score_lead({**business, "email": listed_email(business)}, findings, profile)
    return {**scored, "summary": summarize(business, business.get("website_status"), scored["opportunities"],
                                           facts_of(findings), profile)}


def qualify_business(lead_id: int, customers: list[dict] | None = None) -> int:
    """(Re)qualify one audited business for every active customer. Returns assignments made."""
    business = db.get_business(lead_id)
    if not business or not business.get("analyzed_at"):
        return 0
    industries, areas = db.lead_scope(lead_id)
    made = 0
    for cust in customers if customers is not None else db.list_customers():
        q = qualify(business, cust["profile"], industries, areas)
        if q is None:
            db.unassign_untouched(cust["id"], lead_id)
        else:
            db.assign(cust["id"], lead_id, q)
            made += 1
    return made


def requalify_customer(customer: dict) -> int:
    """Rescore every audited business for one customer (after a profile change).
    Keeps the customer's statuses, notes and AI briefs."""
    n = sum(qualify_business(lid, [customer]) for lid in db.analyzed_business_ids())
    log.info("requalified %s: %d leads in scope", customer["slug"], n)
    return n


def sync_profiles() -> list[dict]:
    """Load profiles/*.json into the customers table; requalify customers whose profile changed."""
    for p in profiles.load_all():
        cid, changed = db.sync_customer(p)
        if changed:
            requalify_customer(db.get_customer(p["slug"]))
    return db.list_customers()
