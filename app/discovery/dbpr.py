"""Florida DBPR construction licenses (Construction Industry Licensing Board, board 06).

Source: https://www2.myfloridalicense.com/construction-industry/public-records/
File: CONSTRUCTIONLICENSE_1.csv, a public record (Chapter 119, F.S.), offered as a free
download and refreshed weekly. Quote/comma-delimited ASCII, 22 columns:
Board, Occupation Code, Licensee Name, DBA Name, Class Code, Address 1-3, City, State, Zip,
County Code, License Number, Primary Status, Secondary Status, Original Licensure Date,
Effective Date, Expiration Date, (blank), Renewal Period, Alternate Lic#, (trailing).

Use here: evidence that a company holds a current state contractor license, which
signals an operating business. The file has no phone numbers. Electrical contractors are
licensed by a different board and aren't in this file.

Privacy: many licensees are individuals ("LAST, FIRST"). For those, only the business
(DBA) name, city, ZIP, license type and number are kept, never the person's name or
street address. Financially-responsible-officer rows (FRO) are skipped entirely.
"""
import hashlib
import re
from datetime import datetime

from ..dedup import normalize_name

COLUMNS = ["board", "occupation", "licensee", "dba", "class", "addr1", "addr2", "addr3", "city", "state", "zip",
           "county", "license_number", "primary_status", "secondary_status", "original_date", "effective_date",
           "expiration_date", "blank", "renewal_period", "alt_license", "trailing"]
OCCUPATIONS = {
    "QB": "Qualified business (company license)", "CGC": "Certified general contractor", "CBC": "Certified building contractor",
    "CRC": "Certified residential contractor", "CCC": "Certified roofing contractor", "CAC": "Certified air conditioning contractor",
    "CMC": "Certified mechanical contractor", "CFC": "Certified plumbing contractor", "CPC": "Certified pool/spa contractor",
    "CUC": "Certified underground utility contractor", "CVC": "Certified solar contractor",
    "SCC": "Certified specialty contractor", "RG": "Registered general contractor", "RB": "Registered building contractor",
    "RR": "Registered residential contractor", "RC": "Registered roofing contractor", "RA": "Registered air conditioning contractor",
    "RM": "Registered mechanical contractor", "RF": "Registered plumbing contractor", "RP": "Registered pool/spa contractor",
}
SKIP_OCCUPATIONS = {"FRO"}
# Values seen in the DBA column that aren't business names.
PLACEHOLDER_DBA = re.compile(r"^\s*(individual|individually|none|n/?a|self|same|sole proprietor|-+)?\s*$", re.I)  # financially responsible officers: people, not businesses
BUSINESS_WORDS = re.compile(
    r"\b(llc|l\.?l\.?c|inc|incorporated|corp|corporation|co|company|companies|group|enterprises?|services?|construction|"
    r"contractors?|contracting|builders?|homes?|roofing|pools?|air|heating|cooling|mechanical|plumbing|electric|"
    r"solutions|systems|industries|development|restoration|remodel\w*|partners|associates|lp|llp|pa|holdings)\b", re.I)


def is_individual(name: str) -> bool:
    """'LAST, FIRST M' with no business words is a person."""
    return "," in name and not BUSINESS_WORDS.search(name)


def parse_date(raw: str) -> str | None:
    try:
        return datetime.strptime(raw.strip(), "%m/%d/%Y").date().isoformat()
    except ValueError:
        return None


def parse_row(row: list[str]) -> dict | None:
    """One CSV row -> license record, or None if it isn't useful or isn't a business."""
    if len(row) < 18:
        return None
    r = dict(zip(COLUMNS, [x.strip() for x in row]))
    if r["occupation"] in SKIP_OCCUPATIONS:
        return None
    individual = is_individual(r["licensee"])
    business_name = None if individual else (r["licensee"] or None)
    dba = None if PLACEHOLDER_DBA.match(r["dba"]) else r["dba"]
    if not business_name and not dba:
        return None  # an individual with no business name: nothing to match, and personal data
    key_src = "|".join([r["occupation"], r["license_number"] or "", business_name or "", dba or "",
                        "" if individual else r["addr1"], r["original_date"]])
    return {
        "source": "dbpr_cilb",
        "record_key": hashlib.sha1(key_src.upper().encode()).hexdigest()[:20],
        "occupation_code": r["occupation"], "license_number": r["alt_license"] or r["license_number"] or None,
        "business_name": business_name, "dba_name": dba,
        "norm_name": normalize_name(business_name) if business_name else None,
        "norm_dba": normalize_name(dba) if dba else None,
        "is_individual": int(individual),
        "address": None if individual else (", ".join(x for x in (r["addr1"], r["addr2"], r["addr3"]) if x) or None),
        "city": r["city"].title() or None, "state": r["state"] or None, "zip": (r["zip"][:5] or None),
        "primary_status": r["primary_status"] or None, "secondary_status": r["secondary_status"] or None,
        # Codes aren't documented on the download page: we treat primary C ("current") without a
        # secondary I ("inactive") as active. DBPR excludes null-and-void and delinquent records.
        "active": int(r["primary_status"] == "C" and r["secondary_status"] != "I"),
        "original_date": parse_date(r["original_date"]), "effective_date": parse_date(r["effective_date"]),
        "expiration_date": parse_date(r["expiration_date"]),
    }


def occupation_label(code: str | None) -> str:
    return OCCUPATIONS.get(code or "", code or "license")
