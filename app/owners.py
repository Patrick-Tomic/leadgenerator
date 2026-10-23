"""Owner and officer names for known businesses, from Florida Sunbiz data files.

Sources: the quarterly full files (doc/Quarterly/Cor/cordata.zip: every active company,
doc/Quarterly/Fic/ficdata.zip: every active fictitious name/DBA) and the daily files.
Layouts: https://dos.sunbiz.org/data-definitions/cor.html and fic.html.

What's kept: person names and titles only (officers/managers of a company, owners of a
DBA), linked to a business we already know. Never officer, owner or registered-agent
addresses, and no company-type officers.

Matching: same normalized name as the business AND location agreement: same street
(high), same ZIP (medium), or same city (low; only for distinctive names of 2+ words when
exactly one business with that name is in that city). A name alone is never enough, and
low matches are labeled "verify" and never used to greet someone in an email. Many small businesses trade
under a DBA, so the fictitious-name file catches trade names the company file misses.
"""
import io
import json
import logging
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from . import db
from .dedup import normalize_name, normalize_street

log = logging.getLogger(__name__)

COR_OFFICERS = [669, 797, 925, 1053, 1181, 1309]           # 1-based starts of the 6 officer blocks
FIC_OWNERS = [389, 560, 731, 902, 1073, 1404, 1575, 1746, 1917]   # owner 6 (1244) skipped: overlapping layout
TITLES = {
    "P": ("President", 0), "PRES": ("President", 0), "CEO": ("CEO", 0), "MGR": ("Manager", 0), "MGRM": ("Managing member", 0),
    "AMBR": ("Authorized member", 0), "MBR": ("Member", 0), "OWNR": ("Owner", 0), "OWNER": ("Owner", 0),
    "AP": ("Authorized person", 1), "VP": ("Vice president", 1), "V": ("Vice president", 1), "D": ("Director", 1),
    "DIR": ("Director", 1), "C": ("Chairman", 1), "COO": ("COO", 1), "CFO": ("CFO", 1),
    "S": ("Secretary", 2), "SEC": ("Secretary", 2), "T": ("Treasurer", 2), "TRES": ("Treasurer", 2),
    # Seen in the real quarterly file (Oct 2026):
    "MANA": ("Manager", 0), "PD": ("President & director", 0), "DP": ("Director & president", 0),
    "PSTD": ("President, secretary, treasurer & director", 0), "AUTH": ("Authorized member", 1),
    "AR": ("Authorized representative", 1), "DIRE": ("Director", 1), "VD": ("Vice president & director", 1),
    "SECR": ("Secretary", 2), "TREA": ("Treasurer", 2),
}


def _f(line: str, start: int, length: int) -> str:
    return line[start - 1:start - 1 + length].strip()


def _cap(text: str) -> str:
    return " ".join(w.upper() if re.fullmatch(r"(II|III|IV|JR|SR)\.?", w, re.I) else
                    "-".join(p.capitalize() for p in w.split("-")) for w in text.split())


def display_name(raw: str) -> str:
    """Person name as written in the data files -> 'First Middle Last'.

    The files store names in fixed sub-fields: last name (20 characters), first name (14),
    then middle, e.g. 'KELLEY              JENNIFER      D'. A few older records use
    'LAST, FIRST' instead."""
    if "," in raw:
        last, first = raw.split(",", 1)
        return _cap(f"{first.strip()} {last.strip()}")
    if len(raw) > 20 and raw[20:].strip():
        last, first, middle = raw[:20].strip(), raw[20:34].strip(), raw[34:].strip()
        return _cap(" ".join(x for x in (first, middle, last) if x))
    return _cap(raw.strip())


def first_name(display: str) -> str:
    return display.split()[0] if display else ""


