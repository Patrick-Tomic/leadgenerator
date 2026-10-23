"""v0.3: evidence-based findings and scoring."""
import asyncio
import csv
import io

import pytest

from app import config, db
from app.analysis import website
from app.analysis.fetcher import FetchResult
from app.findings import CATALOG, enrich
from app.profiles import default_profile, validate
from app.qualify import SCORING_VERSION, qualify_business, sync_profiles
from app.scoring import classify_finding, score_lead

HOME = "https://acme.example/"


def f(code, confidence="high", severity="high", **kw):
    return {"code": code, "title": code, "evidence": f"evidence for {code}", "severity": severity, "confidence": confidence, **kw}


BIZ = {"name": "Acme Plumbing", "industry": "plumbing", "phone": "813-555-0101"}
OPTIONAL = ["no_chat", "no_booking", "no_schema", "no_sitemap", "img_alt", "no_meta_description", "weak_title", "no_h1",
            "old_jquery", "legacy_markup"]


# ---------------------------------------------------------------- scoring

def test_one_verified_strong_problem_beats_many_optional_and_minor_ones():
    strong = score_lead(BIZ, [f("broken_important_link")])
    many = score_lead(BIZ, [f(c, "medium", "low") for c in OPTIONAL] + [f("stale_copyright", "medium"), f("no_title"),
                                                                         f("no_contact_form", "medium")])
    assert strong["score"] > many["score"] * 2
    assert strong["grade"] in ("A", "B")


def test_missing_optional_features_are_not_evidence():
    r = score_lead(BIZ, [f(c, "high", "low") for c in OPTIONAL])
    assert r["score"] <= default_profile()["scoring"]["optional_cap"]
    assert r["grade"] == "D"
    assert "optional" in r["explanation"]["uncertainty"].lower()
    assert all(classify_finding(f(c)) == "potential" for c in OPTIONAL)
    assert all(CATALOG[c]["tier"] == "optional" for c in OPTIONAL)


def test_issue_count_is_not_rewarded_beyond_the_top_three():
    three = [f("no_viewport"), f("no_click_to_call"), f("thin_content")]
    more = three + [f("slow"), f("flash"), f("frames"), f("mailto_form"), f("phone_mismatch")]
    # Same top three values, so extra findings of equal strength add nothing.
    assert score_lead(BIZ, more)["score"] == score_lead(BIZ, three)["score"]


@pytest.mark.parametrize("code", ["site_timeout", "broken_important_link", "no_viewport"])
def test_confidence_levels_order_scores(code):
    v, p, u = (score_lead(BIZ, [f(code, c)])["score"] for c in ("high", "medium", "low"))
    assert v > p > u
    assert enrich(f(code, "high"))["confidence_level"] == "verified"
    assert enrich(f(code, "medium"))["confidence_level"] == "probable"
    assert enrich(f(code, "low"))["confidence_level"] == "unverified"


def test_unverified_no_website_ranks_below_verified_problems():
    unverified = score_lead(BIZ, [f("no_website_unverified", "low")])
    assert unverified["score"] < score_lead(BIZ, [f("no_viewport")])["score"]
    assert unverified["score"] < score_lead(BIZ, [f("no_website_confirmed")])["score"]
    # v0.4: a missing listing field is not evidence at all; it's shown and never scored.
    assert unverified["score"] == 0 and unverified["opportunities"] == []
    assert any("Search for the business" in v for v in unverified["explanation"]["verify"])


def test_dead_domain_is_discounted_for_actionability():
    assert score_lead(BIZ, [f("site_dns")])["score"] < score_lead(BIZ, [f("site_timeout")])["score"]


def test_irrelevant_findings_do_not_score():
    p = validate({**default_profile(), "offerings": {"seo": {"label": "SEO", "codes": ["no_title"], "pitch": "p"}}})
    r = score_lead(BIZ, [f("site_timeout"), f("no_title", "high", "medium")], p)
    assert [o["key"] for o in r["opportunities"]] == ["seo"]
    assert r["explanation"]["strongest"]["code"] == "no_title"


def test_contact_verification_affects_score():
    finding_ = [f("broken_important_link")]
    verified = score_lead(BIZ, finding_, facts={"contact_corroborated": True})["score"]
    listed = score_lead(BIZ, finding_)["score"]
    none = score_lead({"name": "X", "industry": "plumbing"}, finding_)["score"]
    assert verified > listed > none
    assert score_lead({"name": "X", "industry": "plumbing"}, finding_)["explanation"]["contact"] == "none"


