"""v0.13: "Emailed, call later" status and the Call later list."""
from datetime import date, timedelta

import pytest

from app import config, db


@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.qualify import qualify_business
    from test_v02 import biz
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "backups")
    db.use(tmp_path / "leads.db")
    for i, name in enumerate(["Ace Plumbing", "Bay Roofing", "Coast HVAC"], 1):
        db.upsert_business(biz(source_id=f"n/{i}", name=name, phone=f"81355501{i:02}", website=f"https://{i}.example",
                               lat=27.95 + i / 100))
        db.save_analysis(i, {"website_status": "ok", "findings": [
            {"code": "site_ssl", "title": "t", "evidence": "e", "severity": "high", "confidence": "high"}]})
        qualify_business(i)
    db.conn().execute("UPDATE customer_leads SET score=90")
    db.conn().commit()
    yield TestClient(app)
    db._conn.close()
    db._conn = None


def test_mark_emailed_sets_dates_and_lists_by_call_back_date(client):
    r = client.post("/api/leads/1", json={"status": "call_later", "follow_up_days": 5, "notes": "sent intro"}).json()
    assert r["status"] == "call_later" and r["emailed_at"] and r["notes"] == "sent intro"
    assert r["follow_up_at"] == (date.today() + timedelta(days=5)).isoformat()
    client.post("/api/leads/2", json={"status": "call_later"})                       # default: 3 days
    assert db.get_lead(1, 2)["follow_up_at"] == (date.today() + timedelta(days=3)).isoformat()
    rows = client.get("/api/call-later").json()["rows"]
    assert [x["name"] for x in rows] == ["Bay Roofing", "Ace Plumbing"]              # soonest call-back first
    assert rows[0]["phone"] and rows[1]["notes"] == "sent intro"


def test_due_and_overdue_flags(client):
    client.post("/api/leads/1", json={"status": "call_later", "follow_up_days": 0})
    client.post("/api/leads/2", json={"status": "call_later", "follow_up_days": 7})
    db.conn().execute("UPDATE customer_leads SET follow_up_at=? WHERE lead_id=3", ((date.today() - timedelta(days=2)).isoformat(),))
    client.post("/api/leads/3", json={"notes": "x"})
    db.conn().execute("UPDATE customer_leads SET status='call_later', follow_up_at=? WHERE lead_id=3",
                      ((date.today() - timedelta(days=2)).isoformat(),))
    db.conn().commit()
    d = client.get("/api/call-later").json()
    flags = {r["name"]: (r["due"], r["overdue"]) for r in d["rows"]}
    assert flags == {"Coast HVAC": (True, True), "Ace Plumbing": (True, False), "Bay Roofing": (False, False)}
    assert d["due_count"] == 2


def test_called_later_leads_leave_next_lead_and_isolated(client, monkeypatch):
    from app import pipeline
    monkeypatch.setattr(pipeline, "start_refill", lambda *a, **k: False)
    client.post("/api/leads/1", json={"status": "call_later"})
    assert client.get("/api/next").json()["lead"]["id"] != 1
    from test_v02 import second_customer
    db.sync_customer(second_customer(areas=["hillsborough"]))
    assert client.get("/api/call-later?customer=poolco").json()["rows"] == []
    client.post("/api/leads/1", json={"status": "contacted"})                        # after the call
    assert client.get("/api/call-later").json()["rows"] == []
