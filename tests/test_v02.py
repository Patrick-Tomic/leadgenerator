"""v0.2: migrations, dedup, discovery scans, customer isolation, configurable scoring."""
import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app import config, db, migrations, pipeline
from app.dedup import normalize_domain, normalize_name, normalize_phone
from app.discovery.base import DiscoveryAdapter, SourceError
from app.discovery.osm import OverpassAdapter, build_query
from app.profiles import ProfileError, default_profile, validate
from app.qualify import qualify_business, requalify_customer
from app.scoring import classify_finding, score_lead


@pytest.fixture
def tmpdb(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "backups")
    db.use(tmp_path / "leads.db")
    yield tmp_path / "leads.db"
    db._conn.close()
    db._conn = None


def biz(**kw):
    b = {"source": "osm", "source_id": "node/1", "name": "Ace Plumbing LLC", "industry": "plumbing", "address": None,
         "city": "Tampa", "lat": 27.95, "lon": -82.46, "phone": "+1 (813) 555-0101", "email": None,
         "website": "https://www.aceplumbing.com/", "raw": {"tags": {}}}
    b.update(kw)
    return b


def audited(lead_id, findings, status="ok"):
    db.save_analysis(lead_id, {"website_status": status, "findings": findings})


F_NO_VIEWPORT = {"code": "no_viewport", "title": "t", "evidence": "No viewport tag.", "severity": "high", "confidence": "high"}
F_NO_CHAT = {"code": "no_chat", "title": "t", "evidence": "No chat.", "severity": "low", "confidence": "medium"}
F_RESTRICTED = {"code": "access_restricted", "title": "t", "evidence": "403", "severity": "low", "confidence": "low"}


# ---------------------------------------------------------------- migrations

def make_v0_db(path):
    """A database exactly as v0.1 left it, with workflow state on the leads row."""
    c = sqlite3.connect(path)
    c.executescript(migrations.BASE_SCHEMA)
    rows = [
        ("osm", "node/1", "Ace Plumbing", "plumbing", 27.95, -82.46, "8135550101", "https://ace.com", "2026-10-01T00:00:00+00:00",
         "2026-10-01T01:00:00+00:00", "ok", json.dumps([F_NO_VIEWPORT]), json.dumps([{"key": "redesign"}]), 19, "D", "sum1", "saved", "call back", "brief!"),
        ("osm", "way/2", "Bob's HVAC", "hvac", 28.0, -82.5, None, None, "2026-10-01T00:00:00+00:00",
         None, None, None, None, None, None, None, "new", None, None),
    ]
    c.executemany("""INSERT INTO leads (source, source_id, name, industry, lat, lon, phone, website, discovered_at, analyzed_at,
                     website_status, findings, opportunities, score, grade, summary, status, notes, ai_brief, raw)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'{}')""", rows)
    c.execute("INSERT INTO source_runs VALUES ('osm:plumbing', '2026-10-01T00:00:00+00:00', 1)")
    c.commit()
    c.close()


def test_migration_preserves_existing_data(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "backups")
    path = tmp_path / "old.db"
    make_v0_db(path)
    before = [tuple(r) for r in sqlite3.connect(path).execute("SELECT * FROM leads ORDER BY id")]
    db.use(path)
    try:
        assert migrations.version(db.conn()) == migrations.LATEST
        # legacy columns untouched
        after = [tuple(r)[:len(before[0])] for r in db.conn().execute("SELECT * FROM leads ORDER BY id")]
        assert after == before
        # backup is a full v0 copy
        backups = list((tmp_path / "backups").glob("old-v0-*.db"))
        assert len(backups) == 1
        assert sqlite3.connect(backups[0]).execute("PRAGMA user_version").fetchone()[0] == 0
        # workflow state moved to the default customer verbatim
        cust = db.get_customer("atomic")
        lead = db.get_lead(cust["id"], 1)
        assert (lead["status"], lead["notes"], lead["ai_brief"], lead["score"], lead["summary"]) == ("saved", "call back", "brief!", 19, "sum1")
        assert db.get_lead(cust["id"], 2) is None  # never analyzed, no workflow state: not assigned yet
        assert db.lead_scope(1) == ({"plumbing"}, {"tampa_bay_legacy"})
        assert db.scan_history("osm", "tampa_bay_legacy", "plumbing")["last_ok"] == "2026-10-01T00:00:00+00:00"
    finally:
        db._conn.close(); db._conn = None


