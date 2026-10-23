"""v0.12: owner names from Sunbiz and emails for businesses without websites."""
import asyncio
import json
import zipfile

import httpx
import pytest

from app import config, db, emails, noweb, owners
from app.discovery.base import SourceError
from app.discovery.sunbiz import COR_LAYOUT, COR_LENGTH, FIC_LAYOUT, FIC_LENGTH


@pytest.fixture
def tmpdb(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "backups")
    db.use(tmp_path / "leads.db")
    yield tmp_path
    db._conn.close()
    db._conn = None


def put(line, start, text, length):
    return line[:start - 1] + str(text)[:length].ljust(length) + line[start - 1 + length:]


def cor_line(doc, name, addr="12 MAIN ST", zip_="33602", officers=()):
    line = " " * COR_LENGTH
    for key, val in {"doc_number": doc, "name": name, "status": "A", "filing_type": "FLAL", "address1": addr,
                     "city": "TAMPA", "state": "FL", "zip": zip_}.items():
        start, ln = COR_LAYOUT[key]
        line = put(line, start, val, ln)
    for i, (title, kind, pname, paddr) in enumerate(officers):
        start = owners.COR_OFFICERS[i]
        line = put(line, start, title, 4)
        line = put(line, start + 4, kind, 1)
        line = put(line, start + 5, pname, 42)
        line = put(line, start + 47, paddr, 42)
    return line


def fic_line(doc, name, addr="5 BAY DR", zip_="33770", owner=None):
    line = " " * FIC_LENGTH
    for key, val in {"doc_number": doc, "name": name, "address1": addr, "city": "LARGO", "state": "FL", "zip": zip_,
                     "status": "A"}.items():
        start, ln = FIC_LAYOUT[key]
        line = put(line, start, val, ln)
    if owner:
        start = owners.FIC_OWNERS[0]
        line = put(line, start + 12, owner, 55)
        line = put(line, start + 67, "P", 1)
    return line


def osm_business(i, name, tags=None, lat=27.95):
    from app.discovery.osm import element_to_business
    t = {"name": name, "amenity": "restaurant", "addr:city": "Tampa", **(tags or {})}
    b = element_to_business({"type": "node", "id": i, "lat": lat, "lon": -82.46, "tags": t}, "restaurant")
    db.upsert_business(b, area="us-fl")
    return db.conn().execute("SELECT lead_id FROM lead_sources WHERE source_id=?", (f"node/{i}",)).fetchone()[0]


# ---------------------------------------------------------------- owners

def test_owner_names_matched_by_name_and_address_only(tmpdb):
    a = osm_business(1, "Joe's Grill", {"addr:housenumber": "12", "addr:street": "Main St", "addr:postcode": "33602"})
    b = osm_business(2, "Gulf Tacos", {"addr:postcode": "33609"}, lat=27.96)
    c = osm_business(3, "Bay Sushi", {"addr:postcode": "33611"}, lat=27.97)
    p = tmpdb / "cordata0.txt"
    p.write_text("\n".join([
        cor_line("L1", "JOE'S GRILL LLC", officers=[("MGR", "P", "SMITH, JOHN A", "99 HOME LN"), ("AMBR", "C", "HOLDCO LLC", "x")]),
        cor_line("L2", "GULF TACOS, INC.", addr="1 OTHER RD", zip_="33609", officers=[("P", "P", "GARCIA, MARIA", "8 HOME CT")]),
        cor_line("L3", "BAY SUSHI LLC", addr="1 FAR AWAY", zip_="32801", officers=[("MGR", "P", "TANAKA, KEN", "x")]),
    ]) + "\n", encoding="latin-1")
    r = owners.import_owners(p)
    assert r["businesses"] == 2
    assert [(x["name"], x["title"], x["match_confidence"]) for x in owners.people_for(a)] == [("John A Smith", "Manager", "high")]
    assert [(x["name"], x["match_confidence"]) for x in owners.people_for(b)] == [("Maria Garcia", "medium")]
    assert owners.people_for(c) == []                                   # same name, different ZIP: not matched
    dump = json.dumps([list(row) for row in db.conn().execute("SELECT * FROM business_people")])
    assert "HOME" not in dump and "HOLDCO" not in dump                  # no addresses, no company officers
    assert owners.import_owners(p)["status"] == "skipped"                # same version: not re-read