def build_index() -> dict[str, list[dict]]:
    """normalized business name -> businesses with what we know of their address."""
    index: dict[str, list[dict]] = {}
    per_city: dict[tuple, int] = {}
    for r in db.conn().execute("SELECT id, norm_name, address, city FROM leads WHERE norm_name IS NOT NULL"):
        zips = re.findall(r"\b(\d{5})(?:-\d{4})?\b", r["address"] or "")
        index.setdefault(r["norm_name"], []).append(
            {"id": r["id"], "street": normalize_street(r["address"]), "zip": zips[-1] if zips else None,
             "city": (r["city"] or "").lower()})
        key = (r["norm_name"], (r["city"] or "").lower())
        per_city[key] = per_city.get(key, 0) + 1
    for name, businesses in index.items():
        for b in businesses:
            b["unique_in_city"] = bool(b["city"]) and per_city[(name, b["city"])] == 1
    return index


def _distinctive(norm_name: str) -> bool:
    return len(norm_name.split()) >= 2 and len(norm_name) >= 13   # "repipe experts" yes, "best pizza" no


def _match(candidates: list[dict], street: str | None, zip5: str | None, city: str | None = None,
           distinctive: bool = False) -> list[tuple[int, str, list[str]]]:
    out = []
    for b in candidates:
        if street and b["street"] and street == b["street"]:
            out.append((b["id"], "high", [f"same name and street address ({street})"]))
        elif zip5 and b["zip"] and zip5 == b["zip"]:
            out.append((b["id"], "medium", [f"same name and ZIP ({zip5})"]))
        elif distinctive and city and b.get("unique_in_city") and city == b["city"]:
            out.append((b["id"], "low", [f"same distinctive name in the same city ({city.title()}); verify"]))
    return out


# Zip methods Python's zipfile can decompress. Sunbiz's cordata.zip uses Deflate64 (9),
# which it can't, so those members are streamed through the system `unzip -p` instead
# (about 18 GB uncompressed: never written to disk).
_PY_ZIP_METHODS = {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED, zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA}


def _lines(path: Path):
    """Text lines from a .txt data file or every member of a .zip, streamed."""
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as z:
            members = z.infolist()
        for info in members:
            if info.compress_type in _PY_ZIP_METHODS:
                with zipfile.ZipFile(path) as z, z.open(info) as fh:
                    yield from io.TextIOWrapper(fh, encoding="latin-1", newline="")
            else:
                yield from _unzip_stream(path, info.filename)
    else:
        with open(path, encoding="latin-1", newline="") as fh:
            yield from fh


