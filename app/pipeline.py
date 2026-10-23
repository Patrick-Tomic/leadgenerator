"""Discover -> audit -> qualify. Keeps each customer's pool of ready leads topped up.

Discovery is planned per (adapter, area, industry) "scan". A scan that never ran
gets a full scan; a stale one (older than OVERPASS_CACHE_DAYS) is re-run
incrementally when the adapter supports it; a failed one waits out a growing
cooldown. Every attempt is logged in discovery_scans with counts and errors.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from . import config, db
from .analysis.fetcher import PoliteFetcher
from .analysis.website import analyze_site, finding, is_social, normalize_url
from .discovery.base import DiscoveryAdapter, SourceError
from .discovery.brave import get_finders
from .discovery.osm import OverpassAdapter
from .discovery.osm_extract import OsmExtractAdapter
from .qualify import qualify_business
from .websites import verify_website

log = logging.getLogger(__name__)

ADAPTERS: list[DiscoveryAdapter] = [OverpassAdapter()]
# Areas with a Geofabrik extract (states) are read from the extract instead: one scan
# covers every category, and the public Overpass server isn't used for them.
EXTRACT_ADAPTER = OsmExtractAdapter()
ALL_CATEGORIES = "*"
# Re-fetch a little before the last scan so edits that reached the source late aren't missed.
INCREMENTAL_OVERLAP = timedelta(days=1)
# Load failures that can be temporary (a dead domain is not in this list).
TRANSIENT_FAILURES = {"site_timeout", "site_connect", "site_http_error", "site_redirect_loop", "site_other"}

state = {"running": False, "step": "idle", "error": None, "last_run": None, "customer": None}
_lock = asyncio.Lock()


def _set(step: str) -> None:
    state["step"] = step
    log.info(step)


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _contains(outer: tuple, inner: tuple) -> bool:
    return outer[0] <= inner[0] and outer[1] <= inner[1] and outer[2] >= inner[2] and outer[3] >= inner[3]


def _last_ok(adapter: str, area: str, industry: str) -> str | None:
    """Last successful scan covering this area: its own, or one of a larger area that contains it."""
    times = [db.scan_history(adapter, area, industry)["last_ok"]]
    for other, spec in config.AREAS.items():
        if other != area and _contains(spec["bbox"], config.AREAS[area]["bbox"]):
            times.append(db.scan_history(adapter, other, industry)["last_ok"])
    times = [t for t in times if t]
    return max(times) if times else None


def plan_scans(profile: dict, now: datetime | None = None) -> list[dict]:
    """Scans due for this customer's areas x industries, never-run first, then oldest."""
    now = now or datetime.now(timezone.utc)
    fresh_for = timedelta(days=config.OVERPASS_CACHE_DAYS)
    due = []
    for area in [a for a in profile["areas"] if config.AREAS[a].get("extract")]:
        h = db.scan_history(EXTRACT_ADAPTER.name, area, ALL_CATEGORIES)
        if h["failures"] and h["last_failure"]:
            cooldown = timedelta(minutes=min(config.SCAN_RETRY_MINUTES * 2 ** (h["failures"] - 1), 24 * 60))
            if now - _ts(h["last_failure"]) < cooldown:
                continue
        if h["last_ok"] and now - _ts(h["last_ok"]) < timedelta(days=config.EXTRACT_REFRESH_DAYS):
            continue
        due.append({"adapter": EXTRACT_ADAPTER, "area": area, "industry": ALL_CATEGORIES, "since": None,
                    "mode": "extract", "last_ok": h["last_ok"] or ""})
    for adapter in ADAPTERS:
        for area in [a for a in profile["areas"] if not config.AREAS[a].get("extract")]:
            for industry in profile["industries"]:
                h = db.scan_history(adapter.name, area, industry)
                if h["failures"]:
                    cooldown = timedelta(minutes=min(config.SCAN_RETRY_MINUTES * 2 ** (h["failures"] - 1), 24 * 60))
                    if h["last_failure"] and now - _ts(h["last_failure"]) < cooldown:
                        continue
                last_ok = _last_ok(adapter.name, area, industry)
                if last_ok and now - _ts(last_ok) < fresh_for:
                    continue
                since = None
                if last_ok and adapter.supports_incremental:
                    since = (_ts(last_ok) - INCREMENTAL_OVERLAP).strftime("%Y-%m-%dT%H:%M:%SZ")
                due.append({"adapter": adapter, "area": area, "industry": industry, "since": since,
                            "mode": "incremental" if since else "full", "last_ok": last_ok or ""})
    due.sort(key=lambda s: (s["last_ok"] != "", s["last_ok"]))
    return due