def test_migration_is_idempotent_and_refuses_newer_schema(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "backups")
    path = tmp_path / "old.db"
    make_v0_db(path)
    c = sqlite3.connect(path)
    assert migrations.migrate(c, path) == list(range(1, migrations.LATEST + 1))
    assert migrations.migrate(c, path) == []  # second run: nothing to do, no second backup
    assert len(list((tmp_path / "backups").iterdir())) == 1
    c.execute(f"PRAGMA user_version = {migrations.LATEST + 1}")
    with pytest.raises(RuntimeError, match="newer than this code"):
        migrations.migrate(c, path)


def test_failed_migration_rolls_back(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "backups")
    path = tmp_path / "old.db"
    make_v0_db(path)

    def broken(c):
        c.execute("CREATE TABLE half_done (x)")
        c.execute("ALTER TABLE leads ADD COLUMN junk TEXT")
        raise RuntimeError("boom")

    monkeypatch.setattr(migrations, "MIGRATIONS", [broken])
    monkeypatch.setattr(migrations, "LATEST", 1)
    c = sqlite3.connect(path)
    with pytest.raises(RuntimeError, match="boom"):
        migrations.migrate(c, path)
    assert migrations.version(c) == 0
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "half_done" not in tables
    assert "junk" not in {r[1] for r in c.execute("PRAGMA table_info(leads)")}


def test_fresh_database_gets_default_customer(tmpdb):
    assert migrations.version(db.conn()) == migrations.LATEST
    assert [c["slug"] for c in db.list_customers()] == ["atomic"]
    assert not (tmpdb.parent / "backups").exists()  # nothing to back up


# ---------------------------------------------------------------- dedup

def test_normalizers():
    assert normalize_name("The Ace Plumbing, L.L.C.") == normalize_name("Ace Plumbing LLC") == "ace plumbing"
    assert normalize_name("Smith & Sons") == "smith and sons"
    assert normalize_domain("https://WWW.AcePlumbing.com/contact") == "aceplumbing.com"
    assert normalize_domain("aceplumbing.com") == "aceplumbing.com"
    assert normalize_domain("https://facebook.com/ace") is None
    assert normalize_domain("https://ace.wixsite.com/home") is None
    assert normalize_phone("+1 (813) 555-0101") == normalize_phone("813.555.0101") == "8135550101"
    assert normalize_phone("555-0101") is None


def test_dedup_rules(tmpdb):
    assert db.upsert_business(biz()) == "added"
    assert db.upsert_business(biz()) == "seen"                                            # same source record
    assert db.upsert_business(biz(source_id="way/9", name="ACE PLUMBING", phone=None)) == "merged"  # same domain
    assert db.upsert_business(biz(source_id="node/3", name="Ace Plumbing & Drain", website=None)) == "merged"  # same phone
    # same name, ~100 m away, no shared domain/phone -> same place
    assert db.upsert_business(biz(source_id="node/4", website=None, phone=None, lat=27.9509, lon=-82.4600)) == "merged"
    # same name, other side of town -> a different branch/business
    assert db.upsert_business(biz(source_id="node/5", website=None, phone=None, lat=28.30, lon=-82.70)) == "added"
    # unrelated business
    assert db.upsert_business(biz(source_id="node/6", name="Zed Roofing", website="zedroofing.com", phone="7275550000")) == "added"
    assert db.conn().execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 3
    assert db.conn().execute("SELECT COUNT(*) FROM lead_sources WHERE lead_id=1").fetchone()[0] == 4


def test_merge_fills_gaps_and_triggers_reaudit(tmpdb):
    db.upsert_business(biz(website=None, email=None, address=None))
    audited(1, [{"code": "no_website_unverified", "title": "t", "evidence": "e", "severity": "high", "confidence": "low"}], "unverified")
    assert db.get_business(1)["analyzed_at"]
    db.upsert_business(biz(source_id="way/2", website="https://acenew.com", email="hi@acenew.com", address="1 Main St"))
    b = db.get_business(1)
    assert (b["website"], b["email"], b["address"]) == ("https://acenew.com", "hi@acenew.com", "1 Main St")
    assert b["analyzed_at"] is None  # newly learned website -> re-audit
    # merge never overwrites known values
    db.upsert_business(biz(source_id="way/3", website="https://acenew.com", phone="+1 727 000 0000"))
    assert db.get_business(1)["phone"] == "+1 (813) 555-0101"


