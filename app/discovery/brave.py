"""Optional website lookup with the Brave Search API (free tier: ~2,000 queries/month,
1 query/second). Only used for businesses that OSM lists without a website, so a
"no website" finding can be backed by an actual search rather than a missing tag."""
import asyncio
import re
from urllib.parse import urlparse

import httpx

from .. import config
from .base import SourceError, WebsiteFinder

# Results on these domains are listings about the business, not its own site.
DIRECTORY_DOMAINS = {
    "yelp.com", "facebook.com", "instagram.com", "linkedin.com", "bbb.org", "yellowpages.com",
    "mapquest.com", "angi.com", "angieslist.com", "homeadvisor.com", "nextdoor.com", "thumbtack.com",
    "houzz.com", "manta.com", "chamberofcommerce.com", "bizapedia.com", "opencorporates.com",
    "sunbiz.org", "buzzfile.com", "google.com", "apple.com", "tripadvisor.com", "x.com", "twitter.com",
    "youtube.com", "tiktok.com", "porch.com", "birdeye.com", "superpages.com", "dandb.com", "zoominfo.com",
    "carfax.com", "repairpal.com", "mechanicadvisor.com", "networx.com", "buildzoom.com", "alignable.com",
    "local.com", "cylex.us.com", "brownbook.net", "hotfrog.com", "n49.com", "loc8nearme.com", "wikipedia.org",
    "indeed.com", "glassdoor.com", "ziprecruiter.com", "myfloridalicense.com", "dnb.com", "bizjournals.com",
    "facebook.com", "nextdoor.com", "mapquest.com", "yellowpages.com", "fyple.com", "allbiz.com", "ccsearch.com",
}
STOPWORDS = {"the", "and", "of", "inc", "llc", "co", "company", "corp", "services", "service", "fl", "florida", "tampa"}


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if len(t) > 2 and t not in STOPWORDS}


def _registrable(host: str) -> str:
    parts = host.lower().removeprefix("www.").split(".")
    return ".".join(parts[-2:])


def _fmt_phone(p: str) -> str:
    return f"({p[:3]}) {p[3:6]}-{p[6:]}"


class BraveWebsiteFinder(WebsiteFinder):
    """Searches by name + city, and by phone number (a phone search is a strong identity
    signal). Returns non-directory domains as candidates; identity is decided later from
    the candidate page itself, never from the domain spelling alone."""

    name = "search:brave"
    _lock = asyncio.Lock()

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None, min_interval: float = 1.1) -> None:
        self.transport, self.min_interval = transport, min_interval

    async def _search(self, query: str) -> list[str]:
        from datetime import datetime, timezone
        from .. import db
        period = datetime.now(timezone.utc).strftime("%Y-%m")
        if db.api_calls(self.name, period) >= config.BRAVE_MONTHLY_LIMIT:
            raise SourceError(f"Brave monthly search limit reached ({config.BRAVE_MONTHLY_LIMIT}); not searching")
        db.count_api_call(self.name, period)  # count before sending: a sent request may be billed
        async with self._lock:
            try:
                async with httpx.AsyncClient(timeout=20, transport=self.transport) as client:
                    resp = await client.get(
                        "https://api.search.brave.com/res/v1/web/search",
                        params={"q": query, "count": 10, "country": "us"},
                        headers={"X-Subscription-Token": config.BRAVE_API_KEY, "Accept": "application/json"},
                    )
            except httpx.HTTPError as e:
                raise SourceError(f"Brave search failed: {type(e).__name__}") from e
            finally:
                await asyncio.sleep(self.min_interval)  # free tier: 1 request/second
        if resp.status_code != 200:
            raise SourceError(f"Brave search returned HTTP {resp.status_code}")
        return [r["url"] for r in resp.json().get("web", {}).get("results", []) if r.get("url")]

    async def find_candidates(self, profile: dict) -> dict:
        city = profile.get("city") or ""
        queries = [f'"{profile["name"]}" {city} FL'.replace("  ", " ")]
        if profile["phones"]:
            queries.append(f'"{_fmt_phone(profile["phones"][0])}"')
        candidates, checked, directories, seen = [], [], set(), set()
        for q in queries:
            urls = await self._search(q)
            checked += urls
            for rank, url in enumerate(urls, 1):
                host = _registrable(urlparse(url).netloc)
                if host in DIRECTORY_DOMAINS:
                    directories.add(host)
                    continue
                if host in seen or not host:
                    continue
                seen.add(host)
                candidates.append({"url": f"{urlparse(url).scheme}://{urlparse(url).netloc}/", "source": self.name,
                                   "evidence": [f'search result #{rank} for {q}']})
        return {"candidates": candidates[:5], "queries": queries, "checked": checked, "directories": sorted(directories)}


