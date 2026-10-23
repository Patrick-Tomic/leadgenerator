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
    """Proposes website candidates for a business. A finder never decides that a site
    belongs to the business; app/websites.py checks each candidate page for that."""

    name: str
    # False: only consulted when the business's own records list no usable website.
    always: bool = False
    # True: an empty result is evidence of "no website" (a real web search). False for
    # heuristics like guessing domains, where finding nothing proves nothing.
    conclusive: bool = True

    @abstractmethod
    async def find_candidates(self, profile: dict) -> dict:
        """profile: names, phones (10-digit), cities, addresses (see websites.business_profile).
        Return {"candidates": [{"url", "source", "source_record_id"?, "evidence": [...],
        "base_identity"?: "high"|"medium"|"low"}], "queries": [...], "checked": [result urls],
        "directories": [directory domains seen]}. Raise SourceError on failure."""