# ---------------------------------------------------------------- discovery

OVERPASS_OK = {"elements": [
    {"type": "node", "id": 1, "lat": 27.95, "lon": -82.46, "tags": {"name": "Ace Plumbing", "craft": "plumber"}},
    {"type": "way", "id": 2, "center": {"lat": 27.96, "lon": -82.47}, "tags": {"name": "Chain Co", "brand": "Chain"}},
    {"type": "node", "id": 3, "lat": 27.97, "lon": -82.48, "tags": {"craft": "plumber"}},  # no name
]}


def adapter(handler):
    return OverpassAdapter(transport=httpx.MockTransport(handler), retry_delay=0, min_interval=0)


def test_overpass_parses_and_filters():
    out = asyncio.run(adapter(lambda r: httpx.Response(200, json=OVERPASS_OK)).discover("plumbing", "hillsborough"))
    assert [b["source_id"] for b in out] == ["node/1"]  # chain and nameless skipped


@pytest.mark.parametrize("response,match", [
    (httpx.Response(500, text="oops"), "HTTP 500"),
    (httpx.Response(429, text="slow down"), "rate limited"),
    (httpx.Response(200, text="<html>not json</html>"), "non-JSON"),
    (httpx.Response(200, json={"elements": [], "remark": "runtime error: Query timed out in \"query\""}), "timed out"),
])
def test_overpass_failures_raise_source_error(response, match):
    with pytest.raises(SourceError, match=match):
        asyncio.run(adapter(lambda r: response).discover("plumbing", "hillsborough"))


def test_overpass_network_error_raises_source_error():
    def boom(request):
        raise httpx.ConnectError("no route")
    with pytest.raises(SourceError, match="ConnectError"):
        asyncio.run(adapter(boom).discover("plumbing", "hillsborough"))


def test_overpass_retries_busy_then_succeeds():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429) if len(calls) == 1 else httpx.Response(200, json=OVERPASS_OK)
    assert len(asyncio.run(adapter(handler).discover("plumbing", "hillsborough"))) == 1
    assert len(calls) == 2


def test_incremental_query_uses_newer_filter():
    seen = {}

    def handler(request):
        seen["body"] = request.content.decode()
        return httpx.Response(200, json={"elements": []})
    asyncio.run(adapter(handler).discover("plumbing", "pinellas", since="2026-10-01T00:00:00Z"))
    from urllib.parse import unquote_plus
    assert '(newer:"2026-10-01T00:00:00Z")(27.6,-82.87,28.18,-82.54)' in unquote_plus(seen["body"])
    assert "newer" not in build_query(['["craft"="hvac"]'], (1, 2, 3, 4))


class FakeAdapter(DiscoveryAdapter):
    name = "fake"
    supports_incremental = True

    def __init__(self, results=None, fail=False):
        self.results, self.fail, self.calls = results or [], fail, []

    async def discover(self, industry_key, area_key, since=None):
        self.calls.append((industry_key, area_key, since))
        if self.fail:
            raise SourceError("source down")
        return self.results


def test_scan_planning_full_incremental_and_cooldown(tmpdb, monkeypatch):
    fake = FakeAdapter(results=[biz()])
    monkeypatch.setattr(pipeline, "ADAPTERS", [fake])
    profile = {**default_profile(), "areas": ["hillsborough"], "industries": ["plumbing"]}

    plan = pipeline.plan_scans(profile)
    assert [(s["area"], s["industry"], s["mode"]) for s in plan] == [("hillsborough", "plumbing", "full")]
    assert asyncio.run(pipeline.run_scan(plan[0])) == {"status": "ok", "found": 1, "added": 1, "merged": 0}
    assert pipeline.plan_scans(profile) == []  # fresh: nothing repeated

    later = datetime.now(timezone.utc) + timedelta(days=config.OVERPASS_CACHE_DAYS + 1)
    stale = pipeline.plan_scans(profile, now=later)
    assert stale[0]["mode"] == "incremental" and stale[0]["since"]

    fake.fail = True
    res = asyncio.run(pipeline.run_scan(stale[0]))
    assert res["status"] == "error"
    scan = db.recent_scans(1)[0]
    assert (scan["status"], scan["error"]) == ("error", "source down")
    db.conn().execute("UPDATE discovery_scans SET finished_at=? WHERE id=?", (later.isoformat(timespec="seconds"), scan["id"]))
    assert pipeline.plan_scans(profile, now=later + timedelta(minutes=5)) == []  # in failure cooldown
    assert pipeline.plan_scans(profile, now=later + timedelta(hours=1))  # retried after cooldown