def test_dba_owner_from_zipped_fictitious_name_file(tmpdb):
    lid = osm_business(1, "Sunshine Pool Care", {"addr:postcode": "33770"})
    z = tmpdb / "ficdata.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("ficdata.txt", fic_line("G1", "SUNSHINE POOL CARE", owner="DOE, JANE") + "\n")
    owners.import_owners(z)
    assert [(p["name"], p["title"]) for p in owners.people_for(lid)] == [("Jane Doe", "Owner (DBA filing)")]


# ---------------------------------------------------------------- emails

LEAD = {"id": None, "name": "Joe's Grill", "city": "Tampa", "phone": "+1 813-555-0101"}


def test_email_kept_only_when_snippet_identifies_the_business():
    results = [
        {"url": "https://www.yelp.com/biz/joes-grill-tampa", "title": "Joe's Grill - Tampa",
         "content": "Joe's Grill (813) 555-0101 · contact joesgrilltampa@gmail.com · support@yelp.com"},
        {"url": "https://www.facebook.com/joesgrill", "title": "Joe's Grill", "content": "Tampa, FL · joes.grill@outlook.com"},
        {"url": "https://example.org/other", "title": "Joe's Grill Ohio", "content": "Columbus · joe@ohiogrill.com"},
        {"url": "https://example.org/x", "title": "Pizza Place", "content": "Tampa (813) 555-0101 pizza@place.com"},
    ]
    found = {e["email"]: e for e in emails.extract(LEAD, results)}
    assert set(found) == {"joesgrilltampa@gmail.com", "joes.grill@outlook.com"}
    assert found["joesgrilltampa@gmail.com"]["confidence"] == "high" and "Yelp" in found["joesgrilltampa@gmail.com"]["evidence"]
    assert found["joes.grill@outlook.com"]["confidence"] == "medium"


def test_find_email_stores_and_sets_contact_only_when_confident(tmpdb, monkeypatch):
    lid = osm_business(1, "Joe's Grill", {"phone": "+1 813-555-0101"})
    monkeypatch.setattr(config, "TAVILY_API_KEY", "tvly-x")
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"results": [{"url": "https://www.bbb.org/x", "title": "Joe's Grill",
                                                      "content": "Joe's Grill, Tampa. Call (813) 555-0101. joe@joesgrill.net"}]})
    found = asyncio.run(emails.find(db.get_business(lid), transport=httpx.MockTransport(handler)))
    assert [e["email"] for e in found] == ["joe@joesgrill.net"] and "email" in seen[0]["query"]
    assert db.get_business(lid)["email"] == "joe@joesgrill.net"
    assert emails.emails_for(lid)[0]["evidence"].startswith("in a BBB search snippet")
    monkeypatch.setattr(config, "TAVILY_API_KEY", "")
    with pytest.raises(SourceError, match="TAVILY_API_KEY"):
        asyncio.run(emails.find(db.get_business(lid)))


# ---------------------------------------------------------------- tab & email draft

