"""Test-wide safety: no test may download real OSM extracts or probe real websites."""
import pytest

from app import pipeline
from app.discovery.base import DiscoveryAdapter


class NoExtract(DiscoveryAdapter):
    name = "osm_extract"
    supports_incremental = False

    def __init__(self):
        self.calls = []

    async def discover(self, industry_key, area_key, since=None):
        self.calls.append(area_key)
        return []


@pytest.fixture(autouse=True)
def no_domain_guessing(monkeypatch):
    """Guessing domains does real DNS lookups and site visits; tests opt in explicitly."""
    from app import config
    monkeypatch.setattr(config, "DOMAIN_GUESS", False)


@pytest.fixture(autouse=True)
def no_real_extracts(monkeypatch):
    fake = NoExtract()
    monkeypatch.setattr(pipeline, "EXTRACT_ADAPTER", fake)
    return fake
