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
from .discovery.brave import DIRECTORY_DOMAINS, _registrable, get_finder
from .discovery.osm import OverpassAdapter
from .qualify import qualify_business

log = logging.getLogger(__name__)

ADAPTERS: list[DiscoveryAdapter] = [OverpassAdapter()]
# Re-fetch a little before the last scan so edits that reached the source late aren't missed.
INCREMENTAL_OVERLAP = timedelta(days=1)

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
    for adapter in ADAPTERS:
        for area in profile["areas"]:
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
    _set(f"Discovering {config.INDUSTRIES[industry]['label']} in {config.AREAS[area]['label']} via {adapter.name}"
         + (" (changes only)" if scan["since"] else "") + "…")
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
    outcomes = [db.upsert_business(b, area) for b in found]
    stats = {"found": len(found), "added": outcomes.count("added"), "merged": outcomes.count("merged")}
    db.finish_scan(scan_id, "ok", **stats)
    return {"status": "ok", **stats}


async def analyze_business(b: dict, fetcher: PoliteFetcher) -> dict:
    """Customer-independent website audit for one business."""
    findings: list[dict] = []
    website = b.get("website")
    status = "ok"
    facts: dict = {}
    found_site = None

    if not website:
        finder = get_finder()
        if finder:
            try:
                r = await finder.find_website(b)
                if r["website"]:
                    website = found_site = r["website"]
                else:
                    listed = sorted({_registrable(urlparse(u).netloc) for u in r["checked"]} & DIRECTORY_DOMAINS)
                    findings.append(finding("no_website_confirmed", "No business website",
                                            f'Searched "{r["query"]}": top {len(r["checked"])} results were only directory/social listings'
                                            + (f" ({', '.join(listed[:5])})" if listed else "") + ".", "high", "high"))
                    status = "none"
            except Exception as e:  # search is best-effort
                log.warning("website search failed for %s: %s", b["name"], e)
        if not website and status != "none":
            findings.append(finding("no_website_unverified", "No website listed",
                                    "OpenStreetMap lists no website for this business. Not confirmed by web search; check Google before pitching.",
                                    "high", "low"))
            status = "unverified"

    if website:
        if is_social(normalize_url(website)):
            status = "social_only"
            findings.append(finding("social_only", "No real website (only a social media page)",
                                    f"Listed website is {website}.", "high"))
        else:
            site = await analyze_site(website, fetcher)
            status, facts = site["website_status"], site["facts"]
            findings += site["findings"]
            if found_site:
                findings.insert(0, finding("website_found_by_search", "Website found via search",
                                           f"Not listed in map data; matched {found_site} in search results.", "low", "medium"))

    return {
        "website_status": status,
        "final_url": facts.get("final_url"),
        "website": found_site,
        "email": b.get("email") or (facts.get("emails") or [None])[0],
        "findings": findings + ([{"code": "_facts", "facts": facts}] if facts else []),
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
    pending = db.unanalyzed(limit)
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
            while (force and rounds == 0) or db.counts(cust["id"], profile["min_score"])["ready"] < target:
                rounds += 1
                if rounds > 500:
                    break
                if db.unanalyzed(1):
                    await analyze_pending(config.ANALYZE_BATCH)
                    continue
                plan = plan_scans(profile)
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
