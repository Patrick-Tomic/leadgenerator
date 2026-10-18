"""Optional website lookup with the Brave Search API (free tier: ~2,000 queries/month,
1 query/second). Only used for businesses that OSM lists without a website, so a
"no website" finding can be backed by an actual search rather than a missing tag."""
import asyncio
import re
from urllib.parse import urlparse

import httpx

from .. import config
from .base import WebsiteFinder

# Results on these domains are listings about the business, not its own site.
DIRECTORY_DOMAINS = {
    "yelp.com", "facebook.com", "instagram.com", "linkedin.com", "bbb.org", "yellowpages.com",
    "mapquest.com", "angi.com", "angieslist.com", "homeadvisor.com", "nextdoor.com", "thumbtack.com",
    "houzz.com", "manta.com", "chamberofcommerce.com", "bizapedia.com", "opencorporates.com",
    "sunbiz.org", "buzzfile.com", "google.com", "apple.com", "tripadvisor.com", "x.com", "twitter.com",
    "youtube.com", "tiktok.com", "porch.com", "birdeye.com", "superpages.com", "dandb.com", "zoominfo.com",
    "carfax.com", "repairpal.com", "mechanicadvisor.com", "networx.com", "buildzoom.com", "alignable.com",
    "local.com", "cylex.us.com", "brownbook.net", "hotfrog.com", "n49.com", "loc8nearme.com", "wikipedia.org",
}
STOPWORDS = {"the", "and", "of", "inc", "llc", "co", "company", "corp", "services", "service", "fl", "florida", "tampa"}


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if len(t) > 2 and t not in STOPWORDS}


def _registrable(host: str) -> str:
    parts = host.lower().removeprefix("www.").split(".")
    return ".".join(parts[-2:])


def pick_official(name: str, urls: list[str]) -> str | None:
    """Pick a result whose domain plausibly belongs to the business."""
    name_tokens = _tokens(name)
    squashed = "".join(re.findall(r"[a-z0-9]+", name.lower()))
    for url in urls:
        host = urlparse(url).netloc
        if _registrable(host) in DIRECTORY_DOMAINS:
            continue
        domain_word = _registrable(host).split(".")[0]
        hits = sum(1 for t in name_tokens if t in domain_word)
        if hits >= min(2, len(name_tokens)) or (len(domain_word) > 5 and domain_word in squashed):
            return f"{urlparse(url).scheme}://{host}/"
    return None


class BraveWebsiteFinder(WebsiteFinder):
    name = "brave"
    _lock = asyncio.Lock()

    async def find_website(self, business: dict) -> dict:
        query = f'"{business["name"]}" {business.get("city") or ""} FL'.strip()
        async with self._lock:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.get(
                    "https://api.search.brave.com/res/v1/web/search",
                    params={"q": query, "count": 10, "country": "us"},
                    headers={"X-Subscription-Token": config.BRAVE_API_KEY, "Accept": "application/json"},
                )
            await asyncio.sleep(1.1)  # free tier: 1 request/second
        resp.raise_for_status()
        urls = [r["url"] for r in resp.json().get("web", {}).get("results", [])]
        return {"website": pick_official(business["name"], urls), "checked": urls, "query": query}


def get_finder() -> WebsiteFinder | None:
    return BraveWebsiteFinder() if config.BRAVE_API_KEY else None
