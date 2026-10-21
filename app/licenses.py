"""Import Florida DBPR construction licenses and match them to registrations and businesses.

A current state contractor license is strong evidence that a company is actually
operating, which a bare LLC filing is not. Matching requires the same normalized
business (or DBA) name; location agreement makes it high confidence. Florida requires
entity names to be distinguishable statewide, so an exact name match without location
is still a reasonable "medium". Licenses never create or merge businesses on their own.
"""
import csv
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import httpx

from . import config, db
from .discovery.dbpr import occupation_label, parse_row

log = logging.getLogger(__name__)
SOURCE = "dbpr_cilb"
SOURCE_URL = "https://www2.myfloridalicense.com/construction-industry/public-records/"
FIELDS = ("record_key", "occupation_code", "license_number", "business_name", "dba_name", "norm_name", "norm_dba",
          "is_individual", "address", "city", "state", "zip", "primary_status", "secondary_status", "active",
          "original_date", "effective_date", "expiration_date")


def download(dest_dir: Path | None = None) -> dict:
    """Fetch the weekly licensee file, only if DBPR has published a newer one."""
    dest_dir = Path(dest_dir or config.DBPR_DIR)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "CONSTRUCTIONLICENSE_1.csv"
    headers = {"User-Agent": config.USER_AGENT}
    if dest.exists():
        headers["If-Modified-Since"] = datetime.fromtimestamp(dest.stat().st_mtime, timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
    with httpx.Client(timeout=300, follow_redirects=True) as client, client.stream("GET", config.DBPR_CONSTRUCTION_URL, headers=headers) as r:
        if r.status_code == 304:
            return {"status": "not modified", "file": str(dest)}
        r.raise_for_status()
        tmp = dest.with_suffix(".part")
        with open(tmp, "wb") as fh:
            for chunk in r.iter_bytes():
                fh.write(chunk)
        tmp.replace(dest)  # never leave a half-downloaded file in place
        if r.headers.get("last-modified"):
            ts = datetime.strptime(r.headers["last-modified"], "%a, %d %b %Y %H:%M:%S GMT").replace(tzinfo=timezone.utc).timestamp()
            os.utime(dest, (ts, ts))
    return {"status": "downloaded", "file": str(dest), "bytes": dest.stat().st_size}


def import_file(path: Path | None = None, force: bool = False) -> dict:
    """Import (or refresh) the licensee file in one transaction. Skipped if this exact
    version (by file date) was already imported."""
    path = Path(path or config.DBPR_DIR / "CONSTRUCTIONLICENSE_1.csv")
    version = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M")
    c = db.conn()
    prior = c.execute("SELECT status, file_date FROM registration_imports WHERE source=? AND file_name=?", (SOURCE, path.name)).fetchone()
    if prior and prior[0] == "ok" and prior[1] == version and not force:
        return {"file": path.name, "status": "skipped", "reason": "this version is already imported"}
    stats = {"records": 0, "kept": 0, "skipped": 0}
    ts = datetime.now(timezone.utc).isoformat()  # microseconds: identifies this import run
    with db._lock:
        c.execute("""INSERT INTO registration_imports (source, file_name, file_date, started_at, status) VALUES (?, ?, ?, ?, 'running')
                     ON CONFLICT (source, file_name) DO UPDATE SET file_date=excluded.file_date, started_at=excluded.started_at,
                     status='running', finished_at=NULL""", (SOURCE, path.name, version, ts))
        c.commit()
        try:
            batch = []
            with open(path, encoding="latin-1", newline="") as fh:
                for row in csv.reader(fh):
                    stats["records"] += 1
                    rec = parse_row(row)
                    if not rec:
                        stats["skipped"] += 1
                        continue
                    batch.append({**rec, "file": path.name, "ts": ts})
            cols = ", ".join(FIELDS)
            c.executemany(
                f"""INSERT INTO licenses (source, {cols}, source_file, first_seen_at, last_seen_at)
                    VALUES ('{SOURCE}', {', '.join(':' + f for f in FIELDS)}, :file, :ts, :ts)
                    ON CONFLICT (source, record_key) DO UPDATE SET {', '.join(f'{f}=excluded.{f}' for f in FIELDS if f != 'record_key')},
                    source_file=excluded.source_file, last_seen_at=excluded.last_seen_at""", batch)
            stats["kept"] = len(batch)
            # Records missing from this week's file were dropped by DBPR (expired, null and void...).
            stats["no_longer_listed"] = c.execute("UPDATE licenses SET active=0 WHERE source=? AND last_seen_at != ? AND active=1",
                                                  (SOURCE, ts)).rowcount
            c.execute("UPDATE registration_imports SET finished_at=?, status='ok', records=?, imported=?, skipped=? WHERE source=? AND file_name=?",
                      (db.now(), stats["records"], stats["kept"], stats["skipped"], SOURCE, path.name))
            c.commit()
        except Exception as e:
            c.rollback()
            c.execute("UPDATE registration_imports SET finished_at=?, status='error', errors=? WHERE source=? AND file_name=?",
                      (db.now(), json.dumps([f"{type(e).__name__}: {e}"]), SOURCE, path.name))
            c.commit()
            log.exception("DBPR import failed; existing license records kept")
            return {"file": path.name, "status": "error", "error": str(e), **stats}
    matched = match_all()
    return {"file": path.name, "status": "ok", **stats, **matched}


def _candidates(norm: str) -> list[dict]:
    return [db._row(r) for r in db.conn().execute(
        "SELECT * FROM licenses WHERE (norm_name=? OR norm_dba=?) ORDER BY active DESC, original_date DESC", (norm, norm)).fetchall()]


def _level(lic: dict, city: str | None, zip_: str | None) -> tuple[str, list[str]]:
    ev = [f"same name as {occupation_label(lic['occupation_code']).lower()} "
          f"{lic['license_number'] or ''}".strip() + (f" (DBA {lic['dba_name']})" if lic.get("dba_name") else "")]
    if zip_ and lic.get("zip") and zip_[:5] == lic["zip"]:
        ev.append(f"same ZIP ({lic['zip']})")
        return "high", ev
    if city and lic.get("city") and city.lower().replace(".", "") == lic["city"].lower().replace(".", ""):
        ev.append(f"same city ({lic['city']})")
        return "high", ev
    return "medium", ev + ["location differs or is missing"]


def match_all() -> dict:
    """Link licenses to registrations and to known businesses (provenance only)."""
    c = db.conn()
    regs = 0
    for reg in [dict(r) for r in c.execute("SELECT id, norm_name, principal_city, principal_zip FROM registrations WHERE norm_name IS NOT NULL")]:
        best = None
        for lic in _candidates(reg["norm_name"]):
            level, ev = _level(lic, reg["principal_city"], reg["principal_zip"])
            if best is None or (level == "high" and best[1] != "high"):
                best = (lic, level, ev)
        if best:
            c.execute("UPDATE registrations SET license_id=?, license_match_confidence=?, license_evidence=? WHERE id=?",
                      (best[0]["id"], best[1], json.dumps(best[2]), reg["id"]))
            regs += 1
    leads = 0
    for lead in [dict(r) for r in c.execute("SELECT id, norm_name, city, address, industry FROM leads WHERE norm_name IS NOT NULL")]:
        zip_ = next(iter(__import__("re").findall(r"\b(\d{5})\b", lead["address"] or "")), None)
        for lic in _candidates(lead["norm_name"]):
            level, ev = _level(lic, lead["city"], zip_)
            if level != "high":
                continue  # a business record gets license provenance only on a location-confirmed match
            add_license_source(lead["id"], lead["industry"], lic, ev)
            leads += 1
            break
    c.commit()
    from .registrations import refresh_flags
    refresh_flags()
    return {"registrations_matched": regs, "businesses_matched": leads}


def add_license_source(lead_id: int, industry: str, lic: dict, evidence: list[str]) -> None:
    fields = {"license": f"{occupation_label(lic['occupation_code'])}" + (f" {lic['license_number']}" if lic["license_number"] else ""),
              "license_status": "active" if lic["active"] else "not active",
              "licensed_since": lic["original_date"], "dba_name": lic["dba_name"], "legal_name": lic["business_name"]}
    ts = db.now()
    db.conn().execute(
        """INSERT INTO lead_sources (lead_id, source, source_id, industry, area, raw, first_seen_at, last_seen_at, source_url, fields,
                                     match_confidence, match_explanation, metadata, last_checked_at)
           VALUES (?, ?, ?, ?, NULL, '{}', ?, ?, ?, ?, 'high', ?, ?, ?)
           ON CONFLICT (source, source_id) DO UPDATE SET fields=excluded.fields, last_seen_at=excluded.last_seen_at,
               last_checked_at=excluded.last_checked_at""",
        (lead_id, SOURCE, lic["record_key"], industry, ts, ts, SOURCE_URL, json.dumps({k: v for k, v in fields.items() if v}),
         "; ".join(evidence), json.dumps({"note": "state contractor license record (public record)"}), ts))