async def run_scan(scan: dict) -> dict:
    """Run one discovery scan and store results. Never raises for source failures."""
    adapter, area, industry = scan["adapter"], scan["area"], scan["industry"]
    scan_id = db.start_scan(adapter.name, area, industry, scan["mode"], scan["since"])
    what = "all business types" if industry == ALL_CATEGORIES else config.INDUSTRIES[industry]["label"]
    _set(f"Discovering {what} in {config.AREAS[area]['label']} via {adapter.name}"
         + (" (changes only)" if scan["since"] else "")
         + (" (downloading and reading the state file takes several minutes)" if scan["mode"] == "extract" else "") + "…")
    try:
        found = await adapter.discover(industry, area, scan["since"])
    except SourceError as e:
        db.finish_scan(scan_id, "error", error=str(e))
        log.warning("scan %s/%s/%s failed: %s", adapter.name, area, industry, e)
        return {"status": "error", "error": str(e)}
    except Exception as e:  # unexpected: record it, don't kill the pipeline
        log.exception("scan %s/%s/%s crashed", adapter.name, area, industry)
        db.finish_scan(scan_id, "error", error=f"{type(e).__name__}: {e}"[:500])
        return {"status": "error", "error": str(e)}
    outcomes = []
    for i, b in enumerate(found, 1):
        outcomes.append(db.upsert_business(b, area, commit=False))
        if i % 500 == 0:
            db.conn().commit()
            _set(f"Saving {what} in {config.AREAS[area]['label']}: {i:,}/{len(found):,}")
            await asyncio.sleep(0)  # let the web app serve requests between batches
    db.conn().commit()
    stats = {"found": len(found), "added": outcomes.count("added"), "merged": outcomes.count("merged")}
    db.finish_scan(scan_id, "ok", **stats)
    return {"status": "ok", **stats}


