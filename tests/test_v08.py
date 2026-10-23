"""v0.8: favor lesser-known businesses (free estimate + optional Google review counts)."""
import functools
import json
from datetime import date, timedelta

import httpx
import pytest

from app import config, db, reviews
from app.scoring import score_lead
from app.visibility import estimate


# ---------------------------------------------------------------- free estimate

def test_wikidata_means_well_known():
    v = estimate({"raw": {"tags": {"wikidata": "Q1"}}}, {}, [])
    assert v["level"] == "high" and "Wikipedia" in v["reasons"][0]


def test_review_widget_and_socials_mean_well_known():
    v = estimate({}, {"review_widgets": ["Birdeye"], "social_links": ["Facebook", "Instagram", "Yelp"], "internal_link_count": 12}, [])
    assert v["level"] == "high"


def test_new_small_business_is_not_well_known():
    recent = (date.today() - timedelta(days=30)).isoformat()
    v = estimate({"website_state": "unknown"}, {}, [{"fields": {"filing_date": recent}}])
    assert v["level"] == "low" and any("last year" in r for r in v["reasons"]) and "No website found" in v["reasons"]
    v = estimate({}, {"internal_link_count": 4, "social_links": []}, [])
    assert v["level"] == "low" and set(v["reasons"]) == {"Small website", "No social profiles linked"}


def test_no_information_is_unknown_and_neutral():
    assert estimate({}, {}, [])["level"] == "unknown"


def test_well_known_businesses_score_lower():
    finding = [{"code": "site_ssl", "title": "t", "evidence": "e", "severity": "high", "confidence": "high"}]
    b = {"name": "X", "industry": "plumbing", "phone": "1"}
    low = score_lead(b, finding, facts={"visibility": {"level": "low"}})
    high = score_lead(b, finding, facts={"visibility": {"level": "high"}})
    assert high["score"] < low["score"] and low["explanation"]["visibility"]["level"] == "low"


# ---------------------------------------------------------------- Google reviews (representative Places responses)

@pytest.fixture
def tmpdb(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "backups")
    db.use(tmp_path / "leads.db")
    reviews._cache.clear()
    yield
    db._conn.close()
    db._conn = None


def place(pid, name, count, phone="(813) 555-0101"):
    return {"id": pid, "displayName": {"text": name}, "formattedAddress": "12 Main St, Tampa, FL",
            "location": {"latitude": 27.95, "longitude": -82.46}, "nationalPhoneNumber": phone,
            "rating": 4.7, "userRatingCount": count}


def setup_leads():
    from app.qualify import qualify_business
    from test_v04 import osm
    db.upsert_business(osm(1, tags={"name": "Famous Plumbing", "phone": "813-555-0101", "website": "famous.example"}))
    db.upsert_business(osm(2, tags={"name": "Tiny Plumbing", "phone": "727-555-0199", "website": "tiny.example"}, lat=28.3, lon=-82.7))
    for lid in (1, 2):
        db.save_analysis(lid, {"website_status": "ok", "findings": [
            {"code": "site_ssl", "title": "t", "evidence": "e", "severity": "high", "confidence": "high"},
            {"code": "_facts", "facts": {"website_state": "verified"}}]})
        qualify_business(lid)
    db.conn().execute("UPDATE customer_leads SET score = CASE lead_id WHEN 1 THEN 90 ELSE 80 END")   # Famous first
    db.conn().commit()


def mock(handler, calls):
    def wrapped(request):
        calls.append(request)
        return handler(request)
    return httpx.MockTransport(wrapped)


def test_next_lead_skips_businesses_with_many_reviews(tmpdb, monkeypatch):
    from fastapi.testclient import TestClient
    from app import pipeline
    from app.main import app
    setup_leads()
    monkeypatch.setattr(config, "GOOGLE_PLACES_API_KEY", "k")
    monkeypatch.setattr(pipeline, "start_refill", lambda *a, **k: False)
    calls = []

    def handler(request):
        body = json.loads(request.content)
        if "Famous" in body["textQuery"]:
            return httpx.Response(200, json={"places": [place("P1", "Famous Plumbing", 640)]})
        return httpx.Response(200, json={"places": [place("P2", "Tiny Plumbing", 7, phone="(727) 555-0199")]})
    monkeypatch.setattr(reviews, "check", functools.partial(reviews.check, transport=mock(handler, calls)))
    r = TestClient(app).get("/api/next").json()
    assert r["lead"]["name"] == "Tiny Plumbing" and r["skipped_well_known"] == ["Famous Plumbing"]
    assert r["lead"]["google_reviews"]["count"] == 7 and r["lead"]["google_reviews"]["source"] == "Google"
    # Only place IDs are stored; counts and ratings are not.
    stored = json.dumps([dict(x) for x in db.conn().execute("SELECT * FROM lead_sources WHERE source='google_places'")])
    assert "P1" in stored and "P2" in stored and "640" not in stored and "4.7" not in stored
    n = len(calls)
    TestClient(app).get("/api/next")
    assert len(calls) == n                                   # cached in memory, no new calls


def test_name_only_place_match_is_not_used(tmpdb, monkeypatch):
    setup_leads()
    monkeypatch.setattr(config, "GOOGLE_PLACES_API_KEY", "k")
    other = place("PX", "Famous Plumbing", 900, phone="(305) 000-0000")
    other["location"] = {"latitude": 25.7, "longitude": -80.2}
    other["formattedAddress"] = "Miami, FL"
    import asyncio
    r = asyncio.run(reviews.check(db.get_business(1), transport=mock(lambda req: httpx.Response(200, json={"places": [other]}), [])))
    assert r["found"] is False                               # same name, different city and phone -> not theirs


def test_review_check_failures_never_block_leads(tmpdb, monkeypatch):
    from fastapi.testclient import TestClient
    from app import pipeline
    from app.main import app
    setup_leads()
    monkeypatch.setattr(config, "GOOGLE_PLACES_API_KEY", "k")
    monkeypatch.setattr(pipeline, "start_refill", lambda *a, **k: False)
    monkeypatch.setattr(reviews, "check", functools.partial(reviews.check, transport=mock(
        lambda r: httpx.Response(403, json={"error": {"status": "PERMISSION_DENIED", "message": "billing"}}), [])))
    assert TestClient(app).get("/api/next").json()["lead"]["name"] == "Famous Plumbing"   # shown, unchecked
    monkeypatch.setattr(config, "GOOGLE_PLACES_MONTHLY_LIMIT", 0)
    reviews._cache.clear()
    assert TestClient(app).get("/api/next").json()["lead"]["name"] == "Famous Plumbing"


def test_reviews_off_without_key(tmpdb, monkeypatch):
    import asyncio
    monkeypatch.setattr(config, "GOOGLE_PLACES_API_KEY", "")
    setup_leads()
    assert asyncio.run(reviews.check(db.get_business(1))) is None


def test_scored_leads_are_rechecked_before_new_businesses(tmpdb):
    from test_v04 import osm
    setup_leads()                                             # leads 1-2 checked and scored 90/80
    db.upsert_business(osm(3, tags={"name": "Brand New Co", "phone": "4075550000", "website": "new.example"}, lat=28.5))
    db.conn().execute("UPDATE leads SET audit_version=1 WHERE id IN (1, 2)")   # checked by an older version
    assert [b["id"] for b in db.unanalyzed(3)][:2] == [1, 2] or [b["id"] for b in db.unanalyzed(3)][:2] == [2, 1]