def test_no_web_tab_shows_owner_and_email_and_filters(tmpdb):
    a = osm_business(1, "Joe's Grill", {"phone": "813-555-0101"})
    b = osm_business(2, "Quiet Cafe", {"phone": "813-555-0102"}, lat=27.96)
    c = osm_business(3, "Mail Diner", {}, lat=27.97)
    db.conn().execute("""INSERT INTO business_people (lead_id, name, title, rank, source, source_record_id, match_confidence,
                         first_seen_at, last_seen_at) VALUES (?, 'John Smith', 'Manager', 0, 'sunbiz_cor', 'L1', 'high', 'x', 'x')""", (a,))
    db.conn().commit()
    emails.save(c, [{"email": "mail@diner.com", "confidence": "medium", "evidence": "e"}], "search:tavily")
    cust = db.get_customer("atomic")
    rows = noweb.query(cust)["rows"]
    assert [r["name"] for r in rows] == ["Mail Diner", "Joe's Grill", "Quiet Cafe"]         # email, then phone + owner, then phone
    assert rows[1]["owners"][0]["name"] == "John Smith" and rows[0]["emails"][0]["email"] == "mail@diner.com"
    assert [r["name"] for r in noweb.query(cust, has_owner=True)["rows"]] == ["Joe's Grill"]
    assert [r["name"] for r in noweb.query(cust, has_email=True)["rows"]] == ["Mail Diner"]


def test_email_draft_greets_the_owner():
    from app.outreach import draft
    lead = {"id": 1, "name": "Joe's Grill", "findings": [], "explanation": {}, "email": "a@b.com",
            "owners": [{"name": "John A Smith", "title": "Manager"}]}
    d = draft(lead, {"name": "Patrick", "company": "Atomic"})
    assert d["body"].startswith("Hi John,") and any("Sunbiz" in w for w in d["warnings"])


def test_name_plus_city_is_low_and_only_for_distinctive_unique_names(tmpdb):
    a = osm_business(1, "Niceville Family Dental", {"addr:city": "Tampa"})
    b = osm_business(2, "Best Pizza", {"addr:city": "Tampa"}, lat=27.96)                 # not distinctive enough
    p = tmpdb / "20261014c.txt"
    p.write_text("\n".join([
        cor_line("L1", "NICEVILLE FAMILY DENTAL LLC", addr="1 ELSEWHERE", zip_="00000", officers=[("MGR", "P", "LEE, ANN", "x")]),
        cor_line("L2", "BEST PIZZA LLC", addr="1 ELSEWHERE", zip_="00000", officers=[("MGR", "P", "ROE, BOB", "x")]),
    ]) + "\n", encoding="latin-1")
    owners.import_owners(p)
    assert [(x["name"], x["match_confidence"]) for x in owners.people_for(a)] == [("Ann Lee", "low")]
    assert owners.people_for(b) == []
    from app.outreach import draft
    d = draft({"id": a, "name": "Niceville Family Dental", "findings": [], "explanation": {}, "email": "x@y.com",
               "owners": [dict(owners.people_for(a)[0])]}, {"name": "Patrick", "company": "Atomic"})
    assert d["body"].startswith("Hi Niceville Family Dental team,")                     # low match never used to greet


def test_zip_members_python_cannot_read_are_streamed_through_unzip(tmpdb, monkeypatch):
    lid = osm_business(1, "Sunshine Pool Care", {"addr:postcode": "33770"})
    z = tmpdb / "ficdata.zip"
    with zipfile.ZipFile(z, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("ficdata.txt", fic_line("G1", "SUNSHINE POOL CARE", owner="DOE, JANE") + "\n")
    monkeypatch.setattr(owners, "_PY_ZIP_METHODS", set())        # pretend Python can't decompress it (like Deflate64)
    owners.import_owners(z)
    assert [p["name"] for p in owners.people_for(lid)] == ["Jane Doe"]


def test_names_parsed_from_fixed_subfields():
    assert owners.display_name("KELLEY              JENNIFER      D") == "Jennifer D Kelley"
    assert owners.display_name("DIAZ RODRIGUEZ      MARIA         L") == "Maria L Diaz Rodriguez"
    assert owners.display_name("VALCIN              KARL-ERIC") == "Karl-Eric Valcin"
    assert owners.display_name("SMITH, JOHN A") == "John A Smith"
    assert owners.TITLES["PD"][0] == "President & director"