def test_legacy_scan_of_containing_area_makes_rescan_incremental(tmpdb, monkeypatch):
    monkeypatch.setattr(pipeline, "ADAPTERS", [FakeAdapter()])
    sid = db.start_scan("fake", "tampa_bay_legacy", "plumbing", "full", None)
    db.finish_scan(sid, "ok", found=5)
    profile = {**default_profile(), "areas": ["hillsborough", "pasco"], "industries": ["plumbing"]}
    later = datetime.now(timezone.utc) + timedelta(days=config.OVERPASS_CACHE_DAYS + 1)
    modes = {s["area"]: s["mode"] for s in pipeline.plan_scans(profile, now=later)}
    assert modes == {"hillsborough": "incremental", "pasco": "full"}  # pasco extends past the legacy box


def test_refill_stops_after_consecutive_source_failures(tmpdb, monkeypatch):
    fake = FakeAdapter(fail=True)
    monkeypatch.setattr(pipeline, "ADAPTERS", [fake])
    asyncio.run(pipeline.refill("atomic"))
    assert len(fake.calls) == config.MAX_CONSECUTIVE_SCAN_FAILURES
    assert "failed in a row" in pipeline.state["error"]
    assert not pipeline.state["running"]


# ---------------------------------------------------------------- qualification & isolation

def second_customer(**overrides):
    p = {**default_profile(), "slug": "poolco", "name": "PoolCo Marketing", "areas": ["pinellas"],
         "industries": ["pool", "plumbing"],
         "offerings": {"seo": {"label": "SEO", "codes": ["no_viewport"], "pitch": "p", "cap": 50}}}
    p.update(overrides)
    return validate(p)


def test_business_has_independent_scores_and_statuses_per_customer(tmpdb):
    atomic = db.get_customer("atomic")
    cid, _ = db.sync_customer(second_customer(areas=["hillsborough"]))
    db.upsert_business(biz())
    audited(1, [F_NO_VIEWPORT, F_NO_CHAT])
    assert qualify_business(1) == 2
    a, p = db.get_lead(atomic["id"], 1), db.get_lead(cid, 1)
    assert a["score"] != p["score"]
    assert [o["key"] for o in p["opportunities"]] == ["seo"]
    db.update_lead(cid, 1, status="contacted", notes="poolco note")
    assert db.get_lead(atomic["id"], 1)["status"] == "new" and db.get_lead(atomic["id"], 1)["notes"] is None
    assert db.counts(cid)["by_status"] == {"contacted": 1}
    assert db.counts(atomic["id"])["by_status"] == {"new": 1}


def test_out_of_scope_leads_are_invisible_to_customer(tmpdb):
    atomic = db.get_customer("atomic")
    cid, _ = db.sync_customer(second_customer())  # pinellas only
    db.upsert_business(biz())                      # Tampa (Hillsborough)
    db.upsert_business(biz(source_id="n/2", name="Gulf Pools", industry="pool", website="gulfpools.com", phone=None,
                           lat=27.85, lon=-82.80))  # Seminole (Pinellas), not an Atomic... industry pool is Atomic too
    for lid in (1, 2):
        audited(lid, [F_NO_VIEWPORT])
        qualify_business(lid)
    assert [l["id"] for l in db.list_leads(cid)] == [2]
    assert db.get_lead(cid, 1) is None
    assert {l["id"] for l in db.list_leads(atomic["id"])} == {1, 2}
    assert db.next_lead(cid)["id"] == 2


