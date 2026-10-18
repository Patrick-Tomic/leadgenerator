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

Other knobs (region bounding box, industries, pool size, politeness delays) are in `app/config.py` and can be overridden with environment variables.

## Using it

- **Next lead**: the highest-scoring unreviewed lead. It shows a summary, contact details, each opportunity with the evidence behind it, and all raw findings. You can mark the lead *contacted*, *save* it, mark it *not a fit*, or *skip* it, and the next one loads. **Re-check site** re-runs the audit.
- **Pipeline**: every analyzed lead, filterable by status, with CSV export.

## How it works

```
discovery adapters ──► SQLite ──► website analyzer ──► scoring ──► "next lead"
 (OSM/Overpass)                    (polite fetcher)     (evidence-weighted)
 (Brave: optional website finder)
```

1. **Discover** (`app/discovery/`). The Overpass API is queried one industry at a time inside the Tampa Bay bounding box. Industries: HVAC, plumbing, electrical, roofing, general contractors, pool service, pressure washing, landscaping, auto repair, cleaning, pest control, and locksmiths. Results are cached for 7 days per industry. Chains and franchises (OSM `brand` tags) are skipped because they rarely buy local web work.
2. **Analyze** (`app/analysis/`). For each business the analyzer:
   - **Reachability:** DNS, SSL, timeouts, HTTP errors, redirect loops, and an HTTPS check.
   - **Placeholder pages:** parked, placeholder, or suspended pages, and listings that point to a social profile instead of a site.
   - **Mobile:** whether a viewport tag exists.
   - **Outdated tech:** frames, Flash, legacy tags, jQuery 1.x, WordPress older than 6.0, stale copyright years.
   - **SEO:** title, meta description, H1, `noindex`, image alt text, schema.org markup, sitemap.
   - **Lead capture:** contact or quote forms (including the contact page when the homepage has no form), `mailto:` and insecure forms, click-to-call links.
   - **Booking and chat:** scheduling tools (Calendly, Housecall Pro, Jobber, ServiceTitan, and others) and chat widgets.
3. **Score** (`app/scoring.py`). Findings map to seven services: new website, repair, redesign, lead capture, booking automation, AI chatbot, and SEO. Points are severity × confidence, capped per service, with a small bonus when a phone number or email is available.

### Guarding against false leads

The spec says not to assume a small business needs help, so the scoring is built to resist that:

- A well-built site ends up with only weak signals such as "no chat widget", and it scores low (grade D).
- Every finding records the exact evidence, for example *"Form action is mailto:joe@…"*, so you can check it before you pitch.
- Uncertain cases are marked low confidence and score less. These include:
  - a missing website that search hasn't confirmed;
  - HTTP 403 or 503 responses, which are often bot protection rather than a broken site;
  - a domain that no longer resolves, since the business may have moved;
  - JavaScript-rendered pages, whose content the analyzer can't see.

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

To add a source, subclass `DiscoveryAdapter` (or `WebsiteFinder`) in `app/discovery/base.py` and register it in `app/pipeline.py`. To add an industry, add an entry with Overpass selectors to `INDUSTRIES` in `app/config.py`.

## Compliance notes

- The crawler identifies itself with an honest User-Agent and honors robots.txt. When robots.txt disallows it, the site is skipped and flagged for manual review.
- Requests are spaced at least 2 seconds apart per host, at most 4 sites are checked concurrently, and pages are capped at 3 MB.
- Only business contact details that the business publishes (phone, website, `mailto:` addresses on its own site) are stored. No personal data is collected.
- Outreach by email must follow CAN-SPAM: identify yourself, include a physical address, and honor opt-outs. Calling or texting mobile numbers is subject to the TCPA and Florida's telemarketing laws. Check before cold-calling at scale.

## Development

```bash
.venv/bin/python -m pytest -q        # unit tests for checks, scoring, parsing
.venv/bin/uvicorn app.main:app --reload
```

Project layout: `app/main.py` (API and static UI), `app/pipeline.py` (orchestration), `app/discovery/` (sources), `app/analysis/` (fetcher and checks), `app/scoring.py`, `app/ai.py`, `app/db.py` (SQLite at `data/leads.db`), `static/` (UI).
# leadgenerator
