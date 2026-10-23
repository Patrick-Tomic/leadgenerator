"""v0.7: built-in cold email drafts."""
import pytest

from app import config, db
from app.findings import CATALOG
from app.outreach import draft
from app.scoring import score_lead

SENDER = {"name": "Pat Example", "company": "Atomic Consulting", "address": "123 Main St, Tampa, FL 33602",
          "email": "pat@example.com", "phone": "(813) 555-0100"}


def lead_with(findings, **kw):
    business = {"name": "Acme Plumbing", "industry": "plumbing", "phone": "8135550101"}
    scored = score_lead(business, findings)
    from app.findings import enrich
    return {"id": 1, **business, "email": "info@acme.example", "website": "https://www.acme.example/",
            "final_url": "https://www.acme.example/", "findings": [enrich(f) for f in findings],
            "explanation": scored["explanation"], **kw}


def f(code, confidence="high", **kw):
    return {"code": code, "title": CATALOG.get(code, {}).get("title", code.replace("_", " ").capitalize()),
            "evidence": f"evidence {code}", "severity": "high", "confidence": confidence, "page": "https://www.acme.example/contact", **kw}


def test_verified_problem_is_stated_plainly():
    d = draft(lead_with([f("broken_important_link", details={"link_label": '"Contact us"'})]), SENDER)
    assert d["subjects"][0] == "Quick note about acme.example" and "acme.example" in d["subjects"][1]
    assert "We help local businesses with fixing website problems." in d["body"]
    assert "noticed a few things on your site that aren't working the way a customer would expect." in d["body"]
    assert "/contact" not in d["body"] and "what looks like" not in d["body"] and d["based_on"]   # details held back
    assert d["verify"][0].startswith("Specifics to share on the call:")
    assert d["warnings"] == []


def test_probable_problem_is_hedged():
    d = draft(lead_with([f("phone_mismatch", "medium", details={"listing_phone": "(813) 555-0101", "site_phone": "(813) 555-0199"})]), SENDER)
    assert "noticed what looks like some inconsistent contact details" in d["body"]       # probable -> hedged
    assert "555-0199" not in d["body"] and "555-0101" not in d["body"]                     # specifics held back
    assert any("which number is current" in v for v in d["verify"])


def test_unverified_and_optional_findings_are_never_claimed():
    d = draft(lead_with([f("no_chat", "medium"), f("no_booking", "medium"), f("no_contact_form", "low"),
                         f("website_unknown", "low")]), SENDER)
    body = d["body"].lower()
    assert "chat" not in body and "booking" not in body and "quote online" not in body and "no website" not in body
    assert any("general introduction" in w for w in d["warnings"])


def test_email_ends_with_opt_out_then_thanks_for_your_own_signature():
    d = draft(lead_with([f("site_ssl")]), SENDER)
    assert d["body"].rstrip().endswith('If you\'d rather not hear from me, just reply "no thanks" and I won\'t email again.\n\nThanks,')
    assert "123 Main St" not in d["body"] and "pat@example.com" not in d["body"]   # no generated signature block
    assert "I'm Pat Example with Atomic Consulting" in d["body"] and d["warnings"] == []


def test_followup_variant_and_mailto():
    d = draft(lead_with([f("site_ssl")]), SENDER, "followup")
    assert d["subjects"][0].startswith("Re:") and "Following up" in d["body"] and "site security" in d["body"]
    assert d["mailto"].startswith("mailto:info@acme.example?subject=")
    assert draft(lead_with([f("site_ssl")], email=None), SENDER)["mailto"] is None


@pytest.mark.parametrize("code", [c for c, e in CATALOG.items() if e.get("talk")])
def test_no_drafts_claim_lost_business(code):
    d = draft(lead_with([f(code, details={"link_label": "x", "claim": "book online", "site_phone": "1", "listing_phone": "2"})]),
              SENDER, "detailed")
    text = (d["body"] + " ".join(d["subjects"])).lower()
    for banned in ("losing", "lost revenue", "lost customers", "competitor", "guarantee"):
        assert banned not in text, (code, banned)


@pytest.fixture
def tmpdb(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "backups")
    db.use(tmp_path / "leads.db")
    yield
    db._conn.close()
    db._conn = None


def test_email_api_and_sender_settings(tmpdb):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.qualify import qualify_business
    from test_v02 import biz, second_customer
    db.upsert_business(biz())
    db.save_analysis(1, {"website_status": "ok", "findings": [f("site_ssl")]})
    qualify_business(1)
    c = TestClient(app)
    defaults = c.get("/api/settings/sender").json()
    assert (defaults["name"], defaults["company"]) == ("Patrick", "Atomic Consulting and Solutions")   # profile defaults
    c.post("/api/settings/sender", json=SENDER)
    d = c.get("/api/leads/1/email?variant=detailed").json()
    assert d["variant"] == "detailed" and "I'm Pat Example with" in d["body"]
    db.sync_customer(second_customer())
    assert c.get("/api/leads/1/email?customer=poolco").status_code == 404          # customer isolation
    assert c.get("/api/settings/sender?customer=poolco").json()["name"] != "Pat Example"   # saved settings are per customer


def test_emails_hint_without_giving_away_specifics():
    d = draft(lead_with([f("no_https"), f("broken_important_link", details={"link_label": "x"})]), SENDER, "detailed")
    assert "security setting that causes browsers to flag your site" in d["body"]
    assert "I also noticed a few things on your site that aren't working" in d["body"]
    for detail in ("http://", "Not secure", "HTTP 4", "/contact", "unencrypted"):
        assert detail not in d["body"]
    assert "walk you through what I found" in d["body"]
    assert "A question about site security on acme.example" in d["subjects"]


def test_every_finding_with_a_talking_point_has_a_teaser():
    from app.outreach import HINTS
    missing = [c for c, e in CATALOG.items() if e.get("talk") and c not in HINTS and c != "new_registration"]
    assert missing == []
