"""Import, match and flag state business registrations (Sunbiz data files).

Registrations live in their own table: a filed LLC is a legal entity, not proof of an
operating business. They're matched against known businesses conservatively (never on
name alone), and new entities without a confirmed website are flagged for qualification.
A person can promote a registration to a lead, after which the normal website
verification, audit and per-customer qualification apply.
"""
import json
import logging
from datetime import date, timedelta
from pathlib import Path

from . import config, db
from .dedup import normalize_name, normalize_street
from .discovery.sunbiz import KINDS, SEARCH_URL, RecordError, exclusion, file_kind_and_date, infer_area, infer_industry, parse_record

log = logging.getLogger(__name__)
MAX_ERROR_SAMPLES = 20
NEW_ENTITY_DAYS = 90


def import_file(path: Path, kind: str | None = None, since: str | None = None, force: bool = False) -> dict:
    """Import one data file. All of a file's records commit together or not at all, so a
    failure partway leaves previously imported data untouched."""
    path = Path(path)
    detected, file_date = file_kind_and_date(path)
    kind = kind or detected
    if kind not in KINDS:
        raise ValueError(f"{path.name}: can't tell the file type; pass kind='sunbiz_cor' or 'sunbiz_fic'")
    c = db.conn()
    prior = c.execute("SELECT status FROM registration_imports WHERE source=? AND file_name=?", (kind, path.name)).fetchone()
    if prior and prior[0] == "ok" and not force:
        return {"file": path.name, "status": "skipped", "reason": "already imported"}
    stats = {"records": 0, "imported": 0, "updated": 0, "skipped": 0, "rejected": 0}
    errors: list[str] = []
    ts = db.now()
    with db._lock:
        c.execute("""INSERT INTO registration_imports (source, file_name, file_date, started_at, status) VALUES (?, ?, ?, ?, 'running')
                     ON CONFLICT (source, file_name) DO UPDATE SET started_at=excluded.started_at, status='running',
                     finished_at=NULL""", (kind, path.name, file_date, ts))
        c.commit()
        try:
            # Fixed-width byte positions: latin-1 maps one byte to one character.
            with open(path, encoding="latin-1", newline="") as fh:
                for n, line in enumerate(fh, 1):
                    if not line.strip():
                        continue
                    stats["records"] += 1
                    try:
                        rec = parse_record(line, kind)
                    except RecordError as e:
                        stats["rejected"] += 1
                        errors.append(f"line {n}: {e}")
                        continue
                    if rec["date_error"]:
                        stats["rejected"] += 1
                        errors.append(f"line {n} ({rec['doc_number']}): {rec['date_error']}")
                        continue
                    if exclusion(rec) or (since and rec["filing_date"] < since):
                        stats["skipped"] += 1
                        continue
                    stats["imported" if _upsert(c, rec, path.name, ts) else "updated"] += 1
            c.execute("""UPDATE registration_imports SET finished_at=?, status='ok', records=?, imported=?, updated=?, skipped=?,
                         rejected=?, errors=? WHERE source=? AND file_name=?""",
                      (db.now(), stats["records"], stats["imported"], stats["updated"], stats["skipped"], stats["rejected"],
                       json.dumps(errors[:MAX_ERROR_SAMPLES]), kind, path.name))
            c.commit()
        except Exception as e:
            c.rollback()
            c.execute("UPDATE registration_imports SET finished_at=?, status='error', errors=? WHERE source=? AND file_name=?",
                      (db.now(), json.dumps([f"{type(e).__name__}: {e}"]), kind, path.name))
            c.commit()
            log.exception("import of %s failed; no records from it were kept", path.name)
            return {"file": path.name, "status": "error", "error": str(e), **stats}
    match_registrations()
    return {"file": path.name, "status": "ok", **stats, "errors": errors[:MAX_ERROR_SAMPLES]}


def _upsert(c, rec: dict, file_name: str, ts: str) -> bool:
    """Insert or refresh one registration. Returns True if it was new."""
    exists = c.execute("SELECT id FROM registrations WHERE source=? AND doc_number=?", (rec["source"], rec["doc_number"])).fetchone()
    values = {**rec, "norm_name": normalize_name(rec["name"]), "area": infer_area(rec["principal_city"]) or "us-fl",
              "industry": infer_industry(rec["name"]), "source_url": SEARCH_URL, "source_file": file_name, "ts": ts}
    if exists:
        c.execute("""UPDATE registrations SET name=:name, norm_name=:norm_name, status=:status, entity_type=:entity_type,
                     filing_date=:filing_date, filing_date_raw=:filing_date_raw, principal_address=:principal_address,
                     principal_city=:principal_city, principal_state=:principal_state, principal_zip=:principal_zip,
                     county=:county, area=:area, industry=:industry, source_file=:source_file, last_seen_at=:ts
                     WHERE source=:source AND doc_number=:doc_number""", values)
        return False
    c.execute("""INSERT INTO registrations (source, doc_number, name, norm_name, entity_type, status, filing_date, filing_date_raw,
                 principal_address, principal_city, principal_state, principal_zip, county, area, industry, source_url,
                 source_file, first_seen_at, last_seen_at)
                 VALUES (:source, :doc_number, :name, :norm_name, :entity_type, :status, :filing_date, :filing_date_raw,
                 :principal_address, :principal_city, :principal_state, :principal_zip, :county, :area, :industry, :source_url,
                 :source_file, :ts, :ts)""", values)
    return True