def test_explanation_names_strongest_evidence_and_uncertainty():
    r = score_lead(BIZ, [f("no_chat", "medium", "low"), f("phone_mismatch", "medium", details={"listing_phone": "(813) 555-0101",
                                                                                                 "site_phone": "(813) 555-0199"}),
                         f("form_untested", "high", "low")])
    ex = r["explanation"]
    assert ex["strongest"]["code"] == "phone_mismatch"
    assert ex["strongest"]["confidence_level"] == "probable"
    assert "(813) 555-0199" in ex["strongest"]["talking_point"]
    assert "which number is current" in ex["uncertainty"]
    assert any("never submit forms" in v for v in ex["verify"])
    assert ex["text"].startswith(f"Score {r['score']} rests mainly on")


def test_talking_points_make_no_revenue_claims():
    for code, e in CATALOG.items():
        text = " ".join(filter(None, [e.get("talk"), e.get("impact")])).lower()
        for banned in ("losing", "lost revenue", "lost customers", "competitor"):
            assert banned not in text, (code, banned)


def test_legacy_profile_scoring_keys_still_load():
    p = default_profile()
    legacy = validate({**p, "scoring": {"severity_points": {"high": 14}, "confidence_mult": {"high": 1.0, "medium": 0.5, "low": 0.1},
                                        "contact_bonus": {"phone": 5}}})
    assert legacy["scoring"]["confidence_mult"] == {"verified": 1.0, "probable": 0.5, "unverified": 0.1}
    assert "severity_points" not in legacy["scoring"]


# ---------------------------------------------------------------- audit (no network: scripted fetcher)

class FakeFetcher:
    """Serves scripted responses. pages: url -> list of FetchResult (one per call; last repeats).
    links: url -> list of (status, err) for link_status calls."""

    def __init__(self, pages, links=None):
        self.pages, self.links, self._robots, self.calls = pages, links or {}, {}, []

    async def get(self, url, check_robots=True):
        self.calls.append(("get", url))
        seq = self.pages.get(url) or [FetchResult(url=url, ok=False, error="dns", error_detail="Name or service not known")]
        return seq.pop(0) if len(seq) > 1 else seq[0]

    async def link_status(self, url):
        self.calls.append(("link", url))
        seq = self.links.get(url, [(200, None)])
        return seq.pop(0) if len(seq) > 1 else seq[0]

    async def head_ok(self, url):
        return 200


def ok(url, html, elapsed=0.3):
    return FetchResult(url=url, ok=True, status=200, final_url=url, html=html, elapsed=elapsed)


def page(body, head='<meta name="viewport" content="width=device-width"><title>Acme Plumbing | Tampa plumber</title>'):
    return f"<html><head>{head}</head><body><h1>Acme</h1>{body}<p>{'Licensed plumbing in Tampa. ' * 30}</p></body></html>"


def run(fetcher, listing_phone=None):
    return asyncio.run(website.analyze_site(HOME, fetcher, listing_phone=listing_phone))


def codes(result):
    return {x["code"]: x for x in result["findings"]}


@pytest.fixture(autouse=True)
def no_retry_wait(monkeypatch):
    monkeypatch.setattr(website, "RETRY_DELAY", 0)


def test_failed_website_is_verified_only_after_retry():
    timeout = FetchResult(url=HOME, ok=False, error="timeout", error_detail="No response within 15s")
    r = run(FakeFetcher({HOME: [timeout]}))
    assert r["website_status"] == "down"
    fd = codes(r)["site_timeout"]
    assert fd["confidence"] == "high" and "2 attempts" in fd["evidence"]


def test_intermittent_failure_is_probable_and_site_still_audited():
    timeout = FetchResult(url=HOME, ok=False, error="timeout", error_detail="No response within 15s")
    r = run(FakeFetcher({HOME: [timeout, ok(HOME, page('<a href="tel:8135550101">Call (813) 555-0101</a>'))]}))
    assert r["website_status"] == "ok"
    assert codes(r)["site_intermittent"]["confidence"] == "medium"
    assert "site_timeout" not in codes(r)


def test_bot_protection_is_not_scored_as_down():
    blocked = FetchResult(url=HOME, ok=False, status=403, error="http_error", error_detail="HTTP 403")
    r = run(FakeFetcher({HOME: [blocked]}))
    assert r["website_status"] == "restricted"
    assert classify_finding(r["findings"][0]) == "unavailable"
    assert score_lead(BIZ, r["findings"])["score"] == 0


