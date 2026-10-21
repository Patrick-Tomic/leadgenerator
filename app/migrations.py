"""Versioned, additive schema migrations tracked with PRAGMA user_version.

Rules: never drop or rewrite existing tables or columns; each migration runs in one
transaction; a file copy of the database is taken before the first pending migration
runs on a database that already holds leads. Add a migration by appending a function
to MIGRATIONS. Its position (1-based) is its version.
"""
import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .dedup import keys
from .profiles import load_file, profile_hash

log = logging.getLogger(__name__)

# v0: the original MVP schema (kept verbatim so a brand-new DB starts from the same place).
BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    name TEXT NOT NULL,
    industry TEXT NOT NULL,
    address TEXT,
    city TEXT,
    lat REAL,
    lon REAL,
    phone TEXT,
    email TEXT,
    website TEXT,
    raw JSON,
    discovered_at TEXT NOT NULL,
    analyzed_at TEXT,
    website_status TEXT,
    final_url TEXT,
    findings JSON,
    opportunities JSON,
    score INTEGER,
    grade TEXT,
    summary TEXT,
    ai_brief TEXT,
    status TEXT NOT NULL DEFAULT 'new',
    notes TEXT,
    status_changed_at TEXT,
    UNIQUE (source, source_id)
);
CREATE INDEX IF NOT EXISTS idx_leads_ready ON leads (status, analyzed_at, score);

