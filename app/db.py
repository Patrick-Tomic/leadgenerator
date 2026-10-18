"""SQLite storage.

- `leads`: one row per real-world business plus its (customer-independent) website audit.
- `lead_sources`: every source record merged into a business (dedup provenance).
- `discovery_scans`: one row per area x industry discovery attempt, with status/errors.
- `customers` / `customer_leads`: per-customer relevance score, status, notes, AI brief.

Every workflow query takes a customer_id, so one customer's leads, statuses and
exports never leak into another's. Schema changes go through app/migrations.py.
"""
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from . import config, migrations
from .dedup import keys, same_place
from .profiles import profile_hash

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

JSON_COLS = ("raw", "findings", "opportunities", "profile", "explanation")
STATUSES = ("new", "saved", "contacted", "won", "not_fit", "skipped")
BUSINESS_COLS = ("id", "source", "source_id", "name", "industry", "address", "city", "lat", "lon", "phone", "email",
                 "website", "raw", "discovered_at", "last_seen_at", "analyzed_at", "website_status", "final_url", "findings")
CUSTOMER_COLS = ("score", "grade", "opportunities", "explanation", "summary", "status", "notes", "ai_brief", "status_changed_at",
                 "qualified_at")
LEAD_SELECT = (", ".join(f"l.{c}" for c in BUSINESS_COLS) + ", " + ", ".join(f"cl.{c}" for c in CUSTOMER_COLS)
               + ", cl.customer_id FROM customer_leads cl JOIN leads l ON l.id = cl.lead_id")