def test_broken_links_are_classified_by_status_and_importance():
    html = page('<a href="tel:8135550101">(813) 555-0101</a><a href="/contact">Contact us</a><a href="/about">About</a>'
                '<a href="/gallery">Gallery</a><a href="/team">Team</a><a href="/blog">Blog</a><form><textarea></textarea></form>')
    fetcher = FakeFetcher({HOME: [ok(HOME, html)]}, links={
        "https://acme.example/contact": [(404, None)],
        "https://acme.example/about": [(500, None), (500, None)],
        "https://acme.example/gallery": [(403, None)],
        "https://acme.example/team": [(None, "timeout")],
    })
    r = run(fetcher)
    c = codes(r)
    imp = c["broken_important_link"]
    assert imp["confidence"] == "high" and imp["page"] == "https://acme.example/contact" and "HTTP 404" in imp["evidence"]
    other = c["broken_links"]
    assert "about" in other["evidence"] and "gallery" not in other["evidence"]  # 403 is not "broken"
    assert other["confidence"] == "medium"  # a 5xx (even confirmed twice) is probable, not verified
    assert any("team" in u for u in r["facts"]["links_unchecked"])
    assert ("link", "https://acme.example/contact") == next(x for x in fetcher.calls if x[0] == "link")  # important first


def test_forms_are_never_claimed_to_fail_untested():
    html = page('<a href="tel:8135550101">(813) 555-0101</a><form action="mailto:x@acme.example"><input name=name><textarea></textarea></form>')
    r = run(FakeFetcher({HOME: [ok(HOME, html)]}))
    c = codes(r)
    assert classify_finding(c["form_untested"]) == "info"
    assert "never submit" in c["form_untested"]["evidence"]
    assert "not submitted" in c["mailto_form"]["evidence"].lower()
    for x in r["findings"]:
        assert "fails" not in x["evidence"].lower() or x["code"].startswith("site_")
    assert "never submit forms" in enrich(c["form_untested"])["verify"]


def test_no_contact_path_vs_missing_form():
    nothing = run(FakeFetcher({HOME: [ok(HOME, page("<p>We fix pipes.</p>"))]}))
    assert codes(nothing)["no_contact_path"]["confidence"] == "high"
    phone_only = run(FakeFetcher({HOME: [ok(HOME, page('<a href="tel:8135550101">(813) 555-0101</a>'))]}))
    c = codes(phone_only)
    assert "no_contact_path" not in c and c["no_contact_form"]["severity"] == "low"
    assert score_lead(BIZ, nothing["findings"])["score"] > score_lead(BIZ, phone_only["findings"])["score"]


def test_phone_mismatch_and_corroboration():
    html = page('<a href="tel:8135550199">(813) 555-0199</a><form><textarea></textarea></form>')
    mismatch = run(FakeFetcher({HOME: [ok(HOME, html)]}), listing_phone="+1 813-555-0101")
    pm = codes(mismatch)["phone_mismatch"]
    assert pm["confidence"] == "medium" and pm["details"]["site_phone"] == "(813) 555-0199"
    assert mismatch["facts"]["contact_corroborated"] is False
    match = run(FakeFetcher({HOME: [ok(HOME, html)]}), listing_phone="(813) 555-0199")
    assert "phone_mismatch" not in codes(match) and match["facts"]["contact_corroborated"] is True


def test_online_booking_claim_without_any_path():
    claim = page('<a href="tel:8135550101">(813) 555-0101</a><p>Book online in seconds!</p>')
    r = run(FakeFetcher({HOME: [ok(HOME, claim)]}))
    assert codes(r)["claimed_feature_missing"]["confidence"] == "medium"
    with_button = page('<a href="tel:8135550101">(813) 555-0101</a><p>Book online in seconds!</p><a href="/book">Book online</a>')
    assert "claimed_feature_missing" not in codes(run(FakeFetcher({HOME: [ok(HOME, with_button)]})))
    phone_quote = page('<a href="tel:8135550101">Call (813) 555-0101 to request a quote</a>')
    assert "claimed_feature_missing" not in codes(run(FakeFetcher({HOME: [ok(HOME, phone_quote)]})))


def test_javascript_rendered_pages_downgrade_uncertain_findings():
    shell = '<html><head></head><body><div id="root"></div>' + '<script src="/a.js"></script>' * 3 + "</body></html>"
    r = run(FakeFetcher({HOME: [ok(HOME, shell)]}))
    c = codes(r)
    assert c["no_contact_path"]["confidence"] == "low"
    assert classify_finding(c["no_contact_path"]) == "potential"
    assert "verify in a browser" in c["no_contact_path"]["evidence"]


def test_optional_findings_are_labeled_optional_in_evidence():
    r = run(FakeFetcher({HOME: [ok(HOME, page('<a href="tel:8135550101">(813) 555-0101</a>'))]}))
    for code in ("no_chat", "no_booking"):
        assert "Optional feature" in codes(r)[code]["evidence"]
        assert enrich(codes(r)[code])["tier"] == "optional"