def get_finders(background: bool = False) -> list[WebsiteFinder]:
    """Configured website finders. Paid sources are off unless a key is set, and background
    checks only use them when WEBSITE_SEARCH_AUTO is on (on-demand checks always may)."""
    from .domain_guess import DomainGuessFinder
    from .places import GooglePlacesFinder
    # Free and keyless: guessing addresses from the name always runs (unless turned off).
    finders: list[WebsiteFinder] = [DomainGuessFinder()] if config.DOMAIN_GUESS else []
    if background and not config.WEBSITE_SEARCH_AUTO:
        return finders
    if config.TAVILY_API_KEY:
        finders.append(TavilyWebsiteFinder())
    if config.BRAVE_API_KEY:
        finders.append(BraveWebsiteFinder())
    if config.GOOGLE_PLACES_API_KEY:
        finders.append(GooglePlacesFinder())
    return finders


class TavilyWebsiteFinder(WebsiteFinder):
    """Free web search (Tavily): 1,000 basic searches a month, no card required. One search
    per business (name + city + phone), capped by TAVILY_MONTHLY_LIMIT. Like Brave, it only
    proposes candidates; identity is decided from each candidate page."""

    name = "search:tavily"

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.transport = transport

    async def find_candidates(self, profile: dict) -> dict:
        from datetime import datetime, timezone
        from .. import db
        period = datetime.now(timezone.utc).strftime("%Y-%m")
        if db.api_calls(self.name, period) >= config.TAVILY_MONTHLY_LIMIT:
            raise SourceError(f"Tavily monthly search limit reached ({config.TAVILY_MONTHLY_LIMIT}); not searching")
        parts = [f'"{profile["name"]}"', profile.get("city") or "", "FL" if not profile.get("city") else ""]
        if profile["phones"]:
            parts.append(_fmt_phone(profile["phones"][0]))
        query = " ".join(p for p in parts if p)
        db.count_api_call(self.name, period)
        try:
            async with httpx.AsyncClient(timeout=30, transport=self.transport) as client:
                resp = await client.post("https://api.tavily.com/search",
                                         headers={"Authorization": f"Bearer {config.TAVILY_API_KEY}"},
                                         json={"query": query, "search_depth": "basic", "max_results": 10})
        except httpx.HTTPError as e:
            raise SourceError(f"Tavily search failed: {type(e).__name__}") from e
        if resp.status_code in (432, 433):
            raise SourceError("Tavily plan limit reached for this month")
        if resp.status_code != 200:
            raise SourceError(f"Tavily search returned HTTP {resp.status_code}")
        urls = [r["url"] for r in resp.json().get("results", []) if r.get("url")]
        candidates, directories, seen = [], set(), set()
        for rank, url in enumerate(urls, 1):
            host = _registrable(urlparse(url).netloc)
            if host in DIRECTORY_DOMAINS:
                directories.add(host)
                continue
            if not host or host in seen:
                continue
            seen.add(host)
            candidates.append({"url": f"{urlparse(url).scheme}://{urlparse(url).netloc}/", "source": self.name,
                               "evidence": [f"search result #{rank} for {query}"]})
        return {"candidates": candidates[:5], "queries": [query], "checked": urls, "directories": sorted(directories)}
