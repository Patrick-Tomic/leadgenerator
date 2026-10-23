"""v0.6: statewide discovery from OSM extracts, all business types, home-market priority."""
import asyncio

import osmium
import pytest

from app import config, db, pipeline, registrations
from app.discovery.osm_extract import classify, compile_selector, read_businesses
from app.profiles import default_profile, validate
from app.scoring import exclusion_reason


@pytest.fixture
def tmpdb(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "backups")
    db.use(tmp_path / "leads.db")
    yield tmp_path
    db._conn.close()
    db._conn = None


# ---------------------------------------------------------------- categories

def test_selector_compiler_matches_overpass_semantics():
    sel = compile_selector('["name"~"plumbing",i]["amenity"!~"."]')
    assert sel({"name": "Ace PLUMBING"}) and not sel({"name": "Ace Plumbing", "amenity": "x"}) and not sel({"name": "x"})
    assert compile_selector('["shop"]')({"shop": "gift"}) and not compile_selector('["shop"]')({"office": "x"})
    assert compile_selector('["amenity"~"^(cafe|ice_cream)$"]')({"amenity": "cafe"})


@pytest.mark.parametrize("tags,expected", [
    ({"craft": "hvac", "name": "X"}, "hvac"),
    ({"amenity": "restaurant"}, "restaurant"),
    ({"office": "insurance"}, "insurance"),
    ({"amenity": "bank"}, "bank_finance"),
    ({"shop": "hairdresser"}, "beauty_salon"),
    ({"shop": "swimming_pool"}, "pool"),          # specific trade wins over the retail catch-all
    ({"shop": "gift"}, "retail"),                 # any other shop
    ({"office": "it"}, "professional_office"),    # any other office
    ({"craft": "sculptor"}, "trades_other"),
    ({"tourism": "museum"}, None),                # not a business category
])
def test_classify_any_business_type(tags, expected):
    assert classify(tags) == expected


# ---------------------------------------------------------------- extract reader (real .osm.pbf, no network)

def make_pbf(path):
    w = osmium.SimpleWriter(str(path))
    nid = 1
    def node(lat, lon, tags=None):
        nonlocal nid
        w.add_node(osmium.osm.mutable.Node(id=nid, location=osmium.osm.Location(lon, lat), tags=tags or {}))
        nid += 1
        return nid - 1
    node(27.95, -82.46, {"name": "Joe's Grill", "amenity": "restaurant", "phone": "+1 813 555 0101", "website": "joesgrill.example"})
    node(27.96, -82.45, {"name": "Bay Insurance", "office": "insurance"})
    node(27.97, -82.44, {"name": "Brand Burger", "amenity": "fast_food", "brand": "Brand Burger"})   # brand-tagged chain
    for i in range(5):                                                                              # unbranded chain
        node(28.0 + i / 100, -82.3, {"name": "Quick Coffee", "amenity": "cafe"})
    node(27.98, -82.43, {"name": "No Category", "tourism": "museum"})
    node(27.99, -82.42, {"amenity": "restaurant"})                                                  # no name
    outline = [node(28.10, -82.50), node(28.10, -82.49), node(28.11, -82.49), node(28.10, -82.50)]
    w.add_way(osmium.osm.mutable.Way(id=10, nodes=outline, tags={"name": "Sunrise Dental", "amenity": "dentist"}))
    w.close()
    return path


def test_extract_reader_end_to_end(tmp_path):
    out = {b["name"]: b for b in read_businesses(make_pbf(tmp_path / "test.osm.pbf"), chain_min=5)}
    assert set(out) == {"Joe's Grill", "Bay Insurance", "Sunrise Dental"}       # chains, unnamed, non-business skipped
    assert out["Joe's Grill"]["industry"] == "restaurant" and out["Joe's Grill"]["phone"] == "+1 813 555 0101"
    assert out["Joe's Grill"]["source_id"] == "node/1"                          # same ids as the Overpass adapter
    dental = out["Sunrise Dental"]
    assert dental["source_id"] == "way/10" and abs(dental["lat"] - 28.10) < 0.02 and dental["industry"] == "dental"


# ---------------------------------------------------------------- planning & scope

def state_profile(**kw):
    return validate({**default_profile(), "areas": ["us-fl", "us-ga"], **kw})


def test_one_extract_scan_per_state_and_no_overpass(tmpdb, monkeypatch):
    monkeypatch.setattr(pipeline, "ADAPTERS", [])
    plan = pipeline.plan_scans(state_profile())
    assert [(s["adapter"].name, s["area"], s["industry"], s["mode"]) for s in plan] == \
           [("osm_extract", "us-fl", "*", "extract"), ("osm_extract", "us-ga", "*", "extract")]
    assert asyncio.run(pipeline.run_scan(plan[0]))["status"] == "ok"
    assert [s["area"] for s in pipeline.plan_scans(state_profile())] == ["us-ga"]   # fresh for EXTRACT_REFRESH_DAYS


