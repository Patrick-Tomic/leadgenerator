"""Polite HTTP fetching: honest User-Agent, robots.txt, per-host spacing, size cap.

We only read public pages a normal visitor would see. No logins, no form
submissions, no CAPTCHA handling: if a site blocks us, we record that and move on.
"""
import asyncio
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

from .. import config


@dataclass
class FetchResult:
    url: str
    ok: bool
    status: int | None = None
    final_url: str | None = None
    html: str = ""
    elapsed: float = 0.0
    error: str | None = None  # dns | timeout | ssl | connect | blocked_by_robots | http_error | too_large | other
    error_detail: str | None = None
    headers: dict = field(default_factory=dict)
    redirects: list[str] = field(default_factory=list)


class PoliteFetcher:
    def __init__(self) -> None:
        self._robots: dict[str, RobotFileParser | None] = {}
        self._last_hit: dict[str, float] = {}
        self._host_locks: dict[str, asyncio.Lock] = {}
        self.client = httpx.AsyncClient(
            headers={"User-Agent": config.USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
            timeout=config.FETCH_TIMEOUT,
            follow_redirects=True,
            max_redirects=8,
        )

    async def aclose(self) -> None:
        await self.client.aclose()

    async def _wait_turn(self, host: str) -> None:
        lock = self._host_locks.setdefault(host, asyncio.Lock())
        async with lock:
            wait = self._last_hit.get(host, 0) + config.PER_HOST_DELAY - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_hit[host] = time.monotonic()

    async def robots(self, base: str) -> RobotFileParser | None:
        """Return the parsed robots.txt for a scheme://host, or None if there is none."""
        if base in self._robots:
            return self._robots[base]
        parser: RobotFileParser | None = None
        try:
            await self._wait_turn(urlparse(base).netloc)
            r = await self.client.get(base + "/robots.txt")
            if r.status_code == 200 and "text/html" not in r.headers.get("content-type", ""):
                parser = RobotFileParser()
                parser.parse(r.text.splitlines())
            elif r.status_code in (401, 403):
                # Per RFC 9309, an access-restricted robots.txt means "disallow all".
                parser = RobotFileParser()
                parser.parse(["User-agent: *", "Disallow: /"])
        except httpx.HTTPError:
            parser = None
        self._robots[base] = parser
        return parser

    async def allowed(self, url: str) -> bool:
        p = urlparse(url)
        rp = await self.robots(f"{p.scheme}://{p.netloc}")
        return True if rp is None else rp.can_fetch(config.USER_AGENT, url)

    async def head_ok(self, url: str) -> int | None:
        """Lightweight existence check (sitemap.xml etc.). Returns status or None."""
        try:
            await self._wait_turn(urlparse(url).netloc)
            r = await self.client.get(url, headers={"Range": "bytes=0-2048"})
            return r.status_code
        except httpx.HTTPError:
            return None

    async def get(self, url: str, check_robots: bool = True) -> FetchResult:
        if check_robots and not await self.allowed(url):
            return FetchResult(url=url, ok=False, error="blocked_by_robots", error_detail="robots.txt disallows this path")
        await self._wait_turn(urlparse(url).netloc)
        start = time.monotonic()
        try:
            async with self.client.stream("GET", url) as r:
                chunks, size = [], 0
                async for chunk in r.aiter_bytes():
                    size += len(chunk)
                    if size > config.MAX_HTML_BYTES:
                        break
                    chunks.append(chunk)
                body = b"".join(chunks)
                elapsed = time.monotonic() - start
                text = body.decode(r.encoding or "utf-8", errors="replace")
                return FetchResult(
                    url=url,
                    ok=r.status_code < 400,
                    status=r.status_code,
                    final_url=str(r.url),
                    html=text if "html" in r.headers.get("content-type", "html") else "",
                    elapsed=elapsed,
                    error=None if r.status_code < 400 else "http_error",
                    error_detail=None if r.status_code < 400 else f"HTTP {r.status_code}",
                    headers=dict(r.headers),
                    redirects=[str(h.url) for h in r.history],
                )
        except httpx.ConnectError as e:
            msg = str(e)
            low = msg.lower()
            if "certificate" in low or "ssl" in low:
                kind = "ssl"
            elif "name or service" in low or "nodename" in low or "getaddrinfo" in low or "name resolution" in low:
                kind = "dns"
            else:
                kind = "connect"
            return FetchResult(url=url, ok=False, error=kind, error_detail=msg[:300], elapsed=time.monotonic() - start)
        except httpx.TimeoutException:
            return FetchResult(url=url, ok=False, error="timeout", error_detail=f"No response within {config.FETCH_TIMEOUT:.0f}s", elapsed=time.monotonic() - start)
        except httpx.TooManyRedirects:
            return FetchResult(url=url, ok=False, error="redirect_loop", error_detail="Too many redirects")
        except httpx.HTTPError as e:
            return FetchResult(url=url, ok=False, error="other", error_detail=str(e)[:300] or type(e).__name__)
