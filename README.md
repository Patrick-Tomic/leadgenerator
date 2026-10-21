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
| `BRAVE_API_KEY` | Web search (free tier) for website candidates when a business's own records list none. Searches by name + city and by phone. Without any search source, those businesses stay *website status unknown* and are never pitched a website. |
| `GOOGLE_PLACES_API_KEY` | **Paid, off by default.** Google Places (New) Text Search as a second website finder. Capped by `GOOGLE_PLACES_MONTHLY_LIMIT` (default 900 calls/month, under the 1,000 free Enterprise calls). See [costs](#rate-limits-limitations-and-costs). |
| `SUNBIZ_IMPORT_DIR` | Where you put downloaded Florida Sunbiz data files (default `data/sunbiz`). See [Florida registrations](#florida-registrations-sunbiz). |
| `ANTHROPIC_API_KEY` | A **Write AI brief** button: Claude (`claude-opus-5-5`) drafts talking points and a first outreach email from the collected evidence. It runs only when you click it. The request opts in to server-side refusal fallbacks. |

What to look for (service areas, industries, offerings, exclusions, scoring weights) is set per customer in `profiles/*.json` (see [Customer profiles](#customer-profiles)). Operational knobs (politeness delays, scan freshness, retry cooldowns) are in `app/config.py` and can be overridden with environment variables.

## Using it

- **Next lead**: the highest-scoring unreviewed lead. A **Strongest evidence** panel shows the specific problem, the evidence and affected page, its confidence, why it may matter, the suggested service, a factual talking point, and how the score was reached. Below it are **Verify before contacting** (what the system couldn't confirm) and the other observations, grouped by signal strength; optional features are collapsed at the bottom. You can mark the lead *contacted*, *save* it, mark it *not a fit*, or *skip* it, and the next one loads. **Re-check site** re-runs the audit.
- **Details & sources** (on each lead): every field any source provided (names and other names, addresses, phones, email, website, social profiles, hours, description, state filing details), each tagged with its source; conflicting values are highlighted. It also lists the website state, every website candidate with its source, check result and match evidence, each source record with how it was matched and when it was last checked, and the raw source tags. Open any lead directly with `/#lead=<id>`.
- **Pipeline**: every analyzed lead, filterable by status, with CSV export.
- **New registrations**: recently filed Florida LLCs, corporations and fictitious names in the customer's areas and categories, with flags (*new entity*, *no confirmed website*, *needs qualification*, *operating evidence*) and any known business they match. **Promote to lead** runs website verification and qualification on one.
- **Customer selector** (header): appears when more than one profile exists. Leads, statuses, notes, AI briefs and exports are all per customer.
- Each finding carries a confidence of *verified*, *probable* or *unverified*, and a signal strength: *strong*, *moderate*, *minor*, or *optional feature*. Findings the system couldn't check (bot protection, robots.txt, untested forms) are listed separately and never scored.

## Website verification

A missing `website` field in one source is never treated as "no website". Each business gets one of six states:

| State | Meaning | Scored as |
|---|---|---|
| Website verified | Reachable, and its pages show the business's listed phone or name + address (or you entered it) | normal audit |
| Found, not yet verified | A source lists it, or the match is reasonable, but the pages don't confirm the identity, or bot protection (403) blocked reading them | audit findings count ×0.6 |
| Possible website, match uncertain | Only weak candidates (e.g. a similar domain with nothing identifying) | not scored; candidates shown for review |
| No website found after checks | Configured search sources ran and found no candidate | *No website found*: probable with one search source, verified with two |
| Temporarily unreachable | The best candidate timed out, refused connections, returned 5xx, or its domain doesn't resolve | audit findings count ×0.8; a load failure is verified only if the previous audit also failed |
| Unknown | No source listed a site and no search ran | not scored; "search the business yourself" |

Candidates come from the business's own source records (OSM `website`/`contact:website`/`url`/`contact:url`, your manual correction), then, only if those give no usable site, from Brave search and Google Places when configured. Each candidate is fetched politely, follows redirects (both the original and final URL are kept), and is classified: ok, blocked (401/403/429), robots, unreachable, dead domain, certificate error, parked, social profile, or directory listing. Identity is judged from the page: **high** = listed phone, or name + street address; **medium** = name + city/domain; **low** = name alone or nothing. All candidates are stored (`website_candidates`). When two sources point to different reachable sites, the lead is flagged *sources disagree*, and both stay visible. A manual correction wins.

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
   - **Reachability:** DNS, SSL, timeouts, HTTP errors, redirect loops, and an HTTPS check. A site is only reported down after failing twice, 5 s apart; one failure followed by success is reported as *intermittent* (probable).
   - **Broken links:** up to 8 internal links from the homepage, contact/quote/booking pages first, via a ranged GET (first 2 KB). 404/410 is *verified* broken; a 5xx seen twice is *probable*; 401/403/429 and timeouts are recorded as unchecked, not broken.
   - **Contact information:** whether the site offers any way to get in touch (phone, email, form, contact page), and whether the phone in the business listing appears on the site. A match *verifies* the contact details; a mismatch is reported as *probable* because we can't tell which number is current.
   - **Claims vs. functionality:** if the page explicitly promises something online ("book online", "online estimates") but has no form, booking tool or matching link, that's reported as *probable* (it may be a JavaScript widget we can't see).
   - **Placeholder pages:** parked, placeholder, or suspended pages, and listings that point to a social profile instead of a site.
   - **Mobile:** whether a viewport tag exists.
   - **Outdated tech:** frames, Flash, legacy tags, jQuery 1.x, WordPress older than 6.0, stale copyright years.
   - **SEO:** title, meta description, H1, `noindex`, image alt text, schema.org markup, sitemap.
   - **Lead capture:** contact or quote forms (including the contact page when the homepage has no form), `mailto:` and insecure form actions, click-to-call links. **Forms are never submitted.** A form we find is listed as "found, not tested", and nothing claims a form works or fails.
   - **Booking and chat:** scheduling tools (Calendly, Housecall Pro, Jobber, ServiceTitan, and others) and chat widgets.
3. **Qualify** (`app/qualify.py`, `app/scoring.py`). For each customer, a business is in scope if its category is targeted, its location is inside one of the customer's areas, and no exclusion matches. It is then scored with the evidence model below. Re-qualifying is pure computation (no network), so changing a profile is cheap.

### How a lead is scored (evidence model)

Every finding separates **what was observed** (the stored evidence and affected page) from **what it might mean**. The interpretation lives in `app/findings.py`: signal strength, why it may matter, what to verify by hand, and a talking point. A score is built like this:

1. **Relevance:** only findings mapped to one of the customer's offerings count.
2. **Value per finding** = signal-strength points × confidence × actionability.
   - Default points: strong 55, moderate 22, minor 8, optional 2.
   - Confidence: verified ×1.0, probable ×0.65, unverified ×0.3.
   - Actionability is below 1 where the opportunity itself is uncertain. A dead domain is ×0.6, because the business may simply have moved.
3. **No reward for volume:** the strongest finding counts in full, the 2nd at 35%, the 3rd at 15%, and the rest add nothing. All optional features together (no chat widget, no booking tool, missing schema…) add at most 4 points.
4. **Contact verification:**
   - listing phone also found on the business's own site: ×1.0;
   - contact details from the listing only: ×0.9;
   - contact details from the website only: ×0.85;
   - no contact details: ×0.5.

| Signal | Examples |
|---|---|
| Strong | site fails to load on retry, certificate error, no HTTPS, broken contact/quote page link, no way to contact the business, homepage set to `noindex`, parked/placeholder domain, no website (confirmed by search) |
| Moderate | intermittent failure, other broken links, phone mismatch, online-booking claim with no path, no mobile viewport, untappable phone number, `mailto:` form, "no website" from the map listing only (unverified) |
| Minor | no form when phone/email exist, stale copyright year, missing title |
| Optional | no chat, no booking tool, no schema, no sitemap, alt text, meta description, old jQuery |

Each lead stores an explanation (strongest evidence, main uncertainty, verification list). It appears in the UI, in the CSV export, and in the AI brief prompt. Talking points state what was observed and never claim the business is losing customers or revenue.

Weights are configurable per customer in the profile's `scoring` section (`tier_points`, `confidence_mult`, `decay`, `optional_cap`, `contact_mult`, `point_overrides`, `grade_thresholds`). v0.2 profiles still load: `high/medium/low` confidence keys are mapped automatically, and retired keys (`severity_points`, `contact_bonus`, `cap`) are ignored.

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

v1 adds `customers`, `customer_leads`, `lead_sources`, `discovery_scans` and dedup keys on `leads`. Existing workflow state (score, status, notes, AI brief) is copied to the default customer unchanged. The original columns on `leads` are left as they were, frozen, so the backup or the old code can still be used; the app no longer writes them.

v3 adds website candidates and verification state, field-level source provenance (`lead_sources.fields`, `source_url`, match confidence/explanation, last checked), Florida registrations and import tracking, and an API usage counter. The backfill fills only empty values. In testing on a copy of the production database it filled 2 cities parsed from full addresses and 1 phone from a `mobile` tag; nothing existing changed. The new checks then re-run automatically on the next start.

v2 adds a score explanation per customer lead, the audit version per business, and the scoring version per customer. On first start after upgrading, every customer is re-scored (statuses, notes and AI briefs are kept), and businesses audited by older checks are re-audited in the background so they gain the new link and contact checks.

**Stop the old server before upgrading.** A v0.1 process still running against a migrated database would keep writing statuses to the frozen columns, where v0.2 doesn't read them.

## Rate limits, limitations and costs

| Item | Limit / behavior |
|---|---|
| Overpass (public instance) | Documented guidance: well under 10,000 queries and 1 GB per day; HTTP 429 when busy. The app runs one query at a time, ≥10 s apart (`OVERPASS_MIN_INTERVAL`), retries 429/504 twice with backoff, and treats Overpass "runtime error" remarks (timeouts returned as HTTP 200) as failures. A full sweep for Atomic is 4 areas × 25 categories = 100 queries; measured 15–41 s each including spacing in testing. |
| Overpass pagination | Not supported by the API; every match in the area comes back at once. Areas are county-sized to keep responses small. |
| `newer:` incremental scans | Returns elements *edited* since a date. It doesn't report deletions, so closed businesses stay until marked *not a fit*. |
| County boxes | Approximate rectangles that overlap, so a business near a border can appear in two areas. Dedup handles the overlap, and scope checks use these boxes rather than legal county lines. |
| OSM coverage | Good for storefront trades; weak for home-based businesses (many pool, cleaning and pressure-washing operators aren't mapped). |
| Website audits | ≥2 s between requests to the same host, ≤4 sites at once, 3 MB page cap, robots.txt honored. Up to ~12 requests per site (homepage, retry, contact page, 8 links, sitemap); measured ~70 s for 8 sites at 4 in parallel. |
| What the audit can't see | Content rendered by JavaScript (findings that depend on it are downgraded to *unverified*), form behavior (forms are never submitted), pages beyond the homepage, contact page and 8 links, and anything behind bot protection. |
| Google Places | Off unless `GOOGLE_PLACES_API_KEY` is set. Only called when a business's own records list no usable website. Every call is counted per month and stopped at `GOOGLE_PLACES_MONTHLY_LIMIT` (900). |
| Brave Search | Up to 2 queries per business without a listed website (name + city, phone), 1 query/second. |

**Operating cost: $0.** OSM and Overpass are free. Brave Search's free tier is optional. Claude is optional and per-click (roughly a few cents per brief at current Opus pricing). SQLite and a local process need no hosting. The only real constraint is the shared public Overpass instance; heavy multi-customer use should move to a self-hosted Overpass or a paid mirror (set `OVERPASS_URL`).

## Data sources evaluated

| Source | Cost | Coverage | Licensing / terms | Verdict |
|---|---|---|---|---|
| **OpenStreetMap via Overpass** | Free | Moderate for trades: good on auto repair and HVAC, patchy on home-based businesses. Often includes phone, website, and address. | ODbL: commercial use OK with attribution (shown in the footer). Fair use of public instances. | **Used, primary.** |
| **Brave Search API** | Free tier ~2k queries/month, 1 q/s; paid after | Web-wide | Results may be used in apps per their API terms | **Used, optional**, to confirm or find official websites |
| Official business websites | Free | Per business | Public pages only; robots.txt honored; no logins, form submissions, or CAPTCHA bypass | **Used** for analysis |
| Google Places API (New) | Text Search with website/phone fields bills at the Enterprise SKU: 1,000 free calls/month, then about $35 per 1,000 (Google pricing page, Oct 2026) | Best coverage | Place IDs may be stored indefinitely; other content has caching restrictions | **Implemented, optional, off by default.** Website finder only; tested against representative responses, not the live API (no key available). |
| Yelp Fusion API | Limited free trial, now mostly paid | Good for consumer-facing businesses | Display and caching restrictions; no bulk storage | Not used |
| Florida Sunbiz (Division of Corporations) | Free daily/quarterly data files via public SFTP | Every FL entity and fictitious name | Offered free "for informational purposes", as is | **Implemented as a file import** (you download, the app imports). Tested on fixtures built from the official layout; not yet on a real downloaded file. |
| Scraping Google Maps, Yelp, or YellowPages | Free | High | Prohibited by their terms | **Excluded** |

To add a source, subclass `DiscoveryAdapter` (or `WebsiteFinder`) in `app/discovery/base.py`, raise `SourceError` on failure, set `supports_incremental` if it can fetch only changes, and register it in `app/pipeline.py`. To add a category or area, add it to `INDUSTRIES` or `AREAS` in `app/config.py`, then reference it from a profile.

## Compliance notes

- The crawler identifies itself with an honest User-Agent and honors robots.txt. When robots.txt disallows it, the site is skipped and flagged for manual review.
- Requests are spaced at least 2 seconds apart per host, at most 4 sites are checked concurrently, and pages are capped at 3 MB. Link checks read only the first 2 KB of each page.
- No form is ever submitted, and no outreach is sent automatically.
- Only business contact details that the business publishes (phone, website, `mailto:` addresses on its own site) are stored. No personal data is collected.
- Sunbiz files contain registered-agent and officer/owner names and addresses. Those blocks are never parsed or stored; only the entity name, type, status, dates, document number and principal address are kept.
- Google Places: only place IDs are stored (Google allows this indefinitely). Other Places content is used in memory for matching. A website URL from Places is stored only after our own fetch of that website shows the business's phone or address; otherwise only a Google Maps link built from the place ID is kept for review. Review Google's current Maps Platform terms before enabling it.
- Outreach by email must follow CAN-SPAM: identify yourself, include a physical address, and honor opt-outs. Calling or texting mobile numbers is subject to the TCPA and Florida's telemarketing laws. Check before cold-calling at scale.

## Florida registrations (Sunbiz)

**What's permitted** (checked on dos.fl.gov in October 2026): the Division of Corporations offers free daily and quarterly fixed-width data files "for informational purposes" on a public SFTP server. Host and login are listed on the [Data Downloads page](https://dos.fl.gov/sunbiz/other-services/data-downloads/). Daily files: `doc/cor/yyyymmddc.txt` (corporations, LLCs, LPs) and `doc/fic/yyyymmddf.txt` (fictitious names), one per work day, layouts on the [definitions pages](https://dos.sunbiz.org/data-definitions/cor.html). There's no documented API. The app does **not** scrape search.sunbiz.org and does not log in to the SFTP server itself.

**Workflow**
1. Download the daily files you want with any SFTP client (e.g. `sftp` in a terminal) into `data/sunbiz/`.
2. Click **Import downloaded Sunbiz files** on the *New registrations* tab, or run `.venv/bin/python -m app.cli sunbiz-import`.
3. Files already imported are skipped. Registrations filed more than `SUNBIZ_MAX_AGE_DAYS` (120) ago, inactive entities, non-profits/trusts/agent designations, and non-Florida principal addresses are skipped. Rows with a missing, malformed, out-of-range or ambiguous date, or a misaligned row length, are rejected and listed with reasons. Each file imports in one transaction, so a failure leaves existing records untouched and the file can be retried.

**How registrations are used**
- Registrations are kept apart from businesses: a filed LLC is a legal entity, not a confirmed operating business.
- The filed principal address is labeled "as filed" and is never treated as an operating location.
- Category and area are inferred from the name and filed city (`AREA_CITIES` in `app/config.py`, approximate).
- Matching to known businesses needs the same normalized name *and* address agreement:
  - same street address: high, recorded as a source on the business;
  - same ZIP or city: medium, shown for review;
  - name only: low, shown and never merged.
- A promoted registration gets a *recently registered* finding. It's a minor signal, and verified only with operating evidence: another independent listing or a live website.

**Not verified live:** the official date format isn't documented. The parser accepts MMDDYYYY or YYYYMMDD and rejects anything ambiguous. My one attempt to log in to the public SFTP from this environment was refused, so the parser is tested on fixtures built from the official layout, not on a downloaded file. Import one real daily file and check the *Recent imports* line (rejected count and reasons) before relying on it.

## Development

```bash
.venv/bin/python -m pytest -q        # audit checks, evidence scoring, migrations, dedup, discovery, customer isolation
.venv/bin/uvicorn app.main:app --reload
```

Project layout: `app/websites.py` (website verification), `app/details.py` (field provenance for the UI), `app/registrations.py` + `app/discovery/sunbiz.py` (Florida registrations), `app/discovery/places.py` (optional Google Places), `app/cli.py`, `app/main.py` (API and static UI), `app/pipeline.py` (scan planning and orchestration), `app/discovery/` (sources), `app/dedup.py`, `app/analysis/` (fetcher and checks), `app/qualify.py` + `app/scoring.py` (per-customer qualification), `app/profiles.py` + `profiles/` (customer config), `app/db.py` + `app/migrations.py` (SQLite), `app/ai.py`, `static/` (UI). `GET /api/scans` lists recent discovery scans with errors.
# leadgenerator
