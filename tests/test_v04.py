"""v0.4: website verification, source provenance, Sunbiz registrations, optional Places."""
import asyncio
import json

import httpx
import pytest

from app import config, db, registrations
from app.analysis import website
from app.analysis.fetcher import FetchResult
from app.discovery.base import SourceError, WebsiteFinder
from app.discovery.osm import contributed_fields, element_to_business
from app.discovery.places import FIELD_MASK, GooglePlacesFinder
from app.discovery.sunbiz import COR_LAYOUT, COR_LENGTH, FIC_LAYOUT, FIC_LENGTH, parse_record
from app.pipeline import analyze_business
from app.qualify import qualify_business
from app.scoring import score_lead
from app.websites import verify_website

SITE = "https://acmeplumbing.example/"


@pytest.fixture
def tmpdb(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(website, "RETRY_DELAY", 0)
    db.use(tmp_path / "leads.db")
    yield tmp_path
    db._conn.close()
    db._conn = None


class FakeFetcher:
    """pages: url -> FetchResult or list (one per call; last repeats). Unknown URLs fail DNS."""

    def __init__(self, pages=None):
        self.pages, self._robots, self.calls = pages or {}, {}, []

    async def get(self, url, check_robots=True):
        self.calls.append(url)
        seq = self.pages.get(url)
        if seq is None:
            return FetchResult(url=url, ok=False, error="dns", error_detail="Name or service not known")
        if isinstance(seq, list):
            return seq.pop(0) if len(seq) > 1 else seq[0]
        return seq

    async def link_status(self, url):
        return 200, None

    async def head_ok(self, url):
        return 200


class FakeFinder(WebsiteFinder):
    def __init__(self, name, candidates=None, fail=False, always=False):
        self.name, self.cands, self.fail, self.always, self.calls = name, candidates or [], fail, always, 0

    async def find_candidates(self, profile):
        self.calls += 1
        if self.fail:
            raise SourceError(f"{self.name} down")
        return {"candidates": [dict(c) for c in self.cands], "queries": [f'"{profile["name"]}"'], "checked": ["x"],
                "directories": ["yelp.com"]}


def ok(url, body, final=None, redirects=None):
    html = (f"<html><head><title>Acme</title><meta name=viewport content='width=device-width'></head>"
            f"<body><h1>Acme</h1>{body}<p>{'Plumbing services. ' * 40}</p></body></html>")
    return FetchResult(url=url, ok=True, status=200, final_url=final or url, html=html, elapsed=0.2, redirects=redirects or [])


IDENTIFYING = '<a href="tel:8135550101">(813) 555-0101</a> Acme Plumbing, 12 Main St, Tampa'


def osm(source_id, **kw):
    tags = {"name": "Acme Plumbing", "craft": "plumber", "addr:housenumber": "12", "addr:street": "Main St",
            "addr:city": "Tampa", "phone": "813-555-0101", **kw.pop("tags", {})}
    el = {"type": "node", "id": source_id, "lat": kw.pop("lat", 27.95), "lon": kw.pop("lon", -82.46), "tags": tags}
    return element_to_business(el, "plumbing")


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- website states

def test_missing_listing_field_is_unknown_not_no_website(tmpdb):
    db.upsert_business(osm(1))
    r = run(analyze_business(db.get_business(1), FakeFetcher(), finders=[]))
    assert db.get_business(1)["website_state"] == "unknown"
    codes = {f["code"] for f in r["findings"]}
    assert "website_unknown" in codes and "no_website_confirmed" not in codes and "no_website_unverified" not in codes
    assert score_lead({"name": "x", "industry": "plumbing", "phone": "1"}, r["findings"])["score"] == 0


def test_website_from_one_source_survives_another_without_it(tmpdb):
    db.upsert_business(osm(1))                                                   # no website
    assert db.upsert_business(osm(2, tags={"website": SITE}, lat=27.9501)) == "merged"   # same phone, has website
    assert db.upsert_business(osm(3, lat=27.9502)) == "merged"                   # another record, no website
    cands = db.website_candidates(1)
    assert [c["url"] for c in cands] == [SITE]
    v = run(verify_website(db.get_business(1), FakeFetcher({SITE: ok(SITE, IDENTIFYING)}), []))
    assert v["state"] == "verified" and db.get_business(1)["website"] == SITE


def test_search_candidate_verified_by_page_content(tmpdb):
    db.upsert_business(osm(1))
    finder = FakeFinder("search:brave", [{"url": SITE, "source": "search:brave", "evidence": ["search result #1"]}])
    r = run(analyze_business(db.get_business(1), FakeFetcher({SITE: ok(SITE, IDENTIFYING)}), finders=[finder]))
    b = db.get_business(1)
    assert b["website_state"] == "verified" and b["website"] == SITE
    found = next(f for f in r["findings"] if f["code"] == "website_found_by_search")
    assert "search result #1" in found["evidence"]
    cand = db.website_candidates(1)[0]
    assert cand["source"] == "search:brave" and any("phone" in e for e in cand["match_evidence"])


def test_uncertain_identity_is_not_assigned(tmpdb):
    db.upsert_business(osm(1))
    other = "https://acme-roofing.example/"
    finder = FakeFinder("search:brave", [{"url": other, "source": "search:brave", "evidence": ["search result #3"]}])
    r = run(analyze_business(db.get_business(1), FakeFetcher({other: ok(other, "<p>Roofing in Ohio</p>")}), finders=[finder]))
    b = db.get_business(1)
    assert b["website_state"] == "possible_match" and b["website"] is None
    assert "website_match_uncertain" in {f["code"] for f in r["findings"]}
    assert db.website_candidates(1)[0]["identity"] == "low"  # kept for review, not used


def test_name_only_match_is_never_enough():
    from app.websites import page_identity
    profile = {"names": ["Acme Plumbing"], "phones": ["8135550101"], "cities": ["Tampa"], "addresses": ["12 Main St"]}
    level, ev = page_identity(profile, "<p>Welcome to Acme Plumbing</p>", "https://other.example/")
    assert level == "low"
    level, _ = page_identity(profile, "<p>Welcome to Acme Plumbing in Tampa</p>", "https://other.example/")
    assert level == "medium"
    level, _ = page_identity(profile, '<a href="tel:8135550101">call</a>', "https://other.example/")
    assert level == "high"


def test_conflicting_urls_are_kept_and_flagged(tmpdb):
    other = "https://acme-fl.example/"
    db.upsert_business(osm(1, tags={"website": SITE}))
    db.set_website(1, other)
    fetcher = FakeFetcher({SITE: ok(SITE, IDENTIFYING), other: ok(other, IDENTIFYING)})
    r = run(analyze_business(db.get_business(1), fetcher, finders=[]))
    b = db.get_business(1)
    assert b["website_conflict"] == 1 and b["website"] == other  # manual correction wins, both kept
    assert {c["url"] for c in db.website_candidates(1)} == {SITE, other}
    conflict = next(f for f in r["findings"] if f["code"] == "website_conflict")
    assert SITE in conflict["evidence"] and other in conflict["evidence"]


def test_redirects_are_followed_and_recorded(tmpdb):
    new = "https://www.acme-new.example/"
    db.upsert_business(osm(1, tags={"website": SITE}))
    v = run(verify_website(db.get_business(1), FakeFetcher({SITE: ok(SITE, IDENTIFYING, final=new, redirects=[SITE])}), []))
    c = db.website_candidates(1)[0]
    assert v["state"] == "verified" and c["final_url"] == new and "redirected" in c["detail"]
    fb = "https://facebook.com/acme"
    db.upsert_business(osm(9, tags={"website": "https://acme-social.example/", "name": "Zed Plumbing", "phone": "7275550000"},
                           lat=28.3))
    v = run(verify_website(db.get_business(2), FakeFetcher({"https://acme-social.example/": ok("https://acme-social.example/", "",
                                                                                              final=fb)}), []))
    assert v["candidates"][0]["check_status"] == "social" and v["state"] == "unknown"


@pytest.mark.parametrize("failure,state", [
    (FetchResult(url=SITE, ok=False, error="timeout", error_detail="No response within 15s"), "unreachable"),
    (FetchResult(url=SITE, ok=False, status=403, error="http_error", error_detail="HTTP 403"), "found_unverified"),
    (FetchResult(url=SITE, ok=False, status=503, error="http_error", error_detail="HTTP 503"), "unreachable"),
])
def test_timeouts_and_403_never_mean_no_website(tmpdb, failure, state):
    db.upsert_business(osm(1, tags={"website": SITE}))
    r = run(analyze_business(db.get_business(1), FakeFetcher({SITE: failure}), finders=[]))
    b = db.get_business(1)
    assert b["website_state"] == state and b["website"] == SITE
    assert not {"no_website_confirmed", "no_website_unverified", "website_unknown"} & {f["code"] for f in r["findings"]}


def test_none_found_requires_searches_and_one_search_is_only_probable(tmpdb):
    db.upsert_business(osm(1))
    r = run(analyze_business(db.get_business(1), FakeFetcher(), finders=[FakeFinder("search:brave")]))
    nf = next(f for f in r["findings"] if f["code"] == "no_website_confirmed")
    assert db.get_business(1)["website_state"] == "none_found" and nf["confidence"] == "medium"
    assert "yelp.com" in nf["evidence"]
    r = run(analyze_business(db.get_business(1), FakeFetcher(), finders=[FakeFinder("search:brave"), FakeFinder("google_places")]))
    assert next(f for f in r["findings"] if f["code"] == "no_website_confirmed")["confidence"] == "high"
    # A failed search doesn't count as having looked.
    r = run(analyze_business(db.get_business(1), FakeFetcher(), finders=[FakeFinder("search:brave", fail=True)]))
    assert db.get_business(1)["website_state"] == "unknown"
    assert "search failed" in next(f for f in r["findings"] if f["code"] == "website_unknown")["evidence"]


def test_finder_failure_keeps_existing_website(tmpdb):
    db.upsert_business(osm(1, tags={"website": SITE}))
    run(verify_website(db.get_business(1), FakeFetcher({SITE: ok(SITE, IDENTIFYING)}), []))
    v = run(verify_website(db.get_business(1), FakeFetcher({SITE: FetchResult(url=SITE, ok=False, error="timeout",
                                                                                error_detail="t")}),
                           [FakeFinder("search:brave", fail=True, always=True)]))
    assert v["state"] == "unreachable" and db.get_business(1)["website"] == SITE


def test_listed_site_skips_paid_lookups(tmpdb):
    db.upsert_business(osm(1, tags={"website": SITE}))
    places = FakeFinder("google_places")
    run(verify_website(db.get_business(1), FakeFetcher({SITE: ok(SITE, IDENTIFYING)}), [places]))
    assert places.calls == 0


# ---------------------------------------------------------------- provenance & details

def test_duplicates_across_sources_merge_conservatively(tmpdb):
    assert db.upsert_business(osm(1)) == "added"
    assert db.upsert_business(osm(2, tags={"phone": "(813) 555-0101"}, lat=28.4)) == "merged"   # same phone
    # Same name, no shared phone/domain, far away -> separate business
    assert db.upsert_business(osm(3, tags={"phone": "7275559999"}, lat=28.4, lon=-82.7)) == "added"
    # Same name without coordinates and a different street -> not merged
    b = osm(4, tags={"phone": "7275558888", "addr:housenumber": "900", "addr:street": "Oak Ave"})
    b["lat"] = b["lon"] = None
    assert db.upsert_business(b) == "added"
    srcs = db.lead_sources(1)
    assert [s["match_explanation"] for s in srcs] == ["created this business record", "same 10-digit phone number"]
    assert all(s["source_url"].startswith("https://www.openstreetmap.org/") for s in srcs)


def test_incomplete_and_extra_fields_appear_in_lead_details(tmpdb):
    from fastapi.testclient import TestClient
    from app.main import app
    el = {"type": "node", "id": 14, "lat": 27.9, "lon": -82.75, "tags": {
        "name": "Velocity Air Conditioning", "addr:street": "56th Ct N #604, Clearwater, FL 33760", "craft": "hvac",
        "mobile": "+1 727 555 0100", "contact:facebook": "https://facebook.com/velocityac", "alt_name": "Velocity AC",
        "opening_hours": "Mo-Fr 08:00-17:00"}}
    db.upsert_business(element_to_business(el, "hvac"))
    b = db.get_business(1)
    assert (b["city"], b["phone"]) == ("Clearwater", "+1 727 555 0100")
    db.save_analysis(1, {"website_status": "unknown", "findings": []})
    qualify_business(1)
    lead = TestClient(app).get("/api/leads/1").json()
    rows = {r["field"]: r for r in lead["details"]["fields"]}
    assert rows["alt_names"]["values"][0]["value"] == "Velocity AC"
    assert "facebook" in rows["social"]["values"][0]["value"]
    assert rows["opening_hours"]["values"][0]["sources"] == ["OpenStreetMap"]
    assert rows["postcode"]["values"][0]["value"] == "33760"
    assert lead["source_tags"]["mobile"] == "+1 727 555 0100"
    assert lead["details"]["website"]["state"] == "unknown"
    assert lead["details"]["sources"][0]["url"] == "https://www.openstreetmap.org/node/14"


def test_contributed_fields_capture_all_osm_contact_tags():
    f = contributed_fields({"name": "X", "contact:phone": "813-555-0101;813-555-0102", "contact:url": "x.example",
                            "short_name": "XX", "contact:instagram": "https://instagram.com/x"})
    assert f["phones"] == ["813-555-0101", "813-555-0102"] and f["website"] == "x.example"
    assert f["alt_names"] == ["XX"] and f["social"] == {"instagram": "https://instagram.com/x"}


def test_multiple_sources_agreeing_on_phone_verify_contact(tmpdb):
    db.upsert_business(osm(1))
    db.upsert_business(osm(2, lat=27.9501))   # independent source would normally differ; same source counts once
    db.upsert_business({**osm(3), "source": "google_places_test", "source_id": "p1", "lat": 27.95})
    db.save_analysis(1, {"website_status": "unknown", "findings": [{"code": "_facts", "facts": {}}]})
    from app.qualify import phone_sources
    assert phone_sources(1, "813-555-0101") == 2


# ---------------------------------------------------------------- Sunbiz

def fixed(layout, length, **values):
    line = [" "] * length
    for key, val in values.items():
        start, ln = layout[key]
        line[start - 1:start - 1 + ln] = list(str(val)[:ln].ljust(ln))
    return "".join(line)


def cor(doc, name, date="10012026", status="A", ftype="FLAL", city="TAMPA", state="FL", addr="12 MAIN ST", zip_="33602",
        agent="JANE PRIVATE PERSON", officer="JOHN OFFICER"):
    line = fixed(COR_LAYOUT, COR_LENGTH, doc_number=doc, name=name, status=status, filing_type=ftype, address1=addr,
                 city=city, state=state, zip=zip_, file_date=date)
    line = line[:544] + agent.ljust(42) + line[586:]          # registered agent name (pos 545)
    line = line[:673] + officer.ljust(42) + line[715:]        # officer 1 name (pos 674)
    assert len(line) == COR_LENGTH
    return line


def write(tmp, name, lines):
    p = tmp / name
    p.write_text("\n".join(lines) + "\n", encoding="latin-1")
    return p


def test_cor_parser_reads_official_layout_and_skips_personal_fields():
    rec = parse_record(cor("L26000123456", "ACME PLUMBING LLC"), "sunbiz_cor")
    assert (rec["doc_number"], rec["name"], rec["entity_type"], rec["filing_date"]) == \
           ("L26000123456", "ACME PLUMBING LLC", "Florida LLC", "2026-10-01")
    assert rec["principal_city"] == "Tampa" and rec["principal_zip"] == "33602"
    assert "JANE" not in json.dumps(rec) and "JOHN" not in json.dumps(rec)


def test_fic_parser():
    line = fixed(FIC_LAYOUT, FIC_LENGTH, doc_number="G26000098765", name="SUNSHINE POOL CARE", county="PINELLAS",
                 address1="5 BAY DR", city="LARGO", state="FL", zip="33770", file_date="20261005", status="A")
    rec = parse_record(line, "sunbiz_fic")
    assert (rec["name"], rec["county"], rec["filing_date"], rec["entity_type"]) == \
           ("SUNSHINE POOL CARE", "Pinellas", "2026-10-05", "Fictitious name")


def test_import_rejects_missing_malformed_unsupported_dates(tmpdb):
    p = write(tmpdb, "20261015c.txt", [
        cor("L1", "GOOD HVAC LLC"), cor("L2", "NO DATE LLC", date="        "), cor("L3", "BAD DATE LLC", date="2026-1-1"),
        cor("L4", "FUTURE LLC", date="01012099"), cor("L5", "ZERO LLC", date="00000000"), "short line",
        cor("L6", "CHARITY INC", ftype="DOMNP"), cor("L7", "OLD LLC", status="I"), cor("L8", "OHIO LLC", state="OH"),
    ])
    r = registrations.import_file(p, since="2000-01-01")
    assert (r["status"], r["records"], r["imported"], r["rejected"], r["skipped"]) == ("ok", 9, 1, 5, 3)
    msgs = " ".join(r["errors"])
    assert "missing filing date" in msgs and "malformed" in msgs and "unsupported" in msgs and "shorter" in msgs
    row = db.conn().execute("SELECT doc_number, industry, area FROM registrations").fetchall()
    assert [tuple(x) for x in row] == [("L1", "hvac", "hillsborough")]


def test_imports_are_repeatable_and_incremental(tmpdb):
    d = tmpdb / "sunbiz"
    d.mkdir()
    write(d, "20261014c.txt", [cor("L1", "ACME PLUMBING LLC", date="10142026"), cor("L0", "ANCIENT LLC", date="01012001")])
    first = registrations.import_dir(d, since="2026-01-01")
    assert first[0]["imported"] == 1 and first[0]["skipped"] == 1
    again = registrations.import_dir(d, since="2026-01-01")
    assert again[0]["status"] == "skipped"
    write(d, "20261015c.txt", [cor("L1", "ACME PLUMBING LLC", date="10142026"), cor("L2", "BAY ROOFING LLC", date="10152026")])
    third = registrations.import_dir(d, since="2026-01-01")
    assert [r["status"] for r in third] == ["skipped", "ok"] and third[1]["updated"] == 1 and third[1]["imported"] == 1
    assert db.conn().execute("SELECT COUNT(*) FROM registrations").fetchone()[0] == 2
    forced = registrations.import_file(d / "20261014c.txt", since="2026-01-01", force=True)
    assert forced["updated"] == 1 and db.conn().execute("SELECT COUNT(*) FROM registrations").fetchone()[0] == 2


def test_failed_import_keeps_existing_records(tmpdb, monkeypatch):
    registrations.import_file(write(tmpdb, "20261014c.txt", [cor("L1", "ACME PLUMBING LLC")]), since="2000-01-01")
    calls = {"n": 0}
    real = registrations._upsert

    def flaky(*a):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk went away")
        return real(*a)
    monkeypatch.setattr(registrations, "_upsert", flaky)
    r = registrations.import_file(write(tmpdb, "20261015c.txt", [cor("L2", "B LLC"), cor("L3", "C LLC")]), since="2000-01-01")
    assert r["status"] == "error"
    assert [x[0] for x in db.conn().execute("SELECT doc_number FROM registrations")] == ["L1"]  # nothing partial
    assert registrations.recent_imports()[0]["status"] == "error"
    monkeypatch.setattr(registrations, "_upsert", real)
    assert registrations.import_file(tmpdb / "20261015c.txt", since="2000-01-01")["imported"] == 2  # retry works


def test_registration_matching_is_conservative(tmpdb):
    db.upsert_business(osm(1))                                   # Acme Plumbing, 12 Main St, Tampa
    registrations.import_file(write(tmpdb, "20261014c.txt", [
        cor("L1", "ACME PLUMBING LLC", addr="12 MAIN ST"),         # same name + street -> high
        cor("L2", "ACME PLUMBING, INC.", addr="77 PALM AVE", city="MIAMI", zip_="33101"),  # name only -> low
    ]), since="2000-01-01")
    rows = {r["doc_number"]: r for r in registrations.list_registrations(limit=10)}
    assert rows["L1"]["match_confidence"] == "high" and rows["L2"]["match_confidence"] == "low"
    srcs = [s["source"] for s in db.lead_sources(1)]
    assert srcs == ["osm", "sunbiz_cor"]                         # only the high match became provenance
    assert db.conn().execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 1   # nothing merged or created
    assert "operating_evidence" in rows["L1"]["flags"] and "operating_evidence" not in rows["L2"]["flags"]


def test_new_entity_flags_and_promotion(tmpdb):
    from datetime import date
    today = date.today().strftime("%m%d%Y")
    registrations.import_file(write(tmpdb, "20261016c.txt", [cor("L9", "GULF COAST POOLS LLC", date=today, city="LARGO",
                                                                 zip_="33770", addr="5 BAY DR")]), since="2000-01-01")
    reg = registrations.list_registrations(limit=5)[0]
    assert {"new_entity", "no_confirmed_website", "needs_qualification", "relevant_industry", "in_known_area"} <= set(reg["flags"])
    lead_id = registrations.promote(reg["id"])
    b = db.get_business(lead_id)
    assert (b["name"], b["industry"], b["lat"]) == ("GULF COAST POOLS LLC", "pool", None)
    r = run(analyze_business(b, FakeFetcher(), finders=[]))
    nr = next(f for f in r["findings"] if f["code"] == "new_registration")
    assert nr["confidence"] == "low" and "No evidence yet" in nr["evidence"]
    db.save_analysis(lead_id, r)
    assert qualify_business(lead_id) == 1                         # in Atomic's Pinellas area via the filed city
    assert registrations.promote(reg["id"]) == lead_id            # idempotent


# ---------------------------------------------------------------- Google Places (representative responses)

PLACE = {"id": "ChIJabc", "displayName": {"text": "Acme Plumbing"}, "formattedAddress": "12 Main St, Tampa, FL 33602, USA",
         "location": {"latitude": 27.9501, "longitude": -82.4601}, "nationalPhoneNumber": "(813) 555-0101",
         "websiteUri": SITE, "businessStatus": "OPERATIONAL"}


def places(handler):
    return GooglePlacesFinder(transport=httpx.MockTransport(handler))


def test_places_request_uses_field_mask_and_matches_by_phone(tmpdb, monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_PLACES_API_KEY", "test-key")
    seen = {}

    def handler(request):
        seen["mask"], seen["key"] = request.headers["X-Goog-FieldMask"], request.headers["X-Goog-Api-Key"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"places": [PLACE, {**PLACE, "id": "other", "displayName": {"text": "Plumbing"},
                                                            "nationalPhoneNumber": "(305) 000-0000", "location": None,
                                                            "formattedAddress": "Miami"}]})
    profile = {"name": "Acme Plumbing", "names": ["Acme Plumbing"], "phones": ["8135550101"], "cities": ["Tampa"],
               "city": "Tampa", "lat": 27.95, "lon": -82.46}
    out = run(places(handler).find_candidates(profile))
    assert seen["mask"] == FIELD_MASK and "places.websiteUri" in seen["mask"] and seen["key"] == "test-key"
    assert seen["body"]["locationBias"]["circle"]["radius"] == 3000.0
    assert [c["source_record_id"] for c in out["candidates"]] == ["ChIJabc"]
    assert out["candidates"][0]["base_identity"] == "high"
    assert db.api_calls("google_places", __import__("datetime").datetime.utcnow().strftime("%Y-%m")) == 1


@pytest.mark.parametrize("status,body,match", [
    (403, {"error": {"code": 403, "message": "This API project is not authorized", "status": "PERMISSION_DENIED"}}, "billing"),
    (429, {"error": {"code": 429, "message": "Quota exceeded", "status": "RESOURCE_EXHAUSTED"}}, "quota"),
    (400, {"error": {"code": 400, "message": "Invalid field mask", "status": "INVALID_ARGUMENT"}}, "Invalid field mask"),
])
def test_places_errors_are_source_errors(tmpdb, monkeypatch, status, body, match):
    monkeypatch.setattr(config, "GOOGLE_PLACES_API_KEY", "k")
    with pytest.raises(SourceError, match=match):
        run(places(lambda r: httpx.Response(status, json=body)).find_candidates(
            {"name": "A", "names": ["A"], "phones": [], "cities": [], "city": None}))


def test_places_monthly_cap_and_off_by_default(tmpdb, monkeypatch):
    from app.discovery.brave import get_finders
    monkeypatch.setattr(config, "GOOGLE_PLACES_API_KEY", "")
    monkeypatch.setattr(config, "BRAVE_API_KEY", "")
    assert get_finders() == []
    monkeypatch.setattr(config, "GOOGLE_PLACES_API_KEY", "k")
    monkeypatch.setattr(config, "GOOGLE_PLACES_MONTHLY_LIMIT", 0)
    called = []
    with pytest.raises(SourceError, match="monthly limit"):
        run(places(lambda r: called.append(1) or httpx.Response(200, json={})).find_candidates(
            {"name": "A", "names": ["A"], "phones": [], "cities": [], "city": None}))
    assert called == []


def test_unconfirmed_places_website_is_not_stored(tmpdb):
    db.upsert_business(osm(1))
    finder = FakeFinder("google_places", [{"url": SITE, "source": "google_places", "source_record_id": "ChIJabc",
                                           "evidence": ["same phone"], "base_identity": "high", "transient": True}])
    run(verify_website(db.get_business(1), FakeFetcher({SITE: ok(SITE, "<p>nothing identifying</p>")}), [finder]))
    stored = db.website_candidates(1)
    assert [c["url"] for c in stored] == ["https://www.google.com/maps/place/?q=place_id:ChIJabc"]
    assert db.get_business(1)["website"] is None
    run(verify_website(db.get_business(1), FakeFetcher({SITE: ok(SITE, IDENTIFYING)}), [finder]))
    assert db.get_business(1)["website"] == SITE  # our own check confirmed it, so the URL may be kept


def test_outage_is_verified_only_across_audits(tmpdb):
    db.upsert_business(osm(1, tags={"website": SITE}))
    down = FetchResult(url=SITE, ok=False, error="timeout", error_detail="No response within 15s")
    first = run(analyze_business(db.get_business(1), FakeFetcher({SITE: down}), finders=[]))
    f1 = next(f for f in first["findings"] if f["code"] == "site_timeout")
    assert f1["confidence"] == "medium" and "First audit" in f1["evidence"]
    db.save_analysis(1, first)
    second = run(analyze_business(db.get_business(1), FakeFetcher({SITE: down}), finders=[]))
    f2 = next(f for f in second["findings"] if f["code"] == "site_timeout")
    assert f2["confidence"] == "high" and "previous audit" in f2["evidence"]
    assert score_lead({"name": "x", "industry": "plumbing", "phone": "1"}, second["findings"],
                      facts={"website_state": "unreachable"})["score"] > \
           score_lead({"name": "x", "industry": "plumbing", "phone": "1"}, first["findings"],
                      facts={"website_state": "unreachable"})["score"]
