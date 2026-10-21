"""Test-wide safety: no test may download or read real OpenStreetMap extracts."""
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
def no_real_extracts(monkeypatch):
    fake = NoExtract()
    monkeypatch.setattr(pipeline, "EXTRACT_ADAPTER", fake)
    return fake
