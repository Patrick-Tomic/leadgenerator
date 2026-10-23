"""v0.11: free website finding without a card: domain guessing (keyless) and Tavily (free tier)."""
import asyncio

import httpx
import pytest

from app import config, db
from app.discovery import domain_guess
from app.discovery.base import SourceError
from app.discovery.brave import TavilyWebsiteFinder, get_finders
from app.discovery.domain_guess import DomainGuessFinder, guesses


@pytest.fixture
def tmpdb(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "backups")
    from app.analysis import website
    monkeypatch.setattr(website, "RETRY_DELAY", 0)
    db.use(tmp_path / "leads.db")
    yield
    db._conn.close()
    db._conn = None


def test_guesses_from_name_and_alt_names():
    g = guesses({"name": "Joe's Grill & Bar LLC", "names": ["Joe's Grill & Bar LLC", "Joe's"], "city": "Tampa"})
    assert g[:2] == ["joesgrillbar.com", "joesgrillbar.net"] and "joesgrillbartampa.com" in g and "joes.com" in g


def fake_dns(existing):
    async def resolves(domain):
        return domain in existing
    return resolves


def test_guessed_site_accepted_only_when_page_identifies_the_business(tmpdb, monkeypatch):
    from app.pipeline import analyze_business
    from test_v04 import FakeFetcher, ok, osm
    db.upsert_business(osm(1, tags={"name": "Acme Plumbing"}))
    monkeypatch.setattr(domain_guess, "_resolves", fake_dns({"acmeplumbing.com", "acmeplumbing.net"}))
    pages = {"https://acmeplumbing.com/": ok("https://acmeplumbing.com/", "<p>Acme Plumbing of Ohio. Call 216-555-0000</p>"),
             "https://acmeplumbing.net/": ok("https://acmeplumbing.net/", '<a href="tel:8135550101">(813) 555-0101</a> Acme Plumbing')}
    r = asyncio.run(analyze_business(db.get_business(1), FakeFetcher(pages), finders=[DomainGuessFinder()]))
    b = db.get_business(1)
    assert b["website_state"] == "verified" and b["website"] == "https://acmeplumbing.net/"
    assert [c["url"] for c in db.website_candidates(1)] == ["https://acmeplumbing.net/"]   # the Ohio site isn't kept


def test_guessing_nothing_never_means_no_website(tmpdb, monkeypatch):
    from app.pipeline import analyze_business
    from test_v04 import FakeFetcher, osm
    db.upsert_business(osm(1, tags={"name": "Acme Plumbing"}))
    monkeypatch.setattr(domain_guess, "_resolves", fake_dns(set()))
    r = asyncio.run(analyze_business(db.get_business(1), FakeFetcher(), finders=[DomainGuessFinder()]))
    assert db.get_business(1)["website_state"] == "unknown"
    assert "no_website_confirmed" not in {f["code"] for f in r["findings"]}


def test_domain_guessing_is_on_by_default_and_free(monkeypatch):
    for key in ("BRAVE_API_KEY", "TAVILY_API_KEY", "GOOGLE_PLACES_API_KEY"):
        monkeypatch.setattr(config, key, "")
    monkeypatch.setattr(config, "DOMAIN_GUESS", True)
    assert [f.name for f in get_finders(background=True)] == ["domain_guess"]
    monkeypatch.setattr(config, "TAVILY_API_KEY", "tvly-x")
    assert [f.name for f in get_finders(background=True)] == ["domain_guess"]                 # Tavily: on demand only
    assert [f.name for f in get_finders(background=False)] == ["domain_guess", "search:tavily"]


def test_tavily_request_cap_and_errors(tmpdb, monkeypatch):
    monkeypatch.setattr(config, "TAVILY_API_KEY", "tvly-x")
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"results": [{"url": "https://www.yelp.com/biz/acme"},
                                                     {"url": "https://acmeplumbingtampa.com/about"}]})
    profile = {"name": "Acme Plumbing", "names": ["Acme Plumbing"], "phones": ["8135550101"], "cities": ["Tampa"], "city": "Tampa"}
    out = asyncio.run(TavilyWebsiteFinder(httpx.MockTransport(handler)).find_candidates(profile))
    assert seen[0].headers["Authorization"] == "Bearer tvly-x"
    import json
    body = json.loads(seen[0].content)
    assert body["search_depth"] == "basic" and "(813) 555-0101" in body["query"]
    assert [c["url"] for c in out["candidates"]] == ["https://acmeplumbingtampa.com/"] and out["directories"] == ["yelp.com"]
    with pytest.raises(SourceError, match="plan limit"):
        asyncio.run(TavilyWebsiteFinder(httpx.MockTransport(lambda r: httpx.Response(432))).find_candidates(profile))
    monkeypatch.setattr(config, "TAVILY_MONTHLY_LIMIT", 2)
    with pytest.raises(SourceError, match="monthly search limit"):
        asyncio.run(TavilyWebsiteFinder(httpx.MockTransport(handler)).find_candidates(profile))
    assert len(seen) == 1                                                                       # refused before sending


def test_only_one_server_per_database(tmpdb):
    db.acquire_server_lock()
    with pytest.raises(db.AlreadyRunning, match="already using"):
        db.acquire_server_lock()        # a second server on the same database is refused
    assert db.conn().execute("PRAGMA journal_mode").fetchone()[0] == "wal"