CREATE TABLE IF NOT EXISTS source_runs (
    key TEXT PRIMARY KEY,
    ran_at TEXT NOT NULL,
    found INTEGER NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _run(c: sqlite3.Connection, sql: str) -> None:
    # Not executescript(): on Python 3.10 it commits the open transaction first.
    for stmt in sql.split(";"):
        if stmt.strip():
            c.execute(stmt)


def m001_customers_and_discovery(c: sqlite3.Connection) -> None:
    """Customer profiles + per-customer lead assignments, discovery scan log,
    cross-source dedup keys. Existing workflow state moves to the default customer;
    the legacy columns on `leads` are left untouched (frozen) for rollback."""
    _run(c, """
        ALTER TABLE leads ADD COLUMN norm_name TEXT;
        ALTER TABLE leads ADD COLUMN norm_domain TEXT;
        ALTER TABLE leads ADD COLUMN norm_phone TEXT;
        ALTER TABLE leads ADD COLUMN last_seen_at TEXT;
        CREATE INDEX idx_leads_norm_domain ON leads (norm_domain);
        CREATE INDEX idx_leads_norm_phone ON leads (norm_phone);
        CREATE INDEX idx_leads_norm_name ON leads (norm_name);

        CREATE TABLE lead_sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER NOT NULL REFERENCES leads(id),
            source TEXT NOT NULL,
            source_id TEXT NOT NULL,
            industry TEXT,
            area TEXT,
            raw JSON,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            UNIQUE (source, source_id)
        );
        CREATE INDEX idx_lead_sources_lead ON lead_sources (lead_id);

        CREATE TABLE discovery_scans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            adapter TEXT NOT NULL,
            area TEXT NOT NULL,
            industry TEXT NOT NULL,
            mode TEXT NOT NULL,            -- full | incremental
            since TEXT,                    -- incremental: only elements edited after this
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL,          -- running | ok | error
            found INTEGER,
            added INTEGER,
            merged INTEGER,
            error TEXT
        );
        CREATE INDEX idx_scans_key ON discovery_scans (adapter, area, industry, started_at);

        CREATE TABLE customers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            profile JSON NOT NULL,
            profile_hash TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE customer_leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER NOT NULL REFERENCES customers(id),
            lead_id INTEGER NOT NULL REFERENCES leads(id),
            score INTEGER,
            grade TEXT,
            opportunities JSON,
            summary TEXT,
            status TEXT NOT NULL DEFAULT 'new',
            notes TEXT,
            ai_brief TEXT,
            status_changed_at TEXT,
            qualified_at TEXT,
            UNIQUE (customer_id, lead_id)
        );
        CREATE INDEX idx_customer_leads_ready ON customer_leads (customer_id, status, score);
    """)
    now = _now()

    # Dedup keys + source records for every existing lead.
    for r in c.execute("SELECT id, name, website, phone, source, source_id, industry, raw, discovered_at FROM leads").fetchall():
        k = keys({"name": r[1], "website": r[2], "phone": r[3]})
        c.execute("UPDATE leads SET norm_name=?, norm_domain=?, norm_phone=?, last_seen_at=? WHERE id=?",
                  (k["norm_name"], k["norm_domain"], k["norm_phone"], r[8], r[0]))
        c.execute("""INSERT INTO lead_sources (lead_id, source, source_id, industry, area, raw, first_seen_at, last_seen_at)
                     VALUES (?, ?, ?, ?, 'tampa_bay_legacy', ?, ?, ?)""", (r[0], r[4], r[5], r[6], r[7], r[8], r[8]))

    # v0.1 discovery runs become completed scans of the legacy single-box area.
    for key, ran_at, found in c.execute("SELECT key, ran_at, found FROM source_runs").fetchall():
        adapter, _, industry = key.partition(":")
        c.execute("""INSERT INTO discovery_scans (adapter, area, industry, mode, started_at, finished_at, status, found)
                     VALUES (?, 'tampa_bay_legacy', ?, 'full', ?, ?, 'ok', ?)""", (adapter, industry, ran_at, ran_at, found))

    # Default customer, then move existing workflow state onto it, unchanged.
    profile = load_file(config.PROFILES_DIR / f"{config.DEFAULT_CUSTOMER}.json")
    cur = c.execute("""INSERT INTO customers (slug, name, profile, profile_hash, active, created_at, updated_at)
                       VALUES (?, ?, ?, ?, 1, ?, ?)""",
                    (profile["slug"], profile["name"], json.dumps(profile), profile_hash(profile), now, now))
    c.execute("""INSERT INTO customer_leads (customer_id, lead_id, score, grade, opportunities, summary, status, notes,
                                             ai_brief, status_changed_at, qualified_at)
                 SELECT ?, id, score, grade, opportunities, summary, status, notes, ai_brief, status_changed_at, analyzed_at
                 FROM leads WHERE analyzed_at IS NOT NULL OR status != 'new' OR notes IS NOT NULL""", (cur.lastrowid,))


def m002_evidence_scoring(c: sqlite3.Connection) -> None:
    """Score explanations per customer lead, audit version per business, scoring version
    per customer. Purely additive: existing scores, statuses and notes are untouched here;
    the app recomputes scores (not statuses) at startup because scoring_version starts at 0."""
    _run(c, """
        ALTER TABLE customer_leads ADD COLUMN explanation JSON;
        ALTER TABLE leads ADD COLUMN audit_version INTEGER NOT NULL DEFAULT 1;
        ALTER TABLE customers ADD COLUMN scoring_version INTEGER NOT NULL DEFAULT 0
    """)


def m003_provenance_websites_registrations(c: sqlite3.Connection) -> None:
    """Field-level source provenance, website candidates with verification state, and
    Florida business registrations kept apart from operating businesses. Additive only;
    backfills fill empty values and never overwrite existing ones."""
    _run(c, """
        ALTER TABLE lead_sources ADD COLUMN source_url TEXT;
        ALTER TABLE lead_sources ADD COLUMN fields JSON;
        ALTER TABLE lead_sources ADD COLUMN match_confidence TEXT;
        ALTER TABLE lead_sources ADD COLUMN match_explanation TEXT;
        ALTER TABLE lead_sources ADD COLUMN metadata JSON;
        ALTER TABLE lead_sources ADD COLUMN last_checked_at TEXT;

        ALTER TABLE leads ADD COLUMN website_state TEXT;
        ALTER TABLE leads ADD COLUMN website_conflict INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE leads ADD COLUMN website_checked_at TEXT;

        CREATE TABLE website_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER NOT NULL REFERENCES leads(id),
            url TEXT NOT NULL,
            norm_domain TEXT,
            source TEXT NOT NULL,            -- osm | manual | search:brave | google_places | ...
            source_record_id TEXT,
            identity TEXT,                   -- high | medium | low: is this site really this business?
            match_evidence JSON,             -- reasons for the identity level
            check_status TEXT,               -- ok | blocked | robots | unreachable | dns | parked | social | directory | unchecked
            http_status INTEGER,
            final_url TEXT,
            detail TEXT,
            first_seen_at TEXT NOT NULL,
            checked_at TEXT,
            selected INTEGER NOT NULL DEFAULT 0,
            UNIQUE (lead_id, url, source)
        );
        CREATE INDEX idx_candidates_lead ON website_candidates (lead_id);

        CREATE TABLE registrations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,            -- sunbiz_cor | sunbiz_fic
            doc_number TEXT NOT NULL,
            name TEXT NOT NULL,
            norm_name TEXT,
            entity_type TEXT,
            status TEXT,
            filing_date TEXT,
            filing_date_raw TEXT,
            principal_address TEXT,          -- as filed, NOT a confirmed operating location
            principal_city TEXT,
            principal_state TEXT,
            principal_zip TEXT,
            county TEXT,
            area TEXT,
            industry TEXT,
            source_url TEXT,
            source_file TEXT,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            match_lead_id INTEGER REFERENCES leads(id),
            match_confidence TEXT,
            match_evidence JSON,
            flags JSON,
            promoted_lead_id INTEGER REFERENCES leads(id),
            UNIQUE (source, doc_number)
        );
        CREATE INDEX idx_registrations_filed ON registrations (filing_date);
        CREATE INDEX idx_registrations_norm ON registrations (norm_name);

        CREATE TABLE registration_imports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            file_name TEXT NOT NULL,
            file_date TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL,            -- running | ok | error
            records INTEGER, imported INTEGER, updated INTEGER, skipped INTEGER, rejected INTEGER,
            errors JSON,
            UNIQUE (source, file_name)
        );

        CREATE TABLE api_usage (
            source TEXT NOT NULL,
            period TEXT NOT NULL,            -- YYYY-MM
            calls INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (source, period)
        )
    """)
    from .discovery.osm import contributed_fields
    for lid, source, source_id, raw, created in c.execute(
            "SELECT lead_id, source, source_id, raw, first_seen_at FROM lead_sources").fetchall():
        data = json.loads(raw or "{}")
        if source == "osm":
            tags = data.get("tags", {})
            fields, url = contributed_fields(tags), data.get("osm_url")
        else:
            fields, url = {k: v for k, v in data.items() if k in ("website",)}, None
        c.execute("""UPDATE lead_sources SET source_url=?, fields=?, last_checked_at=?,
                     match_confidence=COALESCE(match_confidence, 'high'),
                     match_explanation=COALESCE(match_explanation, ?) WHERE source=? AND source_id=?""",
                  (url, json.dumps(fields), created, "original record (before v0.4 provenance tracking)", source, source_id))
        # Repair values the v0.3 OSM mapping dropped (fill empty columns only).
        if source == "osm":
            c.execute("UPDATE leads SET city=COALESCE(city, ?), phone=COALESCE(phone, ?) WHERE id=?",
                      (fields.get("city"), fields.get("phone"), lid))
    # Existing websites become candidates; their state is re-derived by the next audit.
    for lid, website, ws in c.execute("SELECT id, website, website_status FROM leads").fetchall():
        if website:
            src = c.execute("SELECT source FROM lead_sources WHERE lead_id=? AND source='manual'", (lid,)).fetchone()
            c.execute("""INSERT OR IGNORE INTO website_candidates (lead_id, url, norm_domain, source, identity, match_evidence,
                         check_status, first_seen_at, selected) VALUES (?, ?, ?, ?, 'high', ?, 'unchecked', ?, 1)""",
                      (lid, website, keys({"website": website})["norm_domain"], src[0] if src else "osm",
                       json.dumps(["listed on the business's own source record"]), _now()))
        state = {"unverified": "unknown", "none": "none_found"}.get(ws, "found_unverified" if website else "unknown")
        c.execute("UPDATE leads SET website_state=? WHERE id=?", (state, lid))


def m004_state_licenses(c: sqlite3.Connection) -> None:
    """Florida DBPR construction licenses (public records), matched to registrations and
    businesses as operating evidence. Individuals' names and street addresses are not stored."""
    _run(c, """
        CREATE TABLE licenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,            -- dbpr_cilb
            record_key TEXT NOT NULL,
            occupation_code TEXT,
            license_number TEXT,
            business_name TEXT,              -- NULL when the licensee is an individual
            dba_name TEXT,
            norm_name TEXT,
            norm_dba TEXT,
            is_individual INTEGER NOT NULL DEFAULT 0,
            address TEXT,                    -- business licensees only
            city TEXT,
            state TEXT,
            zip TEXT,
            primary_status TEXT,
            secondary_status TEXT,
            active INTEGER NOT NULL DEFAULT 0,
            original_date TEXT,
            effective_date TEXT,
            expiration_date TEXT,
            source_file TEXT,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            UNIQUE (source, record_key)
        );
        CREATE INDEX idx_licenses_norm_name ON licenses (norm_name);
        CREATE INDEX idx_licenses_norm_dba ON licenses (norm_dba);

        ALTER TABLE registrations ADD COLUMN license_id INTEGER REFERENCES licenses(id);
        ALTER TABLE registrations ADD COLUMN license_match_confidence TEXT;
        ALTER TABLE registrations ADD COLUMN license_evidence JSON
    """)


def m005_priority(c: sqlite3.Connection) -> None:
    """Website-check priority per business (home market first when discovery is statewide)."""
    _run(c, """
        ALTER TABLE leads ADD COLUMN priority INTEGER NOT NULL DEFAULT 1;
        CREATE INDEX idx_leads_audit_queue ON leads (analyzed_at, priority)
    """)
    from .db import _priority
    for lid, lat, lon in c.execute("SELECT id, lat, lon FROM leads").fetchall():
        c.execute("UPDATE leads SET priority=? WHERE id=?", (_priority({"lat": lat, "lon": lon}), lid))
    # Sunbiz is Florida's registry: a registration whose city isn't in AREA_CITIES is still in Florida.
    c.execute("UPDATE registrations SET area='us-fl' WHERE area IS NULL")


MIGRATIONS = [m001_customers_and_discovery, m002_evidence_scoring, m003_provenance_websites_registrations,
              m004_state_licenses, m005_priority]
LATEST = len(MIGRATIONS)


def version(c: sqlite3.Connection) -> int:
    return c.execute("PRAGMA user_version").fetchone()[0]


def backup(c: sqlite3.Connection, db_path: Path, from_version: int) -> Path | None:
    if str(db_path) == ":memory:" or not c.execute("SELECT COUNT(*) FROM leads").fetchone()[0]:
        return None
    config.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = config.BACKUP_DIR / f"{Path(db_path).stem}-v{from_version}-{stamp}.db"
    with sqlite3.connect(dest) as out:
        c.backup(out)
    log.info("backed up database to %s", dest)
    return dest


def migrate(c: sqlite3.Connection, db_path: Path) -> list[int]:
    """Bring the database to LATEST. Returns the versions applied."""
    c.executescript(BASE_SCHEMA)
    start = version(c)
    if start > LATEST:
        raise RuntimeError(f"database is at schema v{start}, newer than this code (v{LATEST}); refusing to run")
    if start == LATEST:
        return []
    backup(c, db_path, start)
    applied = []
    old_isolation = c.isolation_level
    c.isolation_level = None  # explicit transaction control
    try:
        for v in range(start + 1, LATEST + 1):
            c.execute("BEGIN")
            try:
                MIGRATIONS[v - 1](c)
                c.execute(f"PRAGMA user_version = {v}")
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise
            applied.append(v)
            log.info("applied migration v%d (%s)", v, MIGRATIONS[v - 1].__name__)
    finally:
        c.isolation_level = old_isolation
    return applied