# ---------------------------------------------------------------- persistence & workflow

@pytest.fixture
def tmpdb(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "backups")
    db.use(tmp_path / "leads.db")
    yield
    db._conn.close()
    db._conn = None


def test_rescoring_preserves_status_notes_and_csv_columns(tmpdb):
    from fastapi.testclient import TestClient
    from app.main import app
    db.upsert_business({"source": "osm", "source_id": "n/1", "name": "Acme Plumbing", "industry": "plumbing", "address": "1 Main St",
                        "city": "Tampa", "lat": 27.95, "lon": -82.46, "phone": "8135550101", "email": None,
                        "website": HOME, "raw": {"tags": {}}})
    db.save_analysis(1, {"website_status": "ok", "findings": [f("no_chat", "medium", "low"), f("broken_important_link", page=HOME + "contact")]})
    qualify_business(1)
    cust = db.get_customer("atomic")
    db.update_lead(cust["id"], 1, status="saved", notes="call Tuesday")
    db.conn().execute("UPDATE customers SET scoring_version=0")  # as if scored by the previous version
    db.conn().commit()
    sync_profiles()
    lead = db.get_lead(cust["id"], 1)
    assert (lead["status"], lead["notes"]) == ("saved", "call Tuesday")
    assert db.get_customer("atomic")["scoring_version"] == SCORING_VERSION
    assert lead["explanation"]["strongest"]["code"] == "broken_important_link"

    rows = list(csv.reader(io.StringIO(TestClient(app).get("/api/export.csv").text)))
    assert rows[0][:13] == ["score", "grade", "status", "name", "industry", "phone", "email", "website", "address",
                            "top_opportunities", "opportunity_basis", "summary", "notes"]
    record = dict(zip(rows[0], rows[1]))
    assert record["strongest_issue"] == "broken_important_link" and record["confidence"] == "verified"
    assert record["affected_page"] == HOME + "contact" and record["status"] == "saved"
    assert "never submit" not in record["verify_manually"]  # no form finding on this lead


def test_stale_audits_are_queued_for_recheck(tmpdb):
    db.upsert_business({"source": "osm", "source_id": "n/1", "name": "Acme", "industry": "plumbing", "address": None, "city": "Tampa",
                        "lat": 27.95, "lon": -82.46, "phone": None, "email": None, "website": HOME, "raw": {}})
    db.save_analysis(1, {"website_status": "ok", "findings": []})
    assert db.unanalyzed(5) == []
    db.conn().execute("UPDATE leads SET audit_version=1")
    assert [b["id"] for b in db.unanalyzed(5)] == [1] and db.stale_audits() == 1


def test_manual_website_correction(tmpdb, monkeypatch):
    from fastapi.testclient import TestClient
    from app import pipeline
    from app.main import app
    db.upsert_business({"source": "osm", "source_id": "n/14", "name": "Velocity Air Conditioning", "industry": "hvac",
                        "address": "56th Ct N, Clearwater", "city": None, "lat": 27.9, "lon": -82.7, "phone": None, "email": None,
                        "website": None, "raw": {"tags": {}}})
    db.save_analysis(1, {"website_status": "unverified", "findings": [f("no_website_unverified", "low")]})
    qualify_business(1)
    cust = db.get_customer("atomic")
    db.update_lead(cust["id"], 1, status="saved", notes="has a site per owner")

    audited = []

    async def fake_audit(b, fetcher, finders=None):
        audited.append(b["website"])
        return {"website_status": "ok", "final_url": b["website"], "findings": [f("no_chat", "medium", "low")]}
    monkeypatch.setattr(pipeline, "analyze_business", fake_audit)

    client = TestClient(app)
    assert client.post("/api/leads/1/website", json={"website": "not a url"}).status_code == 400
    r = client.post("/api/leads/1/website", json={"website": "velocityac.example"})
    assert r.status_code == 200
    lead = r.json()
    assert audited == ["http://velocityac.example"]
    assert lead["website"] == "http://velocityac.example" and lead["website_status"] == "ok"
    assert "no_website_unverified" not in {x["code"] for x in lead["findings"]}
    assert (lead["status"], lead["notes"]) == ("saved", "has a site per owner")
    assert db.conn().execute("SELECT source FROM lead_sources WHERE lead_id=1 ORDER BY id").fetchall()[-1][0] == "manual"
    # Later discovery of the same domain merges into this business instead of duplicating it.
    assert db.upsert_business({"source": "osm", "source_id": "n/99", "name": "Velocity AC", "industry": "hvac", "address": None,
                               "city": None, "lat": 28.5, "lon": -82.0, "phone": None, "email": None,
                               "website": "https://www.velocityac.example/", "raw": {}}) == "merged"
