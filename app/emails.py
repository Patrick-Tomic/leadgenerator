"""Find a contact email for a business, mainly ones with no website.

Uses one Tavily search (free tier, capped: TAVILY_MONTHLY_LIMIT) and reads only the
result snippets Tavily returns. Directory and social sites are never fetched or scraped by
this app. An email is kept only if the same result also names the business and shows its
phone number (high confidence) or its city (medium). Emails at directory or platform
domains (yelp.com, noreply@...) are dropped. Every email is stored with where it was seen.
"""
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx

from . import config, db
from .dedup import normalize_name, normalize_phone
from .discovery.base import SourceError
from .discovery.brave import DIRECTORY_DOMAINS, _fmt_phone, _registrable

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
JUNK = re.compile(r"^(no-?reply|donotreply|privacy|abuse|support@(yelp|facebook|google))|@(example|sentry|wixpress|"
                  r"domain|email|yourdomain|mysite)\.", re.I)
SITE_LABEL = {"yelp.com": "Yelp", "facebook.com": "Facebook", "bbb.org": "BBB", "nextdoor.com": "Nextdoor",
              "yellowpages.com": "Yellow Pages", "mapquest.com": "MapQuest", "instagram.com": "Instagram",
              "manta.com": "Manta", "chamberofcommerce.com": "Chamber of Commerce", "angi.com": "Angi"}


def _email_ok(email: str) -> bool:
    domain = email.split("@", 1)[1].lower().rstrip(".")
    return not JUNK.search(email) and _registrable(domain) not in DIRECTORY_DOMAINS - {"google.com"}


def extract(lead: dict, results: list[dict]) -> list[dict]:
    """Emails from search results that clearly belong to this business."""
    name = normalize_name(lead["name"]) or ""
    phone = normalize_phone(lead.get("phone"))
    city = (lead.get("city") or "").lower()
    found: dict[str, dict] = {}
    for r in results:
        text = f"{r.get('title', '')} {r.get('content', '')}"
        if not name or f" {name} " not in f" {normalize_name(text) or ''} ":
            continue
        digits = re.sub(r"\D", "", text)
        conf = "high" if phone and phone in digits else "medium" if city and city in text.lower() else None
        if not conf:
            continue
        host = _registrable(urlparse(r.get("url", "")).netloc)
        where = SITE_LABEL.get(host, host or "a web page")
        for email in {e.rstrip(".").lower() for e in EMAIL_RE.findall(text)}:
            if not _email_ok(email):
                continue
            prev = found.get(email)
            if not prev or (conf == "high" and prev["confidence"] != "high"):
                found[email] = {"email": email, "confidence": conf, "source_url": r.get("url"),
                                "evidence": f"in a {where} search snippet that names the business"
                                            + (" and shows its phone" if conf == "high" else f" and {lead.get('city')}")}
    return sorted(found.values(), key=lambda e: e["confidence"] != "high")


async def find(lead: dict, transport: httpx.AsyncBaseTransport | None = None) -> list[dict]:
    """One Tavily search for this business's email; stores and returns what it finds."""
    if not config.TAVILY_API_KEY:
        raise SourceError("Add TAVILY_API_KEY to .env to search for emails (free, no card).")
    period = datetime.now(timezone.utc).strftime("%Y-%m")
    if db.api_calls("search:tavily", period) >= config.TAVILY_MONTHLY_LIMIT:
        raise SourceError(f"Tavily monthly search limit reached ({config.TAVILY_MONTHLY_LIMIT})")
    parts = [f'"{lead["name"]}"', lead.get("city") or "", "FL" if not lead.get("city") else ""]
    phone = normalize_phone(lead.get("phone"))
    if phone:
        parts.append(_fmt_phone(phone))
    parts.append("email")
    query = " ".join(p for p in parts if p)
    db.count_api_call("search:tavily", period)
    try:
        async with httpx.AsyncClient(timeout=30, transport=transport) as client:
            resp = await client.post("https://api.tavily.com/search",
                                     headers={"Authorization": f"Bearer {config.TAVILY_API_KEY}"},
                                     json={"query": query, "search_depth": "basic", "max_results": 10})
    except httpx.HTTPError as e:
        raise SourceError(f"Tavily search failed: {type(e).__name__}") from e
    if resp.status_code in (432, 433):
        raise SourceError("Tavily plan limit reached for this month")
    if resp.status_code != 200:
        raise SourceError(f"Tavily search returned HTTP {resp.status_code}")
    emails = extract(lead, resp.json().get("results", []))
    save(lead["id"], emails, "search:tavily")
    return emails


def save(lead_id: int, emails: list[dict], source: str) -> None:
    ts = db.now()
    with db._lock:
        c = db.conn()
        for e in emails:
            c.execute("""INSERT INTO business_emails (lead_id, email, source, source_url, evidence, confidence, found_at)
                         VALUES (?, ?, ?, ?, ?, ?, ?)
                         ON CONFLICT (lead_id, email) DO UPDATE SET confidence=CASE WHEN excluded.confidence='high'
                         THEN 'high' ELSE business_emails.confidence END""",
                      (lead_id, e["email"], source, e.get("source_url"), e["evidence"], e["confidence"], ts))
        # A high-confidence email becomes the business's contact email if it has none yet.
        best = next((e for e in emails if e["confidence"] == "high"), None)
        if best:
            c.execute("UPDATE leads SET email=COALESCE(email, ?) WHERE id=?", (best["email"], lead_id))
        c.commit()


def emails_for(lead_id: int) -> list[dict]:
    return [dict(r) for r in db.conn().execute(
        "SELECT email, source, source_url, evidence, confidence, found_at FROM business_emails WHERE lead_id=? "
        "ORDER BY confidence='high' DESC, found_at", (lead_id,))]
