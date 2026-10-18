"""Adapter interfaces. Add a new data source by subclassing one of these and
registering it in app/pipeline.py."""
from abc import ABC, abstractmethod


class SourceError(RuntimeError):
    """A discovery source failed (HTTP error, timeout, malformed response). Recorded on
    the scan and retried later; never stops other scans."""


class DiscoveryAdapter(ABC):
    """Finds businesses of one category inside one named area."""

    name: str
    # True if the source can return only records changed since a timestamp, so
    # repeat scans don't re-download everything.
    supports_incremental: bool = False

    @abstractmethod
    async def discover(self, industry_key: str, area_key: str, since: str | None = None) -> list[dict]:
        """Return business dicts with keys: source, source_id, name, industry, address,
        city, lat, lon, phone, email, website, raw. Raise SourceError on failure."""


class WebsiteFinder(ABC):
    """Looks up the official website of a business that was discovered without one."""

    name: str

    @abstractmethod
    async def find_website(self, business: dict) -> dict:
        """Return {"website": url | None, "checked": [result urls], "query": str}."""
