"""Florida Division of Corporations ("Sunbiz") registrations from the official data files.

Access (verified against dos.fl.gov, Oct 2026): the Division publishes free daily and
quarterly fixed-width text files "for informational purposes" on a public SFTP server
(sftp.floridados.gov; login details on https://dos.fl.gov/sunbiz/other-services/data-downloads/).
There is no documented API, and the public search website is not scraped. Daily file names:
  doc/cor/yyyymmddc.txt  corporations, LLCs, LPs (record length 1440)
  doc/fic/yyyymmddf.txt  fictitious-name registrations (record length 2098)
Layouts: https://dos.sunbiz.org/data-definitions/cor.html and .../fic.html.

Workflow: download the daily files yourself (any SFTP client) into SUNBIZ_IMPORT_DIR and
run the import. Files already imported are skipped, so re-running is safe.

Privacy: registered-agent and officer/owner blocks are never parsed or stored. A
registration's principal address is stored as filed and is not treated as a confirmed
operating location. A registration is a legal entity, not a verified operating business.
"""
import re
from datetime import date, datetime
from pathlib import Path

from .. import config

# (start, length) from the official definitions; positions are 1-based.
COR_LAYOUT = {
    "doc_number": (1, 12), "name": (13, 192), "status": (205, 1), "filing_type": (206, 15),
    "address1": (221, 42), "address2": (263, 42), "city": (305, 28), "state": (333, 2), "zip": (335, 10),
    "country": (345, 2), "file_date": (473, 8), "last_transaction_date": (496, 8),
}
COR_LENGTH = 1440
FIC_LAYOUT = {
    "doc_number": (1, 12), "name": (13, 192), "county": (205, 12), "address1": (217, 40), "address2": (257, 40),
    "city": (297, 28), "state": (325, 2), "zip": (327, 10), "country": (337, 2), "file_date": (339, 8),
    "status": (352, 1), "cancel_date": (353, 8), "expiration_date": (361, 8),
}
FIC_LENGTH = 2098
KINDS = {
    "sunbiz_cor": {"layout": COR_LAYOUT, "length": COR_LENGTH, "min_length": 503, "pattern": re.compile(r"(\d{8})c\.txt$", re.I)},
    "sunbiz_fic": {"layout": FIC_LAYOUT, "length": FIC_LENGTH, "min_length": 368, "pattern": re.compile(r"(\d{8})f\.txt$", re.I)},
}
# Filing types that are operating businesses (excludes non-profits, trusts, agent designations).
BUSINESS_FILING_TYPES = {"DOMP": "Florida corporation", "FLAL": "Florida LLC", "FORP": "Foreign corporation",
                         "FORL": "Foreign LLC", "DOMLP": "Florida limited partnership", "FORLP": "Foreign limited partnership"}

# Name keywords for inferring a category from a registration name (OSM tags aren't available here).
INDUSTRY_KEYWORDS = {
    "hvac": r"air condition|\bhvac\b|heating|cooling|\ba/?c\b",
    "plumbing": r"plumb|rooter|drain",
    "electrical": r"electric",
    "roofing": r"roof",
    "general_contractor": r"construction|contracting|builders?\b|remodel|renovat|carpentry",
    "pool": r"\bpools?\b",
    "pressure_washing": r"pressure wash|power wash|soft wash",
    "landscaping": r"landscap|lawn|tree (service|care|trimming)",
    "auto_repair": r"auto (repair|service)|automotive|mechanic|tire",
    "cleaning": r"cleaning|maid|janitor",
    "pest_control": r"pest|termite|extermin",
    "locksmith": r"locksmith",
    "handyman": r"handyman",
    "painting": r"painting|painters?\b",
    "flooring": r"floor",
    "fencing": r"fenc(e|ing)",
    "screen_enclosures": r"screen|enclosure|aluminum",
    "pavers_concrete": r"paver|paving|concrete",
    "irrigation": r"irrigation|sprinkler",
    "garage_doors": r"garage door",
    "appliance_repair": r"appliance",
    "moving": r"moving|movers",
    "towing": r"towing|wrecker",
    "auto_detailing": r"detailing",
    "solar": r"solar",
}
SEARCH_URL = "https://search.sunbiz.org/Inquiry/CorporationSearch/ByName"


