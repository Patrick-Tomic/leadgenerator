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

from . import ai, config, db, pipeline
from .analysis.fetcher import PoliteFetcher
from .qualify import qualify_business, sync_profiles
from .scoring import classify_finding

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
STATIC = config.BASE_DIR / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.conn()  # opens and migrates (with a backup) if needed
    db.close_stale_scans()
    sync_profiles()
    cust = db.get_customer(config.DEFAULT_CUSTOMER)
    if cust and db.counts(cust["id"], cust["profile"]["min_score"])["ready"] < cust["profile"]["ready_pool_target"]:
        pipeline.start_refill(cust["slug"])
    yield


app = FastAPI(title="Atomic Lead Engine", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def _customer(slug: str | None) -> dict:
    cust = db.get_customer(slug or config.DEFAULT_CUSTOMER)
    if not cust:
        raise HTTPException(404, f"Unknown customer '{slug}'")
    return cust


def _public(lead: dict | None) -> dict | None:
    if not lead:
        return None
    lead = dict(lead)
    findings = lead.get("findings") or []
    lead["facts"] = next((f["facts"] for f in findings if f.get("code") == "_facts"), {})
    lead["findings"] = [{**f, "kind": classify_finding(f)} for f in findings if f.get("code") != "_facts"]
    lead["industry_label"] = config.INDUSTRIES.get(lead["industry"], {}).get("label", lead["industry"])
    raw = lead.pop("raw", None) or {}
    lead["source_url"] = raw.get("osm_url")
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
        "region": p["region_label"],
        "areas": {a: config.AREAS[a]["label"] for a in p["areas"]},
        "industries": {k: config.INDUSTRIES[k]["label"] for k in p["industries"]},
        "search_configured": bool(config.BRAVE_API_KEY),
        "ai_configured": ai.available(),
    }


@app.get("/api/scans")
def scans(limit: int = 50):
    return db.recent_scans(limit)


@app.get("/api/next")
def next_lead(customer: str | None = None, exclude: str = ""):
    cust = _customer(customer)
    p = cust["profile"]
    ids = [int(x) for x in exclude.split(",") if x.strip().isdigit()]
    lead = db.next_lead(cust["id"], p["min_score"], ids)
    if db.counts(cust["id"], p["min_score"])["ready"] < p["ready_pool_target"]:
        pipeline.start_refill(cust["slug"])
    return {"lead": _public(lead), "pipeline": pipeline.state}


@app.get("/api/leads")
def leads(customer: str | None = None, status: str | None = Query(None)):
    return [_public(l) for l in db.list_leads(_customer(customer)["id"], status)]


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


@app.post("/api/leads/{lead_id}/reanalyze")
async def reanalyze(lead_id: int, customer: str | None = None):
    cust = _customer(customer)
    _lead_or_404(cust, lead_id)
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
def discover(customer: str | None = None):
    started = pipeline.start_refill(_customer(customer)["slug"], force=True)
    return {"started": started, "pipeline": pipeline.state}


@app.get("/api/export.csv")
def export(customer: str | None = None, status: str | None = None):
    cust = _customer(customer)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["score", "grade", "status", "name", "industry", "phone", "email", "website", "address",
                "top_opportunities", "opportunity_basis", "summary", "notes"])
    for l in db.list_leads(cust["id"], status, limit=10000):
        opps = l.get("opportunities") or []
        w.writerow([l["score"], l["grade"], l["status"], l["name"], l["industry"], l["phone"], l["email"],
                    l.get("final_url") or l["website"], l["address"],
                    "; ".join(o["label"] for o in opps), "; ".join(o.get("basis", "") for o in opps), l["summary"], l["notes"]])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f"attachment; filename=leads-{cust['slug']}.csv"})