async def analyze_business(b: dict, fetcher: PoliteFetcher, finders: list | None = None) -> dict:
    """Customer-independent audit: verify which website (if any) is the business's,
    then audit that site. A missing website field in one source is never treated as
    'no website'; only checks that actually ran can say that."""
    finders = get_finders(background=True) if finders is None else finders
    v = await verify_website(b, fetcher, finders)
    state, best = v["state"], v["best"]
    findings: list[dict] = []
    facts: dict = {"website_state": state, "website_conflict": v["conflict"]}
    status = {"unknown": "unknown", "none_found": "none", "possible_match": "uncertain"}.get(state, "ok")
    from .details import label
    listed_in = sorted({label(s["source"]) for s in db.lead_sources(b["id"])}) if b.get("id") else [b.get("source", "listing")]
    ran = [s for s in v["searches"] if s["ran"]]
    failed = [s for s in v["searches"] if not s["ran"]]

    if state == "unknown":
        why = "no website search source is configured" if not v["searches"] else \
              f"website search failed ({'; '.join(s['error'] for s in failed)[:120]})"
        findings.append(finding("website_unknown", "Website status unknown",
                                f"No website on the {', '.join(listed_in)} record, and {why}. Missing from a listing "
                                "doesn't mean the business has no website.", "low", "low"))
    elif state == "none_found":
        detail = "; ".join(f"{s['source']}: {', '.join(s['queries'])} ({s['results']} results"
                           + (f", only directories such as {', '.join(s['directories'][:3])}" if s["directories"] else "") + ")"
                           for s in ran)
        # One search source can miss a site; two independent ones agreeing is stronger.
        findings.append(finding("no_website_confirmed", "No website found",
                                f"No website on the listing(s) and no matching site from: {detail}.", "high",
                                "high" if len(ran) >= 2 else "medium"))
    elif state == "possible_match":
        cands = [c for c in v["candidates"] if c["check_status"] not in ("social", "directory")]
        findings.append(finding("website_match_uncertain", "Possible website, match uncertain",
                                "; ".join(f"{c['url']} ({c['source']}: {', '.join(c['match_evidence'][-2:])})" for c in cands[:3]),
                                "low", "low"))
    if v["conflict"]:
        real = [c for c in v["candidates"] if c["identity"] in ("high", "medium") and c["check_status"] not in ("dns", "social", "directory")]
        findings.append(finding("website_conflict", "Sources disagree about the website",
                                "; ".join(f"{c['url']} from {c['source']}" for c in real[:4]), "low", "high"))

    if best and state in ("verified", "found_unverified", "unreachable"):
        site = await analyze_site(best["url"], fetcher, listing_phone=b.get("phone"))
        status = site["website_status"]
        facts.update(site["facts"])
        # Two failures seconds apart can be one short outage. A load failure is only
        # verified if the previous audit (hours or days earlier) also found the site down.
        was_down = b.get("website_status") == "down" and b.get("analyzed_at")
        for f in site["findings"]:
            if f["code"] in TRANSIENT_FAILURES and f["confidence"] == "high" and not was_down:
                f["confidence"] = "medium"
                f["evidence"] += " First audit to see this; it counts as verified if the next audit also fails."
            elif f["code"] in TRANSIENT_FAILURES and was_down:
                f["evidence"] += f" Also down at the previous audit ({b['analyzed_at'][:10]})."
        findings += site["findings"]
        # A phone shown on a site verified as the business's own is the business's published
        # number: save it when no source listed one (it never replaces a listed phone).
        site_phones = facts.get("site_phones") or []
        if state == "verified" and site_phones and not b.get("phone") and b.get("id"):
            p = site_phones[0]
            shown = f"({p[:3]}) {p[3:6]}-{p[6:]}"
            if db.set_phone(b["id"], shown, "website", f"shown on the business's own website ({best['url']}), verified as theirs",
                            overwrite=False):
                b["phone"] = shown
                findings.append(finding("phone_from_website", "Phone number taken from the business's website",
                                        f"No listing had a phone; {shown} is shown on {best['url']}.", "low", "high", page=best["url"]))
        if state == "found_unverified":
            findings.insert(0, finding("website_identity_unconfirmed", "Website found, not yet verified",
                                       f"{best['url']} ({best['source']}): {'; '.join(best['match_evidence'][-3:])}. "
                                       "Its pages didn't confirm the business's phone or address.", "low", "high",
                                       page=best["url"]))
        if best["source"] not in ("osm", "manual") and not best["source"].startswith("sunbiz"):
            findings.insert(0, finding("website_found_by_search", "Website found by search",
                                       f"Not on the listing; found via {best['source']}: {'; '.join(best['match_evidence'])}.",
                                       "low", "high", page=best["url"]))
    elif best and best["check_status"] == "social":
        # The only web presence we found is a social profile. "No own site" is verified
        # only if a search also came up empty.
        findings.append(finding("social_only", "Only a social media page found",
                                f"Listed website is {best['url']}.", "high", "high" if ran else "medium", page=best["url"]))
        status = "social_only"

    # A state registration is a legal entity; it counts as a sales signal only with
    # evidence the business is operating (another independent listing or a live website).
    sources = db.lead_sources(b["id"]) if b.get("id") else []
    reg = next((s for s in sources if s["source"].startswith("sunbiz")), None)
    if reg:
        rf = reg.get("fields") or {}
        from .details import label
        others = sorted({label(s["source"]) for s in sources if not s["source"].startswith("sunbiz") and s["source"] != "manual"})
        operating = others or state in ("verified", "found_unverified")
        lic = next(((s.get("fields") or {}) for s in sources if s["source"] == "dbpr_cilb"), {})
        if lic.get("licensed_since") and rf.get("filing_date") and lic["licensed_since"] < rf["filing_date"][:4] + "-01-01":
            rf = {**rf, "entity_type": f"{rf.get('entity_type')}; state-licensed since {lic['licensed_since']}, so an established company"}
        evidence = (f"Filed {rf.get('filing_date')} as {rf.get('entity_type')} (document {rf.get('doc_number')}). "
                    + (f"Operating evidence: listed on {', '.join(others)}" if others else
                       "Operating evidence: a website was found" if operating else
                       "No evidence yet that it's operating; the filed address may be a registered or home address."))
        findings.append(finding("new_registration", "Recently registered business", evidence, "medium",
                                "high" if operating else "low"))
    facts["website_candidates"] = len(v["candidates"])
    return {
        "website_status": status,
        "final_url": facts.get("final_url"),
        "website": None,  # the canonical site is persisted by verify_website
        "email": b.get("email") or (facts.get("emails") or [None])[0],
        "findings": findings + [{"code": "_facts", "facts": facts}],
    }


