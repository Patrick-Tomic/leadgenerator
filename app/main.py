"""Atomic Lead Engine web app. Run: .venv/bin/uvicorn app.main:app --reload

Every lead endpoint is scoped to one customer (?customer=<slug>, default from
LEAD_ENGINE_DEFAULT_CUSTOMER). There is no authentication yet: this is a local,
single-operator tool, not a public multi-tenant deployment.
"""
import csv
import io
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import ai, config, db, details, pipeline, registrations
from urllib.parse import urlparse

from .analysis.fetcher import PoliteFetcher
from .analysis.website import normalize_url
from .qualify import qualify_business, sync_profiles
from .findings import TIER_ORDER, enrich
from .scoring import classify_finding
from .websites import STATE_LABEL

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
STATIC = config.BASE_DIR / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.conn()  # opens and migrates (with a backup) if needed
    db.close_stale_scans()
    sync_profiles()
    cust = db.get_customer(config.DEFAULT_CUSTOMER)
    if cust and (db.counts(cust["id"], cust["profile"]["min_score"])["ready"] < cust["profile"]["ready_pool_target"]
                 or db.stale_audits()):
        pipeline.start_refill(cust["slug"])  # also re-audits leads checked by an older audit version
    yield


app = FastAPI(title="Atomic Lead Engine", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def _customer(slug: str | None) -> dict:
    cust = db.get_customer(slug or config.DEFAULT_CUSTOMER)
    if not cust:
        raise HTTPException(404, f"Unknown customer '{slug}'")
    return cust


def _public(lead: dict | None, detailed: bool = True) -> dict | None:
    if not lead:
        return None
    lead = dict(lead)
    findings = lead.get("findings") or []
    lead["facts"] = next((f["facts"] for f in findings if f.get("code") == "_facts"), {})
    lead["findings"] = sorted(({**enrich(f), "kind": classify_finding(f)} for f in findings if f.get("code") != "_facts"),
                              key=lambda f: (TIER_ORDER[f["tier"]], f["confidence"] != "high"))
    lead["industry_label"] = config.INDUSTRIES.get(lead["industry"], {}).get("label", lead["industry"])
    raw = lead.pop("raw", None) or {}
    lead["source_url"] = raw.get("osm_url")
    lead["website_state_label"] = STATE_LABEL.get(lead.get("website_state") or "unknown")
    if detailed:
        # Every field from every source (with attribution), website candidates and conflicts.
        lead["details"] = details.build(lead, lead["facts"])
        lead["source_tags"] = raw.get("tags") or {}
    return lead


def _lead_or_404(cust: dict, lead_id: int) -> dict:
    lead = db.get_lead(cust["id"], lead_id)
    if not lead:  # also the answer when the business exists but isn't this customer's lead
        raise HTTPException(404, "Lead not found")
    return lead


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/customers")
def customers():
    return [{"slug": c["slug"], "name": c["name"]} for c in db.list_customers()]


@app.get("/api/status")
def status(customer: str | None = None):
    cust = _customer(customer)
    p = cust["profile"]
    return {
        **db.counts(cust["id"], p["min_score"]),
        "customer": {"slug": cust["slug"], "name": cust["name"]},
        "pipeline": pipeline.state,
        "ready_target": p["ready_pool_target"],
        "min_score": p["min_score"],
        "region": p["region_label"],
        "areas": {a: config.AREAS[a]["label"] for a in p["areas"]},
        "industries": {k: config.INDUSTRIES[k]["label"] for k in p["industries"]},
        "search_configured": bool(config.BRAVE_API_KEY),
        "ai_configured": ai.available(),
        "stale_audits": db.stale_audits(),
    }


@app.get("/api/scans")
def scans(limit: int = 50):
    return db.recent_scans(limit)


@app.get("/api/next")
async def next_lead(customer: str | None = None, exclude: str = ""):
    # async: start_refill schedules a task on the running event loop, which a plain
    # (threadpool) endpoint doesn't have.
    cust = _customer(customer)
    p = cust["profile"]
    ids = [int(x) for x in exclude.split(",") if x.strip().isdigit()]
    lead = db.next_lead(cust["id"], p["min_score"], ids)
    if db.counts(cust["id"], p["min_score"])["ready"] < p["ready_pool_target"]:
        pipeline.start_refill(cust["slug"])
    return {"lead": _public(lead), "pipeline": pipeline.state}


@app.get("/api/leads")
def leads(customer: str | None = None, status: str | None = Query(None)):
    return [_public(l, detailed=False) for l in db.list_leads(_customer(customer)["id"], status)]


@app.get("/api/leads/{lead_id}")
def lead(lead_id: int, customer: str | None = None):
    return _public(_lead_or_404(_customer(customer), lead_id))


class StatusUpdate(BaseModel):
    status: str | None = None
    notes: str | None = None


@app.post("/api/leads/{lead_id}")
def update(lead_id: int, body: StatusUpdate, customer: str | None = None):
    cust = _customer(customer)
    _lead_or_404(cust, lead_id)
    fields = body.model_dump(exclude_none=True)
    if "status" in fields and fields["status"] not in db.STATUSES:
        raise HTTPException(400, f"status must be one of {db.STATUSES}")
    db.update_lead(cust["id"], lead_id, **fields)
    return _public(db.get_lead(cust["id"], lead_id))


@app.post("/api/leads/{lead_id}/brief")
async def brief(lead_id: int, customer: str | None = None):
    cust = _customer(customer)
    lead = _lead_or_404(cust, lead_id)
    try:
        text = await ai.write_brief(_public(lead), cust["profile"])
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    db.update_lead(cust["id"], lead_id, ai_brief=text)
    return _public(db.get_lead(cust["id"], lead_id))


class PhoneCorrection(BaseModel):
    phone: str


@app.post("/api/leads/{lead_id}/phone")
def correct_phone(lead_id: int, body: PhoneCorrection, customer: str | None = None):
    """A person found the business's phone number: store it (labeled as entered by you) and re-score."""
    from .dedup import normalize_phone
    cust = _customer(customer)
    _lead_or_404(cust, lead_id)
    p = normalize_phone(body.phone)
    if not p:
        raise HTTPException(400, "Enter a 10-digit US phone number.")
    db.set_phone(lead_id, f"({p[:3]}) {p[3:6]}-{p[6:]}", "manual", "entered by a person reviewing the lead", overwrite=True)
    qualify_business(lead_id)
    return _public(db.get_lead(cust["id"], lead_id))


class WebsiteCorrection(BaseModel):
    website: str


@app.post("/api/leads/{lead_id}/website")
async def correct_website(lead_id: int, body: WebsiteCorrection, customer: str | None = None):
    """A person knows the business's real website: store it and re-audit right away."""
    cust = _customer(customer)
    _lead_or_404(cust, lead_id)
    url = normalize_url(body.website)
    host = urlparse(url).hostname or ""
    if "." not in host or " " in url:
        raise HTTPException(400, "That doesn't look like a website address.")
    db.set_website(lead_id, url)
    return await _reaudit(cust, lead_id)


@app.post("/api/leads/{lead_id}/reanalyze")
async def reanalyze(lead_id: int, customer: str | None = None):
    cust = _customer(customer)
    _lead_or_404(cust, lead_id)
    return await _reaudit(cust, lead_id)


async def _reaudit(cust: dict, lead_id: int) -> dict:
    fetcher = PoliteFetcher()
    try:
        db.save_analysis(lead_id, await pipeline.analyze_business(db.get_business(lead_id), fetcher))
    finally:
        await fetcher.aclose()
    qualify_business(lead_id)  # the audit is shared, so every customer's score refreshes
    lead = db.get_lead(cust["id"], lead_id)
    if not lead:
        raise HTTPException(409, "After re-checking, this business no longer fits this customer's profile.")
    return _public(lead)


@app.post("/api/discover")
async def discover(customer: str | None = None):
    started = pipeline.start_refill(_customer(customer)["slug"], force=True)
    return {"started": started, "pipeline": pipeline.state}


@app.get("/api/registrations")
def list_registrations(customer: str | None = None, all: bool = False, limit: int = 200):
    """State registrations, newest first; by default only the customer's areas and industries."""
    cust = _customer(customer)
    rows = registrations.list_registrations(None if all else cust["profile"], limit)
    return {"registrations": rows, "imports": registrations.recent_imports(10),
            "import_dir": str(config.SUNBIZ_IMPORT_DIR)}


@app.post("/api/registrations/import")
def import_registrations(force: bool = False):
    """Import Sunbiz data files you've placed in SUNBIZ_IMPORT_DIR (already-imported files are skipped)."""
    if not config.SUNBIZ_IMPORT_DIR.exists():
        raise HTTPException(404, f"Put Sunbiz data files in {config.SUNBIZ_IMPORT_DIR} first (see README).")
    return {"results": registrations.import_dir(force=force)}


@app.post("/api/licenses/update")
def update_licenses(force: bool = False):
    """Download DBPR's weekly contractor license file if newer, import it, and re-match."""
    from . import licenses
    try:
        dl = licenses.download()
    except Exception as e:
        raise HTTPException(502, f"Couldn't download the DBPR file: {e}")
    return {"download": dl, "import": licenses.import_file(force=force)}


class Promote(BaseModel):
    industry: str | None = None


@app.post("/api/registrations/{reg_id}/promote")
async def promote_registration(reg_id: int, body: Promote, customer: str | None = None):
    cust = _customer(customer)
    try:
        lead_id = registrations.promote(reg_id, body.industry)
    except LookupError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    lead = db.get_lead(cust["id"], lead_id)
    if lead and lead.get("analyzed_at"):
        return _public(lead)
    business = db.get_business(lead_id)
    fetcher = PoliteFetcher()
    try:
        db.save_analysis(lead_id, await pipeline.analyze_business(business, fetcher))
    finally:
        await fetcher.aclose()
    qualify_business(lead_id)
    lead = db.get_lead(cust["id"], lead_id)
    if not lead:
        raise HTTPException(409, "Promoted, but it's outside this customer's areas or industries.")
    return _public(lead)


@app.get("/api/export.csv")
def export(customer: str | None = None, status: str | None = None):
    cust = _customer(customer)
    buf = io.StringIO()
    w = csv.writer(buf)
    # Existing columns keep their order; evidence columns are appended.
    w.writerow(["score", "grade", "status", "name", "industry", "phone", "email", "website", "address",
                "top_opportunities", "opportunity_basis", "summary", "notes",
                "strongest_issue", "evidence", "confidence", "affected_page", "why_it_may_matter", "suggested_service",
                "talking_point", "main_uncertainty", "verify_manually", "website_state", "website_conflict"])
    for l in db.list_leads(cust["id"], status, limit=10000):
        opps = l.get("opportunities") or []
        ex = l.get("explanation") or {}
        st = ex.get("strongest") or {}
        w.writerow([l["score"], l["grade"], l["status"], l["name"], l["industry"], l["phone"], l["email"],
                    l.get("final_url") or l["website"], l["address"],
                    "; ".join(o["label"] for o in opps), "; ".join(o.get("basis", "") for o in opps), l["summary"], l["notes"],
                    st.get("title"), st.get("evidence"), st.get("confidence_level"), st.get("page"), st.get("impact"),
                    st.get("service"), st.get("talking_point"), ex.get("uncertainty"), " | ".join(ex.get("verify") or []),
                    l.get("website_state") or "unknown", "yes" if l.get("website_conflict") else ""])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f"attachment; filename=leads-{cust['slug']}.csv"})
