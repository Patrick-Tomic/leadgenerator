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


MIGRATIONS = [m001_customers_and_discovery, m002_evidence_scoring]
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