async def audit_and_qualify(b: dict, fetcher: PoliteFetcher) -> None:
    try:
        result = await asyncio.wait_for(analyze_business(b, fetcher), timeout=120)
    except Exception as e:
        log.exception("analysis failed for %s", b["name"])
        result = {"website_status": "error", "findings": [finding("analysis_error", "Analysis failed", str(e)[:200], "low", "low")]}
    db.save_analysis(b["id"], result)
    qualify_business(b["id"])


async def analyze_pending(limit: int) -> int:
    pending = await asyncio.to_thread(db.unanalyzed, limit)
    if not pending:
        return 0
    fetcher = PoliteFetcher()
    sem = asyncio.Semaphore(config.MAX_CONCURRENT_SITES)
    done = 0

    async def one(b: dict) -> None:
        nonlocal done
        async with sem:
            await audit_and_qualify(b, fetcher)
            done += 1
            _set(f"Analyzed {done}/{len(pending)} businesses (latest: {b['name']})")

    try:
        await asyncio.gather(*(one(b) for b in pending))
    finally:
        await fetcher.aclose()
    return done


async def refill(customer_slug: str, force: bool = False) -> None:
    """Discover and audit until the customer has enough ready leads, or nothing is due."""
    if _lock.locked():
        return
    async with _lock:
        state.update(running=True, error=None, customer=customer_slug)
        try:
            cust = db.get_customer(customer_slug)
            profile = cust["profile"]
            target = profile["ready_pool_target"]
            failures, rounds = 0, 0
            # Also keep going while older audits are waiting for a re-check with the current checks.
            # Database work runs in a worker thread so the web server's event loop never waits
            # on it (a slow query here once kept the app from finishing startup).
            async def needs_work() -> bool:
                ready = (await asyncio.to_thread(db.counts, cust["id"], profile["min_score"]))["ready"]
                return ready < target or await asyncio.to_thread(db.stale_audits) > 0

            while (force and rounds == 0) or await needs_work():
                rounds += 1
                if rounds > 500:
                    break
                plan = await asyncio.to_thread(plan_scans, profile)
                # Statewide extract scans run before website checks, so every state is loaded
                # up front instead of waiting for tens of thousands of checks in the first one.
                extract_due = [s for s in plan if s["mode"] == "extract"]
                if extract_due:
                    plan = extract_due
                elif await asyncio.to_thread(db.unanalyzed, 1):
                    await analyze_pending(config.ANALYZE_BATCH)
                    continue
                if not plan:
                    _set("All scans for this profile are up to date; nothing new to discover yet.")
                    break
                result = await run_scan(plan[0])
                if result["status"] == "error":
                    failures += 1
                    if failures >= config.MAX_CONSECUTIVE_SCAN_FAILURES:
                        raise SourceError(f"{failures} discovery scans failed in a row; last error: {result['error']}")
                else:
                    failures = 0
            _set("Idle")
        except Exception as e:
            log.exception("pipeline stopped")
            state["error"] = str(e)
            _set(f"Stopped: {e}")
        finally:
            state.update(running=False, last_run=db.now())


def start_refill(customer_slug: str, force: bool = False) -> bool:
    if state["running"]:
        return False
    asyncio.get_running_loop().create_task(refill(customer_slug, force))
    state["running"] = True
    return True
