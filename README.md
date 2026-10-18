# Atomic Lead Engine

Opens on a researched business lead you could pitch today. The app discovers small local service businesses in Tampa Bay, audits their public web presence, and ranks them by evidence-backed opportunities for Atomic Consulting and Solutions. You never supply names or lists.

## Quick start

```bash
./run.sh            # creates .venv on first run, serves http://localhost:8000
```

On first launch the app starts discovering and analyzing in the background. The first leads appear within a few minutes, because each site is checked politely with a 2-second gap per host. After that, the app keeps about 15 analyzed leads ready and refills the pool as you work through them.

Optional settings go in a `.env` file (see `.env.example`):

| Variable | What it enables |
|---|---|
| `LEAD_ENGINE_CONTACT` | Your URL or email in the crawler's User-Agent, so site owners can reach you. Recommended. |
| `BRAVE_API_KEY` | Website lookup for businesses listed without one. "No website" leads change from low confidence (*unverified*) to high confidence (*confirmed by search*). |
| `ANTHROPIC_API_KEY` | A **Write AI brief** button: Claude (`claude-opus-5-5`) drafts talking points and a first outreach email from the collected evidence. It runs only when you click it. The request opts in to server-side refusal fallbacks. |

What to look for (service areas, industries, offerings, exclusions, scoring weights) is set per customer in `profiles/*.json` (see [Customer profiles](#customer-profiles)). Operational knobs (politeness delays, scan freshness, retry cooldowns) are in `app/config.py` and can be overridden with environment variables.

## Using it

- **Next lead**: the highest-scoring unreviewed lead. It shows a summary, contact details, each opportunity with the evidence behind it, and all raw findings. You can mark the lead *contacted*, *save* it, mark it *not a fit*, or *skip* it, and the next one loads. **Re-check site** re-runs the audit.
- **Pipeline**: every analyzed lead, filterable by status, with CSV export.
- **Customer selector** (header): appears when more than one profile exists. Leads, statuses, notes, AI briefs and exports are all per customer.
- Each finding is labeled *confirmed issue* (observed with high confidence), *potential* (missing optional feature or lower-confidence observation), or *couldn't check* (bot protection, robots.txt, unreadable page). Each opportunity shows whether any confirmed finding backs it.

## How it works

```
                 generic (shared by all customers)                 per customer
discovery scans ──► dedup ──► leads ──► website audit ──► qualification ──► "next lead"
(area x industry)  (domain,    (SQLite)  (findings with    (profile: scope,    (customer_leads:
 OSM/Overpass       phone,                evidence +        offerings,          score, status,
                    name+place)           confidence)       weights)            notes, brief)
```

1. **Discover** (`app/discovery/`, `app/pipeline.py`). Discovery runs as *scans*: one adapter × one named area (county-sized boxes in `config.AREAS`) × one category (25 in `config.INDUSTRIES`). Each scan is logged in `discovery_scans` with mode, counts (found / added / merged) and any error. A scan that never ran is a *full* scan; after `OVERPASS_CACHE_DAYS` (7) it re-runs *incrementally*, fetching only OSM elements edited since the last successful scan (minus a 1-day overlap). Failed scans wait out a cooldown (30 min, doubling, max 24 h) and never stop other scans; 3 failures in a row pause the refill. Chains and franchises (OSM `brand` tags) are skipped because they rarely buy local web work.
   - **Dedup** (`app/dedup.py`): a record is the same business if it has the same source ID, the same website domain (social/directory hosts ignored), the same 10-digit phone, or the same normalized name within 300 m. Merges only fill missing fields; a newly learned website triggers a re-audit. Every source record is kept in `lead_sources`.
2. **Analyze** (`app/analysis/`). For each business the analyzer:
   - **Reachability:** DNS, SSL, timeouts, HTTP errors, redirect loops, and an HTTPS check.
   - **Placeholder pages:** parked, placeholder, or suspended pages, and listings that point to a social profile instead of a site.
   - **Mobile:** whether a viewport tag exists.
   - **Outdated tech:** frames, Flash, legacy tags, jQuery 1.x, WordPress older than 6.0, stale copyright years.
   - **SEO:** title, meta description, H1, `noindex`, image alt text, schema.org markup, sitemap.
   - **Lead capture:** contact or quote forms (including the contact page when the homepage has no form), `mailto:` and insecure forms, click-to-call links.
   - **Booking and chat:** scheduling tools (Calendly, Housecall Pro, Jobber, ServiceTitan, and others) and chat widgets.
3. **Qualify** (`app/qualify.py`, `app/scoring.py`). For each customer, a business is in scope if its category is targeted, its location is inside one of the customer's areas, and no exclusion matches. Findings then map to the customer's offerings. Points are severity × confidence, capped per offering, with a small bonus when a phone number or listed email is available. Atomic's profile reproduces the v0.1 scoring exactly. Re-qualifying is pure computation (no network), so changing a profile is cheap.

### Guarding against false leads

The spec says not to assume a small business needs help, so the scoring is built to resist that:

- A well-built site ends up with only weak signals such as "no chat widget", and it scores low (grade D).
- Every finding records the exact evidence, for example *"Form action is mailto:joe@…"*, so you can check it before you pitch.
- Uncertain cases are marked low confidence and score less. These include:
  - a missing website that search hasn't confirmed;
  - HTTP 403 or 503 responses, which are often bot protection rather than a broken site;
  - a domain that no longer resolves, since the business may have moved;
  - JavaScript-rendered pages, whose content the analyzer can't see.

## Customer profiles

Each JSON file in `profiles/` is a customer, synced into the database at startup. `profiles/atomic.json` is the default; `profiles/examples/example-pool-marketing.json` shows a second customer (copy it into `profiles/` to activate it).

| Field | Meaning |
|---|---|
| `slug`, `name`, `description` | Identity; `description` and offering labels feed the AI brief prompt. |
| `areas`, `industries` | Keys from `config.AREAS` / `config.INDUSTRIES`. Unknown keys are rejected at startup. |
| `exclusions` | `name_patterns` (regex), `domains`, `website_statuses` (e.g. `"restricted"`). |
| `offerings` | Each: `label`, `pitch`, `codes` (audit findings that indicate a need), `cap` (max points). |
| `scoring` | `severity_points`, `confidence_mult`, `point_overrides`, `contact_bonus`, `unreachable_multiplier`, `grade_thresholds`. |
| `min_score`, `ready_pool_target` | Lowest score served as a "next lead"; how many ready leads to keep in stock. |

When a profile changes, that customer's leads are re-scored. Statuses, notes and AI briefs are kept; leads that fall out of scope are removed only if the customer never touched them.

There is no authentication: this is a local single-operator tool. Customer isolation is enforced in every query (`customer_leads` is always filtered by customer), not by login.

## Database and migrations

SQLite at `data/leads.db`. Schema versions are tracked with `PRAGMA user_version` and applied automatically at startup by `app/migrations.py`. Migrations are additive (no tables or columns dropped), each runs in a single transaction, and **before any migration runs on a database that has leads, a full copy is saved to `data/backups/leads-v<old version>-<timestamp>.db`**. A database newer than the code is refused rather than modified.

v1 (this release) adds `customers`, `customer_leads`, `lead_sources`, `discovery_scans` and dedup keys on `leads`. Existing workflow state (score, status, notes, AI brief) is copied to the default customer unchanged. The original columns on `leads` are left as they were, frozen, so the backup or the old code can still be used; the app no longer writes them.

**Stop the old server before upgrading.** A v0.1 process still running against a migrated database would keep writing statuses to the frozen columns, where v0.2 doesn't read them.

## Rate limits, limitations and costs

| Item | Limit / behavior |
|---|---|
| Overpass (public instance) | Documented guidance: well under 10,000 queries and 1 GB per day; HTTP 429 when busy. The app runs one query at a time, ≥10 s apart (`OVERPASS_MIN_INTERVAL`), retries 429/504 twice with backoff, and treats Overpass "runtime error" remarks (timeouts returned as HTTP 200) as failures. A full sweep for Atomic is 4 areas × 25 categories = 100 queries; measured 15–41 s each including spacing in testing. |
| Overpass pagination | Not supported by the API; every match in the area comes back at once. Areas are county-sized to keep responses small. |
| `newer:` incremental scans | Returns elements *edited* since a date. It doesn't report deletions, so closed businesses stay until marked *not a fit*. |
| County boxes | Approximate rectangles that overlap, so a business near a border can appear in two areas. Dedup handles the overlap, and scope checks use these boxes rather than legal county lines. |
| OSM coverage | Good for storefront trades; weak for home-based businesses (many pool, cleaning and pressure-washing operators aren't mapped). |
| Website audits | ≥2 s between requests to the same host, ≤4 sites at once, 3 MB page cap, robots.txt honored. |

**Operating cost: $0.** OSM and Overpass are free. Brave Search's free tier is optional. Claude is optional and per-click (roughly a few cents per brief at current Opus pricing). SQLite and a local process need no hosting. The only real constraint is the shared public Overpass instance; heavy multi-customer use should move to a self-hosted Overpass or a paid mirror (set `OVERPASS_URL`).

## Data sources evaluated

| Source | Cost | Coverage | Licensing / terms | Verdict |
|---|---|---|---|---|
| **OpenStreetMap via Overpass** | Free | Moderate for trades: good on auto repair and HVAC, patchy on home-based businesses. Often includes phone, website, and address. | ODbL: commercial use OK with attribution (shown in the footer). Fair use of public instances. | **Used, primary.** |
| **Brave Search API** | Free tier ~2k queries/month, 1 q/s; paid after | Web-wide | Results may be used in apps per their API terms | **Used, optional**, to confirm or find official websites |
| Official business websites | Free | Per business | Public pages only; robots.txt honored; no logins, form submissions, or CAPTCHA bypass | **Used** for analysis |
| Google Places API | ~$200/mo free credit, then paid per call | Best coverage | Terms restrict storing data (except place IDs) and require display with Google Maps | Not used in the MVP. Strongest upgrade if you accept the cost and terms. |
| Yelp Fusion API | Limited free trial, now mostly paid | Good for consumer-facing businesses | Display and caching restrictions; no bulk storage | Not used |
| Florida Sunbiz (Division of Corporations) | Free bulk data files | Every registered FL entity | Public records | Good future adapter for brand-new businesses. It has no website or phone data, so it needs enrichment. |
| Scraping Google Maps, Yelp, or YellowPages | Free | High | Prohibited by their terms | **Excluded** |

To add a source, subclass `DiscoveryAdapter` (or `WebsiteFinder`) in `app/discovery/base.py`, raise `SourceError` on failure, set `supports_incremental` if it can fetch only changes, and register it in `app/pipeline.py`. To add a category or area, add it to `INDUSTRIES` or `AREAS` in `app/config.py`, then reference it from a profile.

## Compliance notes

- The crawler identifies itself with an honest User-Agent and honors robots.txt. When robots.txt disallows it, the site is skipped and flagged for manual review.
- Requests are spaced at least 2 seconds apart per host, at most 4 sites are checked concurrently, and pages are capped at 3 MB.
- Only business contact details that the business publishes (phone, website, `mailto:` addresses on its own site) are stored. No personal data is collected.
- Outreach by email must follow CAN-SPAM: identify yourself, include a physical address, and honor opt-outs. Calling or texting mobile numbers is subject to the TCPA and Florida's telemarketing laws. Check before cold-calling at scale.

## Development

```bash
.venv/bin/python -m pytest -q        # audit checks, scoring, migrations, dedup, discovery, customer isolation
.venv/bin/uvicorn app.main:app --reload
```

Project layout: `app/main.py` (API and static UI), `app/pipeline.py` (scan planning and orchestration), `app/discovery/` (sources), `app/dedup.py`, `app/analysis/` (fetcher and checks), `app/qualify.py` + `app/scoring.py` (per-customer qualification), `app/profiles.py` + `profiles/` (customer config), `app/db.py` + `app/migrations.py` (SQLite), `app/ai.py`, `static/` (UI). `GET /api/scans` lists recent discovery scans with errors.
# leadgenerator