READY = "cl.status='new' AND l.analyzed_at IS NOT NULL AND cl.score >= ?"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def use(path: Path | str) -> sqlite3.Connection:
    """Open (and migrate) the database at path. Tests use this to point at a temp DB."""
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(path, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        migrations.migrate(_conn, Path(path))
    return _conn


def conn() -> sqlite3.Connection:
    return _conn if _conn is not None else use(config.DB_PATH)


def _row(r: sqlite3.Row | None) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    for col in JSON_COLS:
        if d.get(col):
            d[col] = json.loads(d[col])
    return d


def _write(sql: str, args=()) -> sqlite3.Cursor:
    with _lock:
        cur = conn().execute(sql, args)
        conn().commit()
        return cur


# ---------------------------------------------------------------- businesses


def find_existing(b: dict) -> tuple[int, str] | None:
    """Return (lead_id, match_rule) if this discovered record is a business we already have."""
    c = conn()
    r = c.execute("SELECT lead_id FROM lead_sources WHERE source=? AND source_id=?", (b["source"], b["source_id"])).fetchone()
    if r:
        return r[0], "source_id"
    k = keys(b)
    if k["norm_domain"]:
        r = c.execute("SELECT id FROM leads WHERE norm_domain=? ORDER BY id LIMIT 1", (k["norm_domain"],)).fetchone()
        if r:
            return r[0], "domain"
    if k["norm_phone"]:
        r = c.execute("SELECT id FROM leads WHERE norm_phone=? ORDER BY id LIMIT 1", (k["norm_phone"],)).fetchone()
        if r:
            return r[0], "phone"
    if k["norm_name"]:
        for r in c.execute("SELECT id, lat, lon, city FROM leads WHERE norm_name=? ORDER BY id", (k["norm_name"],)).fetchall():
            if same_place(dict(r), b):
                return r["id"], "name+location"
    return None


def upsert_business(b: dict, area: str | None = None) -> str:
    """Store a discovered business record. Returns 'added', 'merged' (new source record
    for a known business) or 'seen' (source record already known)."""
    ts = now()
    raw = json.dumps(b.get("raw", {}))
    with _lock:
        c = conn()
        match = find_existing(b)
        if match is None:
            k = keys(b)
            cur = c.execute(
                """INSERT INTO leads (source, source_id, name, industry, address, city, lat, lon, phone, email, website, raw,
                                      discovered_at, last_seen_at, norm_name, norm_domain, norm_phone)
                   VALUES (:source, :source_id, :name, :industry, :address, :city, :lat, :lon, :phone, :email, :website, :raw,
                           :ts, :ts, :norm_name, :norm_domain, :norm_phone)""",
                {**b, **k, "raw": raw, "ts": ts},
            )
            lead_id, outcome = cur.lastrowid, "added"
        else:
            lead_id, rule = match
            outcome = "seen" if rule == "source_id" else "merged"
            existing = dict(c.execute("SELECT website, phone FROM leads WHERE id=?", (lead_id,)).fetchone())
            # Fill gaps only; never overwrite what we already know.
            c.execute(
                """UPDATE leads SET last_seen_at=:ts, phone=COALESCE(phone, :phone), email=COALESCE(email, :email),
                       website=COALESCE(website, :website), address=COALESCE(address, :address), city=COALESCE(city, :city),
                       lat=COALESCE(lat, :lat), lon=COALESCE(lon, :lon) WHERE id=:id""",
                {**b, "ts": ts, "id": lead_id},
            )
            if not existing["website"] and b.get("website"):
                # Newly learned website: the old audit ("no website") is stale; re-audit.
                c.execute("UPDATE leads SET analyzed_at=NULL WHERE id=?", (lead_id,))
            if b.get("phone") and not existing["phone"]:
                c.execute("UPDATE leads SET norm_phone=? WHERE id=?", (keys(b)["norm_phone"], lead_id))
            if b.get("website") and not existing["website"]:
                c.execute("UPDATE leads SET norm_domain=? WHERE id=?", (keys(b)["norm_domain"], lead_id))
        c.execute(
            """INSERT INTO lead_sources (lead_id, source, source_id, industry, area, raw, first_seen_at, last_seen_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (source, source_id) DO UPDATE SET last_seen_at=excluded.last_seen_at, raw=excluded.raw""",
            (lead_id, b["source"], b["source_id"], b["industry"], area, raw, ts, ts),
        )
        c.commit()
    return outcome


def lead_scope(lead_id: int) -> tuple[set[str], set[str]]:
    """(industries, areas) the business was discovered under, across all sources."""
    rows = conn().execute("SELECT industry, area FROM lead_sources WHERE lead_id=?", (lead_id,)).fetchall()
    return {r[0] for r in rows if r[0]}, {r[1] for r in rows if r[1]}


def set_website(lead_id: int, url: str) -> None:
    """Record a person's correction of a business's website. It overrides the source
    data, is kept as a 'manual' source record for provenance, and the stale audit is
    cleared so the next audit uses the corrected site."""
    ts = now()
    with _lock:
        c = conn()
        industry = c.execute("SELECT industry FROM leads WHERE id=?", (lead_id,)).fetchone()[0]
        c.execute("UPDATE leads SET website=?, norm_domain=?, final_url=NULL, analyzed_at=NULL WHERE id=?",
                  (url, keys({"website": url})["norm_domain"], lead_id))
        c.execute(
            """INSERT INTO lead_sources (lead_id, source, source_id, industry, area, raw, first_seen_at, last_seen_at)
               VALUES (?, 'manual', ?, ?, NULL, ?, ?, ?)
               ON CONFLICT (source, source_id) DO UPDATE SET raw=excluded.raw, last_seen_at=excluded.last_seen_at""",
            (lead_id, f"website/{lead_id}", industry, json.dumps({"website": url, "corrected_at": ts}), ts, ts),
        )
        c.commit()


def save_analysis(lead_id: int, result: dict) -> None:
    """Store the customer-independent website audit."""
    from .analysis.website import AUDIT_VERSION
    _write(
        """UPDATE leads SET analyzed_at=?, website_status=?, final_url=?, website=COALESCE(website, ?),
           email=COALESCE(email, ?), findings=?, audit_version=? WHERE id=?""",
        (now(), result["website_status"], result.get("final_url"), result.get("website"), result.get("email"),
         json.dumps(result["findings"]), AUDIT_VERSION, lead_id),
    )


def get_business(lead_id: int) -> dict | None:
    return _row(conn().execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone())


def analyzed_business_ids() -> list[int]:
    return [r[0] for r in conn().execute("SELECT id FROM leads WHERE analyzed_at IS NOT NULL").fetchall()]


def unanalyzed(limit: int) -> list[dict]:
    """Businesses needing an audit: never audited first, then ones audited by an older
    version of the checks (so existing leads gain new checks such as broken links)."""
    from .analysis.website import AUDIT_VERSION
    rows = conn().execute(
        """SELECT * FROM leads WHERE analyzed_at IS NULL OR audit_version < ?
           ORDER BY analyzed_at IS NOT NULL, (website IS NULL), discovered_at LIMIT ?""", (AUDIT_VERSION, limit)
    ).fetchall()
    return [_row(r) for r in rows]


def stale_audits() -> int:
    from .analysis.website import AUDIT_VERSION
    return conn().execute("SELECT COUNT(*) FROM leads WHERE analyzed_at IS NOT NULL AND audit_version < ?",
                          (AUDIT_VERSION,)).fetchone()[0]


# ---------------------------------------------------------------- discovery scans


def start_scan(adapter: str, area: str, industry: str, mode: str, since: str | None) -> int:
    return _write(
        "INSERT INTO discovery_scans (adapter, area, industry, mode, since, started_at, status) VALUES (?, ?, ?, ?, ?, ?, 'running')",
        (adapter, area, industry, mode, since, now()),
    ).lastrowid


def finish_scan(scan_id: int, status: str, found: int = 0, added: int = 0, merged: int = 0, error: str | None = None) -> None:
    _write("UPDATE discovery_scans SET finished_at=?, status=?, found=?, added=?, merged=?, error=? WHERE id=?",
           (now(), status, found, added, merged, error, scan_id))


def scan_history(adapter: str, area: str, industry: str) -> dict:
    """Last successful scan start time and failures since then."""
    c = conn()
    ok = c.execute("""SELECT id, started_at FROM discovery_scans WHERE adapter=? AND area=? AND industry=? AND status='ok'
                      ORDER BY id DESC LIMIT 1""", (adapter, area, industry)).fetchone()
    fails = c.execute("""SELECT COUNT(*), MAX(finished_at) FROM discovery_scans WHERE adapter=? AND area=? AND industry=?
                         AND status='error' AND id > ?""", (adapter, area, industry, ok[0] if ok else 0)).fetchone()
    return {"last_ok": ok[1] if ok else None, "failures": fails[0], "last_failure": fails[1]}


def recent_scans(limit: int = 50) -> list[dict]:
    return [dict(r) for r in conn().execute("SELECT * FROM discovery_scans ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]


def close_stale_scans() -> None:
    """Scans left 'running' by a crash or restart are marked failed so they get retried."""
    _write("UPDATE discovery_scans SET status='error', error='interrupted', finished_at=? WHERE status='running'", (now(),))


# ---------------------------------------------------------------- customers


def sync_customer(profile: dict) -> tuple[int, bool]:
    """Insert or update a customer from its profile. Returns (customer_id, profile_changed)."""
    h = profile_hash(profile)
    with _lock:
        c = conn()
        r = c.execute("SELECT id, profile_hash FROM customers WHERE slug=?", (profile["slug"],)).fetchone()
        if r and r["profile_hash"] == h:
            return r["id"], False
        if r:
            c.execute("UPDATE customers SET name=?, profile=?, profile_hash=?, active=?, updated_at=? WHERE id=?",
                      (profile["name"], json.dumps(profile), h, int(profile["active"]), now(), r["id"]))
            cid = r["id"]
        else:
            cid = c.execute("""INSERT INTO customers (slug, name, profile, profile_hash, active, created_at, updated_at)
                               VALUES (?, ?, ?, ?, ?, ?, ?)""",
                            (profile["slug"], profile["name"], json.dumps(profile), h, int(profile["active"]), now(), now())).lastrowid
        c.commit()
        return cid, True


def set_scoring_version(customer_id: int, version: int) -> None:
    _write("UPDATE customers SET scoring_version=? WHERE id=?", (version, customer_id))


def get_customer(slug: str) -> dict | None:
    return _row(conn().execute("SELECT * FROM customers WHERE slug=?", (slug,)).fetchone())


def list_customers(active_only: bool = True) -> list[dict]:
    q = "SELECT * FROM customers" + (" WHERE active=1" if active_only else "") + " ORDER BY id"
    return [_row(r) for r in conn().execute(q).fetchall()]


# ---------------------------------------------------------------- customer leads


def assign(customer_id: int, lead_id: int, q: dict) -> None:
    """Create or refresh a customer's qualification of a business. Status, notes and
    AI brief are the customer's own work and are never overwritten here."""
    _write(
        """INSERT INTO customer_leads (customer_id, lead_id, score, grade, opportunities, explanation, summary, qualified_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT (customer_id, lead_id) DO UPDATE SET score=excluded.score, grade=excluded.grade,
               opportunities=excluded.opportunities, explanation=excluded.explanation, summary=excluded.summary,
               qualified_at=excluded.qualified_at""",
        (customer_id, lead_id, q["score"], q["grade"], json.dumps(q["opportunities"]), json.dumps(q.get("explanation")),
         q["summary"], now()),
    )


def unassign_untouched(customer_id: int, lead_id: int) -> None:
    """Drop an assignment that no longer fits the profile, unless the customer already worked it."""
    _write("""DELETE FROM customer_leads WHERE customer_id=? AND lead_id=? AND status='new'
              AND notes IS NULL AND ai_brief IS NULL""", (customer_id, lead_id))


def get_lead(customer_id: int, lead_id: int) -> dict | None:
    return _row(conn().execute(f"SELECT {LEAD_SELECT} WHERE cl.customer_id=? AND cl.lead_id=?", (customer_id, lead_id)).fetchone())


def next_lead(customer_id: int, min_score: int = 1, exclude: list[int] | None = None) -> dict | None:
    exclude = exclude or []
    not_in = f"AND l.id NOT IN ({','.join('?' * len(exclude))})" if exclude else ""
    r = conn().execute(
        f"SELECT {LEAD_SELECT} WHERE cl.customer_id=? AND {READY} {not_in} ORDER BY cl.score DESC, l.discovered_at ASC LIMIT 1",
        (customer_id, min_score, *exclude),
    ).fetchone()
    return _row(r)


def list_leads(customer_id: int, status: str | None = None, limit: int = 200) -> list[dict]:
    q = f"SELECT {LEAD_SELECT} WHERE cl.customer_id=? AND l.analyzed_at IS NOT NULL"
    args: list = [customer_id]
    if status:
        q += " AND cl.status=?"
        args.append(status)
    q += " ORDER BY cl.score DESC LIMIT ?"
    args.append(limit)
    return [_row(r) for r in conn().execute(q, args).fetchall()]


def update_lead(customer_id: int, lead_id: int, **fields) -> None:
    fields = {k: v for k, v in fields.items() if k in ("status", "notes", "ai_brief")}
    if not fields:
        return
    if "status" in fields:
        fields["status_changed_at"] = now()
    cols = ", ".join(f"{k}=?" for k in fields)
    _write(f"UPDATE customer_leads SET {cols} WHERE customer_id=? AND lead_id=?", (*fields.values(), customer_id, lead_id))


def counts(customer_id: int, min_score: int = 1) -> dict:
    c = conn()
    return {
        "discovered": c.execute("SELECT COUNT(*) FROM leads").fetchone()[0],
        "analyzed": c.execute("SELECT COUNT(*) FROM leads WHERE analyzed_at IS NOT NULL").fetchone()[0],
        "assigned": c.execute("SELECT COUNT(*) FROM customer_leads WHERE customer_id=?", (customer_id,)).fetchone()[0],
        "ready": c.execute(f"SELECT COUNT(*) FROM customer_leads cl JOIN leads l ON l.id=cl.lead_id WHERE cl.customer_id=? AND {READY}",
                           (customer_id, min_score)).fetchone()[0],
        "by_status": dict(c.execute("SELECT status, COUNT(*) FROM customer_leads WHERE customer_id=? GROUP BY status",
                                    (customer_id,)).fetchall()),
    }
