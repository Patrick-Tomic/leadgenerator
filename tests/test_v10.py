"""v0.10: Brave website search is capped and on-demand by default (Brave has no free plan)."""
import asyncio

import httpx
import pytest

from app import config, db
from app.discovery.base import SourceError
from app.discovery.brave import BraveWebsiteFinder, get_finders


@pytest.fixture
def tmpdb(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "backups")
    db.use(tmp_path / "leads.db")
    yield
    db._conn.close()
    db._conn = None


def brave(calls):
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"web": {"results": [{"url": "https://www.yelp.com/biz/x"}]}})
    return BraveWebsiteFinder(transport=httpx.MockTransport(handler), min_interval=0)


PROFILE = {"name": "Acme", "names": ["Acme"], "phones": ["8135550101"], "cities": ["Tampa"], "city": "Tampa"}


def test_monthly_cap_stops_searches_before_sending(tmpdb, monkeypatch):
    monkeypatch.setattr(config, "BRAVE_API_KEY", "k")
    monkeypatch.setattr(config, "BRAVE_MONTHLY_LIMIT", 3)
    calls = []
    asyncio.run(brave(calls).find_candidates(PROFILE))              # 2 queries (name+city, phone)
    with pytest.raises(SourceError, match="monthly search limit"):
        asyncio.run(brave(calls).find_candidates(PROFILE))          # 3rd allowed, 4th refused
    assert len(calls) == 3


def test_background_checks_never_search_unless_enabled(monkeypatch):
    monkeypatch.setattr(config, "BRAVE_API_KEY", "k")
    monkeypatch.setattr(config, "GOOGLE_PLACES_API_KEY", "")
    monkeypatch.setattr(config, "WEBSITE_SEARCH_AUTO", False)
    assert get_finders(background=True) == []
    assert [f.name for f in get_finders(background=False)] == ["search:brave"]
    monkeypatch.setattr(config, "WEBSITE_SEARCH_AUTO", True)
    assert [f.name for f in get_finders(background=True)] == ["search:brave"]


def test_find_website_button(tmpdb, monkeypatch):
    from fastapi.testclient import TestClient
    from app import main
    from test_v04 import FakeFinder, osm
    b = osm(1)
    db.upsert_business(b)
    c = TestClient(main.app)
    monkeypatch.setattr(main, "get_finders", lambda background=False: [])
    assert c.post("/api/no-web/1/search").status_code == 400                      # no key configured
    monkeypatch.setattr(main, "get_finders", lambda background=False: [FakeFinder("search:brave")])
    r = c.post("/api/no-web/1/search").json()
    assert (r["website_state"], r["label"]) == ("none_found", "No website found (searched)")
