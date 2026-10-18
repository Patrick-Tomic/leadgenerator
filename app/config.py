"""Central settings. Everything here can be overridden with environment variables."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.getenv("LEAD_ENGINE_DB", BASE_DIR / "data" / "leads.db"))

# Tampa Bay area: Hillsborough, Pinellas, Pasco and northern Manatee counties.
# (south, west, north, east) — the order Overpass expects.
BBOX = tuple(
    float(x)
    for x in os.getenv("LEAD_ENGINE_BBOX", "27.40,-82.85,28.45,-82.05").split(",")
)
REGION_LABEL = os.getenv("LEAD_ENGINE_REGION", "Tampa Bay, FL")

# Identify ourselves honestly to every site we touch. Set LEAD_ENGINE_CONTACT to a
# URL or email so site owners can reach you about the crawler.
CONTACT = os.getenv("LEAD_ENGINE_CONTACT", "")
USER_AGENT = "AtomicLeadEngine/0.1 (+business website audit" + (f"; {CONTACT}" if CONTACT else "") + ")"

OVERPASS_URL = os.getenv("OVERPASS_URL", "https://overpass-api.de/api/interpreter")
# A completed area x industry scan is fresh for this many days; after that it is
# re-run incrementally (only OSM elements edited since the last successful scan).
OVERPASS_CACHE_DAYS = int(os.getenv("OVERPASS_CACHE_DAYS", "7"))
# Public Overpass instances ask for modest use: one query at a time, spaced out.
OVERPASS_MIN_INTERVAL = float(os.getenv("OVERPASS_MIN_INTERVAL", "10"))
# A failed scan is retried after this many minutes, doubling per consecutive failure (max 24h).
SCAN_RETRY_MINUTES = float(os.getenv("SCAN_RETRY_MINUTES", "30"))
# Stop a refill after this many consecutive failed scans (source is probably down).
MAX_CONSECUTIVE_SCAN_FAILURES = int(os.getenv("MAX_CONSECUTIVE_SCAN_FAILURES", "3"))

# Optional: Brave Search API key (free tier) used to find websites for businesses
# that OSM lists without one. Without it, "no website" findings stay low-confidence.
BRAVE_API_KEY = os.getenv("BRAVE_API_KEY", "")

# Optional: Claude writes a lead brief + outreach draft on demand.
ANTHROPIC_MODEL = os.getenv("LEAD_ENGINE_MODEL", "claude-opus-5-5")

# Politeness limits for website analysis.
FETCH_TIMEOUT = float(os.getenv("FETCH_TIMEOUT", "15"))
PER_HOST_DELAY = float(os.getenv("PER_HOST_DELAY", "2.0"))
MAX_CONCURRENT_SITES = int(os.getenv("MAX_CONCURRENT_SITES", "4"))
MAX_HTML_BYTES = 3_000_000

# Customer profiles (offerings, areas, industries, exclusions, scoring) live here as JSON.
PROFILES_DIR = Path(os.getenv("LEAD_ENGINE_PROFILES", BASE_DIR / "profiles"))
DEFAULT_CUSTOMER = os.getenv("LEAD_ENGINE_DEFAULT_CUSTOMER", "atomic")
BACKUP_DIR = DB_PATH.parent / "backups"

# Keep at least this many analyzed, unreviewed leads ready; refill in background.
READY_POOL_TARGET = int(os.getenv("READY_POOL_TARGET", "15"))
ANALYZE_BATCH = int(os.getenv("ANALYZE_BATCH", "25"))

# Named search areas (south, west, north, east). Customer profiles pick from these.
# Approximate county bounds; overlaps are fine because discovery de-duplicates.
AREAS = {
    "tampa_bay_legacy": {"label": "Tampa Bay (v0.1 single box)", "bbox": BBOX},
    "hillsborough": {"label": "Hillsborough County", "bbox": (27.57, -82.65, 28.18, -82.05)},
    "pinellas": {"label": "Pinellas County", "bbox": (27.60, -82.87, 28.18, -82.54)},
    "pasco": {"label": "Pasco County", "bbox": (28.17, -82.82, 28.49, -82.05)},
    "manatee": {"label": "Manatee County", "bbox": (27.20, -82.80, 27.65, -81.56)},
    "hernando": {"label": "Hernando County", "bbox": (28.43, -82.70, 28.70, -82.05)},
    "sarasota": {"label": "Sarasota County", "bbox": (26.94, -82.65, 27.39, -82.05)},
    "polk": {"label": "Polk County", "bbox": (27.64, -82.11, 28.36, -81.13)},
    "citrus": {"label": "Citrus County", "bbox": (28.69, -82.80, 29.05, -82.17)},
}

# Business categories and how to find them in OpenStreetMap. Each selector is an
# Overpass filter fragment applied to nodes, ways and relations inside an area.
# This catalog is generic; which categories matter is set per customer profile.
INDUSTRIES = {
    "hvac": {"label": "HVAC / AC repair", "selectors": ['["craft"="hvac"]', '["name"~"air condition|heating (&|and) (air|cooling)|a/c (service|repair)",i]["shop"!~"."]["amenity"!~"."]']},
    "plumbing": {"label": "Plumbing", "selectors": ['["craft"="plumber"]', '["name"~"plumbing",i]["amenity"!~"."]']},
    "electrical": {"label": "Electrical contractor", "selectors": ['["craft"="electrician"]']},
    "roofing": {"label": "Roofing", "selectors": ['["craft"="roofer"]', '["name"~"roofing",i]["amenity"!~"."]']},
    "general_contractor": {"label": "General contractor / remodeling", "selectors": ['["craft"~"^(builder|carpenter|painter|tiler|floorer|plasterer|glaziery|insulation)$"]', '["office"="construction_company"]']},
    "pool": {"label": "Pool service", "selectors": ['["shop"="swimming_pool"]', '["name"~"pool (service|care|clean|repair|maint)|pools( |$)",i]["leisure"!~"."]["amenity"!~"."]']},
    "pressure_washing": {"label": "Pressure washing", "selectors": ['["name"~"pressure wash|power wash|soft wash",i]']},
    "landscaping": {"label": "Landscaping / lawn care", "selectors": ['["craft"="gardener"]', '["name"~"landscap|lawn (care|service)|tree (service|care)",i]["amenity"!~"."]']},
    "auto_repair": {"label": "Auto repair", "selectors": ['["shop"="car_repair"]', '["shop"="tyres"]']},
    "cleaning": {"label": "Cleaning services", "selectors": ['["craft"="cleaning"]', '["name"~"cleaning (service|co)|maid|janitorial|carpet clean",i]["amenity"!~"."]']},
    "pest_control": {"label": "Pest control", "selectors": ['["name"~"pest control|termite|exterminat",i]']},
    "locksmith": {"label": "Locksmith", "selectors": ['["shop"="locksmith"]', '["craft"="locksmith"]']},
    "handyman": {"label": "Handyman", "selectors": ['["craft"="handyman"]', '["name"~"handyman",i]']},
    "painting": {"label": "Painting", "selectors": ['["name"~"painting (co|contractor|service|inc|llc)|painters",i]["shop"!~"."]["amenity"!~"."]']},
    "flooring": {"label": "Flooring", "selectors": ['["shop"="flooring"]', '["name"~"flooring",i]["amenity"!~"."]']},
    "fencing": {"label": "Fencing", "selectors": ['["name"~"fence|fencing",i]["barrier"!~"."]["amenity"!~"."]']},
    "screen_enclosures": {"label": "Screen enclosures / aluminum", "selectors": ['["name"~"screen (enclosure|room|repair)|pool cage|aluminum (inc|llc|co|construction|structures)",i]']},
    "pavers_concrete": {"label": "Pavers / concrete", "selectors": ['["name"~"pavers|paving|concrete (inc|llc|co|contractor|service)",i]["highway"!~"."]["amenity"!~"."]']},
    "irrigation": {"label": "Irrigation / sprinklers", "selectors": ['["name"~"irrigation|sprinkler",i]["amenity"!~"."]']},
    "garage_doors": {"label": "Garage doors", "selectors": ['["name"~"garage door",i]']},
    "appliance_repair": {"label": "Appliance repair", "selectors": ['["craft"="electronics_repair"]', '["name"~"appliance repair",i]']},
    "moving": {"label": "Moving companies", "selectors": ['["office"="moving_company"]', '["name"~"moving (co|company|service)|movers",i]["amenity"!~"."]']},
    "towing": {"label": "Towing", "selectors": ['["name"~"towing|wrecker",i]']},
    "auto_detailing": {"label": "Auto detailing", "selectors": ['["name"~"detailing",i]', '["shop"="car_repair"]["service:vehicle:detailing"="yes"]']},
    "solar": {"label": "Solar installers", "selectors": ['["craft"="photovoltaic"]', '["name"~"solar (energy|power|solutions|install)",i]["power"!~"."]']},
}