def import_dir(directory: Path | None = None, since: str | None = None, force: bool = False) -> list[dict]:
    """Import every recognizable data file in a directory, oldest first. Incremental:
    files already imported are skipped; records filed before `since` are skipped."""
    directory = Path(directory or config.SUNBIZ_IMPORT_DIR)
    if since is None and config.SUNBIZ_MAX_AGE_DAYS:
        since = (date.today() - timedelta(days=config.SUNBIZ_MAX_AGE_DAYS)).isoformat()
    files = sorted((p for p in directory.glob("*.txt") if file_kind_and_date(p)[0]),
                   key=lambda p: (file_kind_and_date(p)[1] or "", p.name))
    return [import_file(p, since=since, force=force) for p in files]


# ---------------------------------------------------------------- matching & flags

def _match(reg: dict, lead: dict) -> tuple[str | None, list[str]]:
    """Same normalized name is required but never sufficient: the address must agree too."""
    ev = ["same normalized name"]
    street_r, street_l = normalize_street(reg.get("principal_address")), normalize_street(lead.get("address"))
    if street_r and street_r == street_l:
        ev.append(f"same street address ({street_r})")
        return "high", ev
    zip_l = (lead.get("address") or "")
    same_zip = bool(reg.get("principal_zip")) and reg["principal_zip"] in zip_l
    same_city = bool(reg.get("principal_city")) and (reg["principal_city"] or "").lower() in \
        ((lead.get("city") or "") + " " + (lead.get("address") or "")).lower()
    if same_zip:
        ev.append(f"same ZIP ({reg['principal_zip']})")
    if same_city:
        ev.append(f"same city ({reg['principal_city']})")
    if same_zip or same_city:
        return "medium", ev
    return "low", ev + ["no matching address; may be a different business with the same name"]


def match_registrations(only_unmatched: bool = True) -> int:
    """Link registrations to known businesses. High matches add a provenance record to the
    business (legal entity details only); medium/low are stored for review, never merged."""
    c = db.conn()
    q = "SELECT * FROM registrations WHERE norm_name IS NOT NULL" + (" AND match_lead_id IS NULL" if only_unmatched else "")
    linked = 0
    for reg in [dict(r) for r in c.execute(q).fetchall()]:
        best = None
        for lead in [dict(r) for r in c.execute("SELECT id, address, city FROM leads WHERE norm_name=?", (reg["norm_name"],))]:
            level, ev = _match(reg, lead)
            if best is None or ["high", "medium", "low"].index(level) < ["high", "medium", "low"].index(best[1]):
                best = (lead["id"], level, ev)
        if best:
            db._write("UPDATE registrations SET match_lead_id=?, match_confidence=?, match_evidence=? WHERE id=?",
                      (best[0], best[1], json.dumps(best[2]), reg["id"]))
            if best[1] == "high":
                _add_registration_source(best[0], reg, best[2])
            linked += 1
    refresh_flags()
    return linked


def _add_registration_source(lead_id: int, reg: dict, evidence: list[str]) -> None:
    fields = {"legal_name": reg["name"], "entity_type": reg["entity_type"], "filing_date": reg["filing_date"],
              "registration_status": "active" if reg["status"] == "A" else reg["status"], "doc_number": reg["doc_number"],
              "registered_address": reg["principal_address"]}
    ts = db.now()
    industry = db.conn().execute("SELECT industry FROM leads WHERE id=?", (lead_id,)).fetchone()[0]
    db._write("""INSERT INTO lead_sources (lead_id, source, source_id, industry, area, raw, first_seen_at, last_seen_at, source_url,
                 fields, match_confidence, match_explanation, metadata, last_checked_at)
                 VALUES (?, ?, ?, ?, ?, '{}', ?, ?, ?, ?, ?, ?, ?, ?)
                 ON CONFLICT (source, source_id) DO UPDATE SET fields=excluded.fields, last_seen_at=excluded.last_seen_at""",
              (lead_id, reg["source"], reg["doc_number"], industry, reg.get("area"), ts, ts, reg["source_url"],
               json.dumps({k: v for k, v in fields.items() if v}), "high", "; ".join(evidence),
               json.dumps({"note": "legal entity record; address is as filed, not a confirmed operating location"}), ts))


