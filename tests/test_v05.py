"""v0.5: DBPR contractor licenses as operating evidence; phone numbers from websites and manual entry."""
import asyncio
import csv
import io
import os

import pytest

from app import config, db, licenses, registrations
from app.analysis import website
from app.analysis.fetcher import FetchResult
from app.discovery.dbpr import parse_row
from app.pipeline import analyze_business

ROW = ["06", "QB", "", "", "", "", "", "", "TAMPA", "FL", "33602", "29", "", "C", "", "10/01/2026", "10/01/2026", "", "", "", "", ""]


def row(**kw):
    r = list(ROW)
    for i, k in enumerate(["board", "occupation", "licensee", "dba", "class", "addr1", "addr2", "addr3", "city", "state", "zip",
                           "county", "license_number", "primary_status", "secondary_status", "original_date"]):
        if k in kw:
            r[i] = kw[k]
    if "alt" in kw:
        r[20] = kw["alt"]
    return r


def write_csv(path, rows, mtime=None):
    buf = io.StringIO()
    csv.writer(buf, quoting=csv.QUOTE_ALL).writerows(rows)
    path.write_text(buf.getvalue(), encoding="latin-1")
    if mtime:
        os.utime(path, (mtime, mtime))
    return path


@pytest.fixture
def tmpdb(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(website, "RETRY_DELAY", 0)
    db.use(tmp_path / "leads.db")
    yield tmp_path
    db._conn.close()
    db._conn = None


# ---------------------------------------------------------------- parsing & privacy

def test_business_license_parsed():
    rec = parse_row(row(licensee="BAY BREEZE AIR CONDITIONING & HEATING, INC.", addr1="12 MAIN ST"))
    assert rec["business_name"] == "BAY BREEZE AIR CONDITIONING & HEATING, INC." and rec["is_individual"] == 0
    assert rec["address"] == "12 MAIN ST" and rec["active"] == 1 and rec["original_date"] == "2026-10-01"


def test_individuals_keep_only_business_details():
    rec = parse_row(row(occupation="CAC", licensee="SMITH, JOHN A", dba="SMITH COOLING", addr1="9 HOME LN", alt="CAC1234567"))
    assert rec["business_name"] is None and rec["dba_name"] == "SMITH COOLING" and rec["address"] is None
    assert rec["license_number"] == "CAC1234567" and "SMITH, JOHN" not in str(rec)
    assert parse_row(row(occupation="CAC", licensee="SMITH, JOHN A", dba="INDIVIDUAL")) is None   # placeholder, no business
    assert parse_row(row(occupation="FRO", licensee="ACME LLC")) is None                          # officers skipped
    assert parse_row(row(secondary_status="I", licensee="ACME LLC"))["active"] == 0


# ---------------------------------------------------------------- import

def test_import_is_versioned_and_drops_unlisted_records(tmpdb):
    p = write_csv(tmpdb / "CONSTRUCTIONLICENSE_1.csv", [row(licensee="ACME ROOFING LLC"), row(licensee="BETA POOLS INC")], mtime=1_790_000_000)
    r = licenses.import_file(p)
    assert (r["status"], r["kept"]) == ("ok", 2)
    assert licenses.import_file(p)["status"] == "skipped"                    # same weekly version
    write_csv(p, [row(licensee="ACME ROOFING LLC")], mtime=1_790_600_000)     # next week: BETA gone
    r = licenses.import_file(p)
    assert r["status"] == "ok" and r["no_longer_listed"] == 1
    active = dict(db.conn().execute("SELECT business_name, active FROM licenses").fetchall())
    assert active == {"ACME ROOFING LLC": 1, "BETA POOLS INC": 0}


def test_failed_import_keeps_existing_licenses(tmpdb, monkeypatch):
    p = write_csv(tmpdb / "CONSTRUCTIONLICENSE_1.csv", [row(licensee="ACME ROOFING LLC")], mtime=1_790_000_000)
    licenses.import_file(p)
    write_csv(p, [row(licensee="NEW CO LLC")], mtime=1_790_600_000)
    monkeypatch.setattr(licenses, "parse_row", lambda r: (_ for _ in ()).throw(OSError("bad disk")))
    assert licenses.import_file(p)["status"] == "error"
    assert db.conn().execute("SELECT business_name, active FROM licenses").fetchall()[0][:] == ("ACME ROOFING LLC", 1)


# ---------------------------------------------------------------- matching & flags

def cor_reg(doc, name, city="TAMPA", zip_="33602"):
    from test_v04 import cor
    return cor(doc, name, city=city, zip_=zip_)


def test_licenses_mark_registrations_as_operating(tmpdb):
    from test_v04 import write
    registrations.import_file(write(tmpdb, "20261014c.txt", [
        cor_reg("L1", "ACME ROOFING LLC"), cor_reg("L2", "GAMMA ROOFING LLC", city="LARGO", zip_="33770"),
        cor_reg("L3", "DELTA ROOFING LLC")]), since="2000-01-01")
    licenses.import_file(write_csv(tmpdb / "CONSTRUCTIONLICENSE_1.csv", [
        row(licensee="ACME ROOFING, L.L.C."),                          # same name + city -> high
        row(licensee="GAMMA ROOFING LLC", city="MIAMI", zip="33101"),  # name only -> medium
    ]))
    regs = {r["doc_number"]: r for r in registrations.list_registrations(limit=10)}
    assert regs["L1"]["license_match_confidence"] == "high" and "state_license" in regs["L1"]["flags"]
    assert "operating_evidence" in regs["L1"]["flags"]
    assert regs["L2"]["license_match_confidence"] == "medium"
    assert regs["L3"]["license_id"] is None and "state_license" not in regs["L3"]["flags"]
    assert [r["doc_number"] for r in registrations.list_registrations(limit=10)][:2] == ["L1", "L2"]  # licensed first


def test_business_gets_license_provenance_only_on_location_match(tmpdb):
    from test_v04 import osm
    db.upsert_business(osm(1, tags={"name": "Acme Roofing", "addr:postcode": "33602"}))
    db.upsert_business(osm(2, tags={"name": "Beta Roofing", "phone": "7275550000", "addr:city": "Largo"}, lat=27.9))
    licenses.import_file(write_csv(tmpdb / "CONSTRUCTIONLICENSE_1.csv", [
        row(licensee="ACME ROOFING LLC"), row(licensee="BETA ROOFING LLC", city="MIAMI", zip="33101")]))
    assert [s["source"] for s in db.lead_sources(1)] == ["osm", "dbpr_cilb"]
    assert [s["source"] for s in db.lead_sources(2)] == ["osm"]


def test_promoted_licensed_registration_has_operating_evidence(tmpdb):
    from test_v04 import FakeFetcher, write
    registrations.import_file(write(tmpdb, "20261014c.txt", [cor_reg("L1", "ACME ROOFING LLC")]), since="2000-01-01")
    licenses.import_file(write_csv(tmpdb / "CONSTRUCTIONLICENSE_1.csv", [row(licensee="ACME ROOFING LLC")]))
    lead_id = registrations.promote(registrations.list_registrations(limit=1)[0]["id"])
    assert {s["source"] for s in db.lead_sources(lead_id)} == {"sunbiz_cor", "dbpr_cilb"}
    r = asyncio.run(analyze_business(db.get_business(lead_id), FakeFetcher(), finders=[]))
    nr = next(f for f in r["findings"] if f["code"] == "new_registration")
    assert nr["confidence"] == "high" and "Florida DBPR" in nr["evidence"]


# ---------------------------------------------------------------- phone numbers

SITE = "https://acmeplumbing.example/"


def test_phone_from_verified_website_fills_missing_phone(tmpdb):
    from test_v04 import FakeFetcher, ok, osm
    b = osm(1, tags={"website": SITE})
    b["phone"] = None; b["fields"].pop("phone"); b["fields"].pop("phones")
    db.upsert_business(b)
    page = ok(SITE, '<a href="tel:8135559999">(813) 555-9999</a> Acme Plumbing, 12 Main St, Tampa')
    r = asyncio.run(analyze_business(db.get_business(1), FakeFetcher({SITE: page}), finders=[]))
    assert db.get_business(1)["phone"] == "(813) 555-9999"
    src = next(s for s in db.lead_sources(1) if s["source"] == "website")
    assert "verified as theirs" in src["match_explanation"]
    assert "phone_from_website" in {f["code"] for f in r["findings"]}


def test_website_phone_never_replaces_listed_phone_or_comes_from_unverified_site(tmpdb):
    from test_v04 import FakeFetcher, ok, osm
    db.upsert_business(osm(1, tags={"website": SITE}))                       # listed 813-555-0101
    page = ok(SITE, '<a href="tel:8135559999">(813) 555-9999</a> Acme Plumbing, 12 Main St, Tampa')
    asyncio.run(analyze_business(db.get_business(1), FakeFetcher({SITE: page}), finders=[]))
    assert db.get_business(1)["phone"] == "813-555-0101"
    b = osm(2, tags={"website": "https://other.example/", "name": "Zed Air", "phone": ""}, lat=28.3)
    b["phone"] = None
    db.upsert_business(b)
    other = ok("https://other.example/", '<a href="tel:7275550000">(727) 555-0000</a> nothing about the business')
    asyncio.run(analyze_business(db.get_business(2), FakeFetcher({"https://other.example/": other}), finders=[]))
    assert db.get_business(2)["phone"] is None    # site not verified as theirs -> phone not taken


def test_manual_phone_entry(tmpdb):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.qualify import qualify_business
    from test_v04 import osm
    b = osm(1)
    b["phone"] = None
    db.upsert_business(b)
    db.save_analysis(1, {"website_status": "unknown", "findings": [{"code": "_facts", "facts": {}}]})
    qualify_business(1)
    c = TestClient(app)
    assert c.post("/api/leads/1/phone", json={"phone": "555-0101"}).status_code == 400
    lead = c.post("/api/leads/1/phone", json={"phone": "+1 (727) 555-0142"}).json()
    assert lead["phone"] == "(727) 555-0142"
    rows = {r["field"]: r for r in lead["details"]["fields"]}
    assert any("Entered by you" in v["sources"] for v in rows["phone"]["values"])
    assert db.get_lead(db.get_customer("atomic")["id"], 1)["status"] == "new"


def test_established_company_filing_a_new_name_is_flagged(tmpdb):
    from test_v04 import write
    registrations.import_file(write(tmpdb, "20261014c.txt", [cor_reg("L1", "JUST KOOLIN AIR LLC")]), since="2000-01-01")
    licenses.import_file(write_csv(tmpdb / "CONSTRUCTIONLICENSE_1.csv", [row(licensee="JUST KOOLIN AIR LLC", original_date="10/01/2012")]))
    flags = registrations.list_registrations(limit=1)[0]["flags"]
    assert "established_business" in flags and "state_license" in flags


def test_next_and_discover_start_a_refill_without_error(tmpdb, monkeypatch):
    """Regression: /api/next returned 500 whenever the ready pool was below target, because
    it scheduled the refill from a worker thread with no event loop."""
    from fastapi.testclient import TestClient
    from app import pipeline
    from app.main import app
    started = []

    async def fake_refill(slug, force=False):  # stub only the network work, not start_refill
        started.append(slug)
        pipeline.state["running"] = False
    monkeypatch.setattr(pipeline, "refill", fake_refill)
    pipeline.state["running"] = False
    with TestClient(app) as c:   # runs the real event loop, like uvicorn
        pipeline.state["running"] = False
        assert c.get("/api/next").status_code == 200        # 0 ready < target -> refill scheduled
        pipeline.state["running"] = False
        assert c.post("/api/discover").status_code == 200
    assert started and set(started) == {"atomic"}