def test_bulk_import_through_run_scan(tmpdb, no_real_extracts, tmp_path):
    businesses = list(read_businesses(make_pbf(tmp_path / "t.osm.pbf"), chain_min=5))
    async def discover(industry, area, since=None):
        return businesses
    no_real_extracts.discover = discover
    r = asyncio.run(pipeline.run_scan({"adapter": no_real_extracts, "area": "us-fl", "industry": "*", "mode": "extract",
                                       "since": None}))
    assert (r["status"], r["added"]) == ("ok", 3)
    assert db.lead_scope(1)[1] == {"us-fl"}
    again = asyncio.run(pipeline.run_scan({"adapter": no_real_extracts, "area": "us-fl", "industry": "*", "mode": "extract",
                                           "since": None}))
    assert again["added"] == 0                                                    # re-reading the state adds no duplicates


def test_state_scope_uses_scan_area_not_overlapping_boxes():
    p = state_profile(areas=["us-ga"])
    # Inside Florida's (approximate) box AND Georgia's: only the scan area decides.
    lead = {"name": "X", "industry": "restaurant", "lat": 30.6, "lon": -84.0}
    assert exclusion_reason(lead, p, {"us-fl"}) == "outside service areas"
    assert exclusion_reason(lead, p, {"us-ga"}) is None
    # A Tampa lead found by a county scan belongs to Florida.
    assert exclusion_reason({**lead, "lat": 27.95, "lon": -82.46}, state_profile(areas=["us-fl"]), {"hillsborough"}) is None
    # County customers keep the exact location check.
    county = state_profile(areas=["pinellas"])
    assert exclusion_reason({**lead, "lat": 27.95, "lon": -82.46}, county, {"pinellas"}) == "outside service areas"


def test_priority_areas_are_checked_first(tmpdb, monkeypatch):
    monkeypatch.setattr(config, "PRIORITY_AREAS", ["hillsborough"])
    from test_v04 import osm
    far = osm(1, tags={"name": "Miami Grill", "phone": "3055550000", "website": "miami.example"}, lat=25.77, lon=-80.19)
    near = osm(2, tags={"name": "Tampa Grill", "phone": "8135550000", "website": "tampa.example"}, lat=27.95, lon=-82.46)
    db.upsert_business(far)
    db.upsert_business(near)
    assert [b["name"] for b in db.unanalyzed(2)] == ["Tampa Grill", "Miami Grill"]


def test_registrations_in_scope_statewide(tmpdb):
    from test_v04 import cor, write
    registrations.import_file(write(tmpdb, "20261014c.txt", [
        cor("L1", "GULF BREEZE GRILL LLC", city="TALLAHASSEE", zip_="32301"),   # city not in AREA_CITIES -> Florida
        cor("L2", "BAYSIDE PLUMBING LLC"),                                       # Tampa -> Hillsborough (in Florida)
    ]), since="2000-01-01")
    rows = {r["doc_number"]: r for r in registrations.list_registrations(state_profile(areas=["us-fl"]), 10)}
    assert set(rows) == {"L1", "L2"} and rows["L1"]["area"] == "us-fl" and rows["L1"]["industry"] == "restaurant"
    county_only = registrations.list_registrations(state_profile(areas=["hillsborough"]), 10)
    assert [r["doc_number"] for r in county_only] == ["L2"]


def test_no_priority_by_default_and_extracts_load_before_checks(tmpdb, no_real_extracts, monkeypatch):
    from test_v04 import osm
    assert config.PRIORITY_AREAS == []
    db.upsert_business(osm(1, tags={"website": "a.example"}, lat=27.95, lon=-82.46))   # Tampa
    db.refresh_priorities()
    assert db.get_business(1)["priority"] == 1                                          # no Tampa bump
    # With an unchecked business waiting, a due state extract still runs first.
    db.sync_customer(state_profile(areas=["us-fl", "us-ga"], ready_pool_target=1, min_score=99))
    monkeypatch.setattr(pipeline, "ADAPTERS", [])
    checked = []
    async def fake_analyze(limit):
        checked.append(limit)
        db.conn().execute("UPDATE leads SET analyzed_at='x', audit_version=99")
        return 1
    monkeypatch.setattr(pipeline, "analyze_pending", fake_analyze)
    asyncio.run(pipeline.refill("atomic"))
    assert no_real_extracts.calls == ["us-fl", "us-ga"] and checked                  # both states loaded, then checks