def refresh_flags() -> None:
    """Flag registrations worth a closer look. Flags describe evidence, not conclusions."""
    c = db.conn()
    cutoff = (date.today() - timedelta(days=NEW_ENTITY_DAYS)).isoformat()
    for reg in [dict(r) for r in c.execute("SELECT * FROM registrations").fetchall()]:
        flags = []
        if reg["filing_date"] and reg["filing_date"] >= cutoff:
            flags.append("new_entity")
        lead = None
        if reg.get("license_id"):
            lic = c.execute("SELECT active, original_date FROM licenses WHERE id=?", (reg["license_id"],)).fetchone()
            if lic and lic[0]:
                flags.append("state_license")
                flags.append("operating_evidence")  # holds a current state contractor license
                # Licensed long before this filing: an existing company filing a new name (often a
                # DBA), not a new business.
                if lic[1] and reg["filing_date"] and lic[1] < (date.fromisoformat(reg["filing_date"]) - timedelta(days=180)).isoformat():
                    flags.append("established_business")
        if reg["match_lead_id"] and reg["match_confidence"] == "high":
            lead = c.execute("SELECT website_state FROM leads WHERE id=?", (reg["match_lead_id"],)).fetchone()
            if "operating_evidence" not in flags:
                flags.append("operating_evidence")  # also appears in an independent business listing
        if not lead or lead[0] not in ("verified", "found_unverified"):
            flags.append("no_confirmed_website")
        if reg["industry"]:
            flags.append("relevant_industry")
        if reg["area"]:
            flags.append("in_known_area")
        if "new_entity" in flags and "no_confirmed_website" in flags:
            flags.append("needs_qualification")
        c.execute("UPDATE registrations SET flags=? WHERE id=?", (json.dumps(flags), reg["id"]))
    c.commit()


def list_registrations(profile: dict | None = None, limit: int = 200) -> list[dict]:
    """Registrations, newest filings first. With a profile, only its areas and industries."""
    """Licensed (operating) companies first, then newest filings."""
    q = """SELECT r.*, l.occupation_code AS license_occupation, l.license_number, l.active AS license_active,
                  l.original_date AS licensed_since, l.dba_name AS license_dba
           FROM registrations r LEFT JOIN licenses l ON l.id = r.license_id"""
    args: list = []
    if profile:
        # A county is in scope when the profile covers it or the state containing it.
        areas = sorted(a for a in config.AREAS if config.area_ancestors(a) & set(profile["areas"]))
        q += f" WHERE r.area IN ({','.join('?' * len(areas))}) AND r.industry IN ({','.join('?' * len(profile['industries']))})"
        args = [*areas, *profile["industries"]]
    q += """ ORDER BY (l.active = 1 AND r.license_match_confidence = 'high') DESC, (l.active = 1) DESC,
                      r.filing_date DESC, r.id DESC LIMIT ?"""
    return [db._row(r) for r in db.conn().execute(q, (*args, limit)).fetchall()]


def promote(reg_id: int, industry: str | None = None) -> int:
    """Turn a registration into a lead so it gets website verification and qualification.
    The lead carries the registration as its source; its address is marked as filed."""
    reg = db._row(db.conn().execute("SELECT * FROM registrations WHERE id=?", (reg_id,)).fetchone())
    if not reg:
        raise LookupError("registration not found")
    if reg["promoted_lead_id"]:
        return reg["promoted_lead_id"]
    if reg["match_lead_id"] and reg["match_confidence"] == "high":
        db._write("UPDATE registrations SET promoted_lead_id=? WHERE id=?", (reg["match_lead_id"], reg_id))
        return reg["match_lead_id"]
    industry = industry or reg["industry"]
    if industry not in config.INDUSTRIES:
        raise ValueError("choose an industry for this registration")
    b = {"source": reg["source"], "source_id": reg["doc_number"], "source_url": reg["source_url"], "name": reg["name"],
         "industry": industry, "address": reg["principal_address"], "city": reg["principal_city"], "lat": None, "lon": None,
         "phone": None, "email": None, "website": None,
         "fields": {"legal_name": reg["name"], "entity_type": reg["entity_type"], "filing_date": reg["filing_date"],
                    "doc_number": reg["doc_number"], "registered_address": reg["principal_address"],
                    "city": reg["principal_city"], "postcode": reg["principal_zip"]},
         "metadata": {"note": "legal entity record; address is as filed, not a confirmed operating location"},
         "raw": {"registration_id": reg_id}}
    db.upsert_business(b, area=reg["area"])
    lead_id = db.conn().execute("SELECT lead_id FROM lead_sources WHERE source=? AND source_id=?",
                                (reg["source"], reg["doc_number"])).fetchone()[0]
    if reg.get("license_id"):
        from .licenses import add_license_source
        lic = db._row(db.conn().execute("SELECT * FROM licenses WHERE id=?", (reg["license_id"],)).fetchone())
        add_license_source(lead_id, industry, lic, list(reg.get("license_evidence") or []))
        db.conn().commit()
    db._write("UPDATE registrations SET promoted_lead_id=? WHERE id=?", (lead_id, reg_id))
    return lead_id


def recent_imports(limit: int = 20) -> list[dict]:
    return [db._row(r) for r in db.conn().execute(
        "SELECT * FROM registration_imports ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]
