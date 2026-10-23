"""v0.9: "No web presence" tab."""
import functools
import json

import httpx
import pytest

from app import config, db, noweb, reviews


@pytest.fixture
def tmpdb(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "backups")
    db.use(tmp_path / "leads.db")
    reviews._cache.clear()
    yield
    db._conn.close()
    db._conn = None


def add(source_id, name, industry="restaurant", area="us-fl", **tags):
    from app.discovery.osm import element_to_business
    el = {"type": "node", "id": source_id, "lat": 27.95 + source_id / 1000, "lon": -82.46,
          "tags": {"name": name, "amenity": "restaurant", "addr:city": "Tampa", **tags}}
    db.upsert_business(element_to_business(el, industry), area=area)
    return db.conn().execute("SELECT lead_id FROM lead_sources WHERE source_id=?", (f"node/{source_id}",)).fetchone()[0]


def customer():
    return db.get_customer("atomic")


def test_lists_only_businesses_without_any_website(tmpdb):
    a = add(1, "No Site Grill", phone="813-555-0101")
    add(2, "Has Site Cafe", website="https://hassite.example")
    c = add(3, "Verified Elsewhere Diner")
    db.save_website_state(c, "verified", "https://found.example", False)      # found later -> not "no web"
    d = add(4, "Georgia Grill", area="us-ga")
    out = noweb.query(customer())
    assert {r["name"] for r in out["rows"]} == {"No Site Grill", "Georgia Grill"}
    assert out["rows"][0]["website_label"] == "No website listed (not confirmed)"
    assert out["rows"][0]["google_label"] == "Not checked"
    assert {r["name"] for r in noweb.query(customer(), area="us-ga")["rows"]} == {"Georgia Grill"}
    assert noweb.query(customer(), industry="plumbing")["total"] == 0


def test_ordering_phone_first_then_no_google_listing(tmpdb):
    no_phone = add(1, "Quiet Cafe")
    unchecked = add(2, "Unchecked Grill", phone="813-555-0102")
    no_google = add(3, "Offline Diner", phone="813-555-0103")
    has_google = add(4, "Listed Bistro", phone="813-555-0104")
    db.conn().execute("UPDATE leads SET google_listing=0 WHERE id=?", (no_google,))
    db.conn().execute("UPDATE leads SET google_listing=1 WHERE id=?", (has_google,))
    db.conn().commit()
    assert [r["name"] for r in noweb.query(customer())["rows"]] == ["Offline Diner", "Unchecked Grill", "Listed Bistro", "Quiet Cafe"]
    assert [r["name"] for r in noweb.query(customer(), google="not_found")["rows"]] == ["Offline Diner"]


def test_google_check_stores_only_the_outcome(tmpdb, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    found = add(1, "Listed Grill", phone="813-555-0101")
    missing = add(2, "Offline Grill", phone="813-555-0199")
    c = TestClient(app)
    monkeypatch.setattr(config, "GOOGLE_PLACES_API_KEY", "")
    assert c.post(f"/api/no-web/{found}/google").status_code == 400            # needs a key
    monkeypatch.setattr(config, "GOOGLE_PLACES_API_KEY", "k")

    def handler(request):
        body = json.loads(request.content)
        if "Listed" in body["textQuery"]:
            return httpx.Response(200, json={"places": [{"id": "PX", "displayName": {"text": "Listed Grill"},
                                                          "nationalPhoneNumber": "(813) 555-0101", "userRatingCount": 12, "rating": 4.1}]})
        return httpx.Response(200, json={"places": []})
    monkeypatch.setattr(reviews, "check", functools.partial(reviews.check, transport=httpx.MockTransport(handler)))
    assert c.post(f"/api/no-web/{found}/google").json()["found"] is True
    assert c.post(f"/api/no-web/{missing}/google").json()["google_label"] == "No Google listing found"
    rows = dict(db.conn().execute("SELECT id, google_listing FROM leads").fetchall())
    assert rows == {found: 1, missing: 0}
    dump = json.dumps([list(r) for r in db.conn().execute("SELECT * FROM leads")])
    assert "4.1" not in dump and '"12"' not in dump                            # no Google content stored


def test_open_checks_and_scores_the_business(tmpdb):
    from fastapi.testclient import TestClient
    from app.main import app
    lead_id = add(1, "No Site Grill", phone="813-555-0101")
    assert db.get_business(lead_id)["analyzed_at"] is None
    lead = TestClient(app).post(f"/api/no-web/{lead_id}/open").json()
    assert lead["id"] == lead_id and lead["website_state"] == "unknown" and lead["status"] == "new"
    outside = add(2, "Texas Grill", area="us-tx-not-ours")
    assert TestClient(app).post(f"/api/no-web/{outside}/open").status_code == 409