def _unzip_stream(path: Path, member: str):
    import shutil
    import subprocess
    if not shutil.which("unzip"):
        raise RuntimeError(f"{path.name} needs the 'unzip' program (sudo apt install unzip)")
    proc = subprocess.Popen(["unzip", "-p", str(path), member], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        yield from io.TextIOWrapper(proc.stdout, encoding="latin-1", newline="")
    finally:
        proc.stdout.close()
        if proc.wait() not in (0, -13):  # -13: we stopped reading early (SIGPIPE)
            raise RuntimeError(f"unzip failed on {path.name}:{member} (exit {proc.returncode})")


def kind_of(path: Path) -> str | None:
    n = path.name.lower()
    if n.startswith("cordata") or re.search(r"\d{8}c\.txt$", n):
        return "sunbiz_cor"
    if n.startswith("ficdata") or re.search(r"\d{8}f\.txt$", n):
        return "sunbiz_fic"
    return None


def people_from(line: str, kind: str) -> tuple[str, str, str | None, str | None, list[tuple[str, str, int]]]:
    """(doc number, entity name, street key, zip, [(display name, title, rank)]) for one record."""
    if kind == "sunbiz_cor":
        doc, name = _f(line, 1, 12), _f(line, 13, 192)
        street, zip5 = normalize_street(_f(line, 221, 42)), (_f(line, 335, 10)[:5] or None)
        people = []
        for start in COR_OFFICERS:
            if _f(line, start + 4, 1) != "P":           # persons only
                continue
            raw = line[start + 4:start + 4 + 42]          # unstripped: the sub-fields are positional
            if raw.strip():
                code = _f(line, start, 4).upper()
                label, rank = TITLES.get(code, (code.title() or "Officer", 3))
                people.append((display_name(raw), label, rank))
    else:
        doc, name = _f(line, 1, 12), _f(line, 13, 192)
        street, zip5 = normalize_street(_f(line, 217, 40)), (_f(line, 327, 10)[:5] or None)
        people = []
        for start in FIC_OWNERS:
            if _f(line, start + 67, 1) != "P":
                continue
            raw = line[start - 1 + 12:start - 1 + 67]
            if raw.strip():
                people.append((display_name(raw), "Owner (DBA filing)", 0))
    return doc, name, street, zip5, people


def import_owners(path: Path, force: bool = False) -> dict:
    """Stream one Sunbiz data file (txt or zip) and attach owner/officer names to matching
    businesses. Re-importing the same file version is skipped."""
    path = Path(path)
    kind = kind_of(path)
    if not kind:
        raise ValueError(f"{path.name}: not a Sunbiz cordata/ficdata file or daily c/f file")
    source = f"owners_{kind}"
    version = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M")
    c = db.conn()
    prior = c.execute("SELECT status, file_date FROM registration_imports WHERE source=? AND file_name=?", (source, path.name)).fetchone()
    if prior and prior[0] == "ok" and prior[1] == version and not force:
        return {"file": path.name, "status": "skipped", "reason": "this version is already imported"}
    index = build_index()
    stats = {"records": 0, "name_matches": 0, "businesses": set(), "people": 0}
    ts = db.now()
    rows = []
    for line in _lines(path):
        if len(line) < 300:
            continue
        stats["records"] += 1
        norm = normalize_name(line[12:204])
        cands = index.get(norm)
        if not cands:
            continue
        if kind == "sunbiz_fic" and _f(line, 352, 1) not in ("A", ""):
            continue
        stats["name_matches"] += 1
        doc, entity, street, zip5, people = people_from(line, kind)
        city = _f(line, 305, 28).lower() if kind == "sunbiz_cor" else _f(line, 297, 28).lower()
        for lead_id, conf, ev in _match(cands, street, zip5, city, _distinctive(norm)):
            for person, title, rank in people:
                rows.append((lead_id, person, title, rank, kind, doc, entity.strip(), conf, json.dumps(ev), ts, ts))
            if people:
                stats["businesses"].add(lead_id)
    with db._lock:
        c.executemany("""INSERT INTO business_people (lead_id, name, title, rank, source, source_record_id, entity_name,
                         match_confidence, match_evidence, first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                         ON CONFLICT (lead_id, source, source_record_id, name) DO UPDATE SET title=excluded.title,
                         rank=excluded.rank, match_confidence=excluded.match_confidence, last_seen_at=excluded.last_seen_at""", rows)
        c.execute("""INSERT INTO registration_imports (source, file_name, file_date, started_at, finished_at, status, records, imported)
                     VALUES (?, ?, ?, ?, ?, 'ok', ?, ?)
                     ON CONFLICT (source, file_name) DO UPDATE SET file_date=excluded.file_date, finished_at=excluded.finished_at,
                     status='ok', records=excluded.records, imported=excluded.imported""",
                  (source, path.name, version, ts, db.now(), stats["records"], len(rows)))
        c.commit()
    stats["people"] = len(rows)
    stats["businesses"] = len(stats["businesses"])
    log.info("%s: %d records, %d name matches, %d businesses got %d names", path.name, stats["records"],
             stats["name_matches"], stats["businesses"], stats["people"])
    return {"file": path.name, "status": "ok", **stats}


def import_dir(directory: Path) -> list[dict]:
    files = sorted(p for p in Path(directory).rglob("*") if p.is_file() and kind_of(p))
    out = []
    for p in files:
        try:
            out.append(import_owners(p))
        except zipfile.BadZipFile:
            # e.g. a download still in progress: skip it, the next run picks it up
            out.append({"file": p.name, "status": "skipped", "reason": "incomplete or not a valid zip"})
    return out


def people_for(lead_id: int) -> list[dict]:
    return [dict(r) for r in db.conn().execute(
        """SELECT name, title, rank, source, source_record_id, entity_name, match_confidence, match_evidence
           FROM business_people WHERE lead_id=?
           ORDER BY CASE match_confidence WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, rank, name""", (lead_id,))]


def primary_person(lead_id: int) -> dict | None:
    """The best person to address: owner/manager/president first, high-confidence matches first."""
    people = people_for(lead_id)
    return people[0] if people else None