class RecordError(ValueError):
    pass


def parse_date(raw: str) -> tuple[str | None, str | None]:
    """The definitions don't state a date format, so accept MMDDYYYY or YYYYMMDD when exactly
    one reading is a real date between 1900 and next year. Returns (ISO date, error)."""
    raw = (raw or "").strip()
    if not raw or set(raw) <= {"0"}:
        return None, "missing filing date"
    if not re.fullmatch(r"\d{8}", raw):
        return None, f"malformed filing date {raw!r}"
    readings = []
    for fmt in ("%m%d%Y", "%Y%m%d"):
        try:
            d = datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
        if 1900 <= d.year <= date.today().year + 1:
            readings.append(d)
    if not readings:
        return None, f"unsupported filing date {raw!r}"
    if len(set(readings)) > 1:
        return None, f"ambiguous filing date {raw!r}"
    return readings[0].isoformat(), None


def _field(line: str, start: int, length: int) -> str:
    return line[start - 1:start - 1 + length].strip()


def parse_record(line: str, kind: str) -> dict:
    """One fixed-width line -> registration dict. Raises RecordError for misaligned rows."""
    spec = KINDS[kind]
    line = line.rstrip("\r\n")
    if len(line) > spec["length"]:
        raise RecordError(f"row longer than {spec['length']} characters (misaligned or embedded line break)")
    if len(line) < spec["min_length"]:
        raise RecordError(f"row shorter than {spec['min_length']} characters (truncated)")
    line = line.ljust(spec["length"])
    f = {k: _field(line, *pos) for k, pos in spec["layout"].items()}
    if not f["doc_number"] or not f["name"]:
        raise RecordError("missing document number or name")
    filing_date, err = parse_date(f["file_date"])
    address = ", ".join(x for x in (f["address1"], f["address2"]) if x) or None
    rec = {
        "source": kind, "doc_number": f["doc_number"], "name": re.sub(r"\s+", " ", f["name"]),
        "status": f["status"], "filing_date": filing_date, "filing_date_raw": f["file_date"], "date_error": err,
        "principal_address": address, "principal_city": f["city"].title() or None, "principal_state": f["state"] or None,
        "principal_zip": (f["zip"][:5] or None), "county": f.get("county", "").title() or None,
        "entity_type": BUSINESS_FILING_TYPES.get(f.get("filing_type", ""), f.get("filing_type") or "Fictitious name"),
        "filing_type": f.get("filing_type"),
    }
    return rec


def exclusion(rec: dict) -> str | None:
    """Why a parsed record isn't imported as a potential business, or None."""
    if rec["status"] != "A":
        return "not active"
    if rec["source"] == "sunbiz_cor" and rec["filing_type"] not in BUSINESS_FILING_TYPES:
        return f"filing type {rec['filing_type'] or 'blank'} is not a for-profit business"
    if rec["principal_state"] and rec["principal_state"] != "FL":
        return "principal address outside Florida"
    return None


def infer_industry(name: str) -> str | None:
    for key, pattern in INDUSTRY_KEYWORDS.items():
        if re.search(pattern, name, re.I):
            return key
    return None


def _city_key(city: str | None) -> str:
    c = re.sub(r"[^a-z ]", "", (city or "").lower().replace("saint ", "st ").replace("st. ", "st "))
    return re.sub(r"\s+", " ", c).strip()


def infer_area(city: str | None) -> str | None:
    key = _city_key(city)
    for area, cities in config.AREA_CITIES.items():
        if key and key in {_city_key(c) for c in cities}:
            return area
    return None


def file_kind_and_date(path: Path) -> tuple[str | None, str | None]:
    for kind, spec in KINDS.items():
        m = spec["pattern"].search(path.name)
        if m:
            d, _ = parse_date(m.group(1))
            return kind, d
    return None, None
