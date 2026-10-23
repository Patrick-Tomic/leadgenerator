"""Free website finder: guess web addresses from the business name and keep only the ones
whose own pages identify the business.

No account, no API, no cost. For "Joe's Grill & Bar LLC" in Tampa it tries addresses like
joesgrillbar.com, joesgrill.com, joesgrilltampa.com and joes-grill.com. Only addresses that
exist in DNS are visited (politely, like any visitor). A guessed site is accepted only if
its page shows the business's phone number, or its name plus street address; anything
else is treated as someone else's site (see websites.verify_website, "strict").

Finding nothing proves nothing (the business may use an unrelated domain), so this
finder never supports a "no website found" conclusion.
"""
import asyncio
import re
import socket

from ..dedup import normalize_name
from .base import WebsiteFinder

MAX_GUESSES = 6
GENERIC = {"and", "the", "of", "inc", "llc", "co", "corp", "company", "services", "service", "group", "fl", "florida"}


def _slug(words: list[str]) -> str:
    return "".join(words)


def guesses(profile: dict) -> list[str]:
    """Candidate domains, most likely first."""
    out: list[str] = []
    city = re.sub(r"[^a-z]", "", (profile.get("city") or "").lower())
    for name in profile.get("names") or [profile["name"]]:
        words = [w for w in (normalize_name(name) or "").split() if w not in GENERIC]
        if not words:
            continue
        full = _slug(words)
        variants = [full, "-".join(words)]
        if len(words) > 2:
            variants.append(_slug(words[:2]))
        if city and not full.endswith(city):
            variants.append(full + city)
        for v in variants:
            if 4 <= len(v) <= 40:
                for tld in (".com", ".net"):
                    out.append(v + tld)
    seen, ordered = set(), []
    for d in out:
        if d not in seen:
            seen.add(d)
            ordered.append(d)
    return ordered


async def _resolves(domain: str) -> bool:
    loop = asyncio.get_running_loop()
    try:
        await asyncio.wait_for(loop.getaddrinfo(domain, 443, type=socket.SOCK_STREAM), timeout=5)
        return True
    except (OSError, asyncio.TimeoutError):
        return False


class DomainGuessFinder(WebsiteFinder):
    name = "domain_guess"
    conclusive = False

    async def find_candidates(self, profile: dict) -> dict:
        tried = guesses(profile)[: MAX_GUESSES * 2]
        found = []
        for domain in tried:
            if len(found) >= MAX_GUESSES:
                break
            if await _resolves(domain):
                found.append(domain)
        return {"candidates": [{"url": f"https://{d}/", "source": self.name, "strict": True,
                                "evidence": [f"address guessed from the business name ({d})"]} for d in found],
                "queries": tried, "checked": found, "directories": []}