def test_api_is_customer_scoped(tmpdb):
    from fastapi.testclient import TestClient
    from app.main import app
    cid, _ = db.sync_customer(second_customer())
    db.upsert_business(biz())  # Hillsborough: atomic only
    audited(1, [F_NO_VIEWPORT])
    qualify_business(1)
    client = TestClient(app)  # no context manager: lifespan (and its network refill) doesn't run
    assert client.get("/api/leads/1").status_code == 200
    assert client.get("/api/leads/1?customer=poolco").status_code == 404
    assert client.post("/api/leads/1?customer=poolco", json={"status": "won"}).status_code == 404
    assert client.get("/api/leads/1").json()["status"] == "new"
    assert "Ace Plumbing" in client.get("/api/export.csv").text
    assert "Ace Plumbing" not in client.get("/api/export.csv?customer=poolco").text
    assert client.get("/api/leads?customer=nobody").status_code == 404
    kinds = {f["code"]: f["kind"] for f in client.get("/api/leads/1").json()["findings"]}
    assert kinds == {"no_viewport": "confirmed"}


def test_profile_change_requalifies_but_keeps_customer_work(tmpdb):
    cid, _ = db.sync_customer(second_customer(areas=["hillsborough"]))
    db.upsert_business(biz())
    db.upsert_business(biz(source_id="n/2", name="Other Plumbing", website="other.com", phone=None, lat=27.96, lon=-82.45))
    for lid in (1, 2):
        audited(lid, [F_NO_VIEWPORT])
        qualify_business(lid)
    db.update_lead(cid, 1, status="saved", notes="keep me")
    # customer narrows scope to Pinellas: untouched lead 2 drops out, worked lead 1 is kept
    cid2, changed = db.sync_customer(second_customer(areas=["pinellas"]))
    assert cid2 == cid and changed
    requalify_customer(db.get_customer("poolco"))
    assert db.get_lead(cid, 2) is None
    assert (db.get_lead(cid, 1)["status"], db.get_lead(cid, 1)["notes"]) == ("saved", "keep me")


def test_scoring_is_configurable():
    b = {"name": "X", "industry": "pool", "phone": "1"}
    base = default_profile()
    s1 = score_lead(b, [F_NO_VIEWPORT], base)["score"]
    heavier = validate({**base, "scoring": {**base["scoring"], "tier_points": {"moderate": 40}}})
    assert score_lead(b, [F_NO_VIEWPORT], heavier)["score"] > s1
    no_redesign = validate({**base, "offerings": {k: v for k, v in base["offerings"].items() if k != "redesign"}})
    r = score_lead(b, [F_NO_VIEWPORT], no_redesign)
    assert r["opportunities"] == [] and r["score"] == 0  # not relevant to this customer's services
    strict = validate({**base, "scoring": {**base["scoring"], "grade_thresholds": {"A": 90, "B": 80, "C": 15}}})
    assert score_lead(b, [F_NO_VIEWPORT], strict)["grade"] == "C"


def test_exclusions(tmpdb):
    cid, _ = db.sync_customer(second_customer(areas=["hillsborough"], exclusions={
        "name_patterns": ["^ace\\b"], "domains": [], "website_statuses": []}))
    db.upsert_business(biz())
    audited(1, [F_NO_VIEWPORT])
    qualify_business(1)
    assert db.get_lead(cid, 1) is None
    assert db.get_lead(db.get_customer("atomic")["id"], 1) is not None


def test_profile_validation():
    with pytest.raises(ProfileError, match="unknown areas"):
        validate({**default_profile(), "areas": ["atlantis"]})
    with pytest.raises(ProfileError, match="unknown industries"):
        validate({**default_profile(), "industries": ["alchemy"]})
    with pytest.raises(ProfileError, match="missing 'pitch'"):
        validate({**default_profile(), "offerings": {"x": {"label": "l", "codes": []}}})


def test_finding_kinds_and_opportunity_basis():
    assert classify_finding(F_NO_VIEWPORT) == "confirmed"
    assert classify_finding(F_NO_CHAT) == "potential"
    assert classify_finding(F_RESTRICTED) == "unavailable"
    assert classify_finding({**F_NO_VIEWPORT, "confidence": "low"}) == "potential"
    assert classify_finding({"code": "site_dns", "severity": "high", "confidence": "medium"}) == "potential"
    opps = score_lead({"name": "X", "industry": "pool"}, [F_NO_VIEWPORT, F_NO_CHAT, F_RESTRICTED])["opportunities"]
    assert {o["key"]: o["basis"] for o in opps} == {"redesign": "confirmed", "ai_chatbot": "potential"}
