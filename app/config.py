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

# Optional: Brave Search API key, used to find websites for businesses that no source
# lists one for. Brave has no free plan since Feb 2026: about $5 of monthly credit
# (~1,000 searches) with a card on file, and usage beyond that is billed with no cap.
# So searches are capped per month here, and run only when you ask (Find website /
# Re-check site) unless WEBSITE_SEARCH_AUTO is turned on.
BRAVE_API_KEY = os.getenv("BRAVE_API_KEY", "")
# Free alternative: Tavily web search, 1,000 basic searches/month, no card required.
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")
TAVILY_MONTHLY_LIMIT = int(os.getenv("TAVILY_MONTHLY_LIMIT", "950"))
# Free, keyless: try web addresses made from the business name (e.g. joesgrill.com) and
# keep one only if its pages show the business's phone or name + street address.
DOMAIN_GUESS = os.getenv("DOMAIN_GUESS", "true").lower() not in ("0", "false", "no")
BRAVE_MONTHLY_LIMIT = int(os.getenv("BRAVE_MONTHLY_LIMIT", "900"))
# Let background website checks use Brave/Google Places on every business with no listed
# website. Off by default: with tens of thousands of such businesses it would use up the
# monthly caps within hours.
WEBSITE_SEARCH_AUTO = os.getenv("WEBSITE_SEARCH_AUTO", "").lower() in ("1", "true", "yes")

# Optional, paid: Google Places API (New) Text Search, used only to find website
# candidates for businesses whose own records list none. Off unless a key is set.
# Website/phone fields bill at the Text Search Enterprise SKU (1,000 free calls/month,
# then about $35 per 1,000 at the time of writing; check current pricing). The monthly
# cap below stops calls before the free allowance runs out; raise it deliberately.
GOOGLE_PLACES_API_KEY = os.getenv("GOOGLE_PLACES_API_KEY", "")
GOOGLE_PLACES_MONTHLY_LIMIT = int(os.getenv("GOOGLE_PLACES_MONTHLY_LIMIT", "900"))

# Florida Division of Corporations (Sunbiz) daily/quarterly data files, downloaded by
# you from the official public SFTP and placed here (see README "Sunbiz import").
SUNBIZ_IMPORT_DIR = Path(os.getenv("SUNBIZ_IMPORT_DIR", BASE_DIR / "data" / "sunbiz"))
# Florida DBPR construction licensee file (public record, refreshed weekly by DBPR).
DBPR_CONSTRUCTION_URL = "https://www2.myfloridalicense.com/sto/file_download/extracts//CONSTRUCTIONLICENSE_1.csv"
DBPR_DIR = Path(os.getenv("DBPR_DIR", BASE_DIR / "data" / "dbpr"))
# Only registrations filed within this many days are imported (0 = no limit).
SUNBIZ_MAX_AGE_DAYS = int(os.getenv("SUNBIZ_MAX_AGE_DAYS", "120"))

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
    "tampa_bay_legacy": {"label": "Tampa Bay (v0.1 single box)", "bbox": BBOX, "parent": "us-fl"},
    "hillsborough": {"label": "Hillsborough County", "bbox": (27.57, -82.65, 28.18, -82.05), "parent": "us-fl"},
    "pinellas": {"label": "Pinellas County", "bbox": (27.60, -82.87, 28.18, -82.54), "parent": "us-fl"},
    "pasco": {"label": "Pasco County", "bbox": (28.17, -82.82, 28.49, -82.05), "parent": "us-fl"},
    "manatee": {"label": "Manatee County", "bbox": (27.20, -82.80, 27.65, -81.56), "parent": "us-fl"},
    "hernando": {"label": "Hernando County", "bbox": (28.43, -82.70, 28.70, -82.05), "parent": "us-fl"},
    "sarasota": {"label": "Sarasota County", "bbox": (26.94, -82.65, 27.39, -82.05), "parent": "us-fl"},
    "polk": {"label": "Polk County", "bbox": (27.64, -82.11, 28.36, -81.13), "parent": "us-fl"},
    "citrus": {"label": "Citrus County", "bbox": (28.69, -82.80, 29.05, -82.17), "parent": "us-fl"},
}

# States, discovered from free Geofabrik OpenStreetMap extracts (one download per state)
# instead of the public Overpass server, which isn't meant for high-volume automated use.
# Bounding boxes are approximate and only used for records that lack a scan area.
STATE_EXTRACTS = {
    "us-fl": {"label": "Florida", "extract": "north-america/us/florida", "bbox": (24.3, -87.7, 31.1, -79.8)},
    "us-ga": {"label": "Georgia", "extract": "north-america/us/georgia", "bbox": (30.3, -85.7, 35.1, -80.7)},
    "us-sc": {"label": "South Carolina", "extract": "north-america/us/south-carolina", "bbox": (32.0, -83.4, 35.3, -78.5)},
    "us-nc": {"label": "North Carolina", "extract": "north-america/us/north-carolina", "bbox": (33.8, -84.4, 36.6, -75.4)},
}
AREAS.update(STATE_EXTRACTS)
GEOFABRIK_BASE = os.getenv("GEOFABRIK_BASE", "https://download.geofabrik.de")
OSM_EXTRACT_DIR = Path(os.getenv("OSM_EXTRACT_DIR", BASE_DIR / "data" / "osm"))
# Re-download and re-read a state's extract after this many days (Geofabrik updates daily).
EXTRACT_REFRESH_DAYS = int(os.getenv("EXTRACT_REFRESH_DAYS", "30"))
# A business name appearing at this many locations in one state is treated as a chain.
CHAIN_MIN_LOCATIONS = int(os.getenv("CHAIN_MIN_LOCATIONS", "5"))
# Businesses inside these areas are website-checked first (e.g. "hillsborough,pinellas").
# Empty by default: checks are spread evenly across every searched area.
PRIORITY_AREAS = [a for a in os.getenv("PRIORITY_AREAS", "").split(",") if a]


def area_ancestors(area: str | None) -> set[str]:
    """The area and every area containing it (county -> state)."""
    out = set()
    while area and area not in out:
        out.add(area)
        area = AREAS.get(area, {}).get("parent")
    return out


# Cities per area, used to place records that have an address but no coordinates
# (e.g. state business registrations). Approximate; cities spanning areas are listed once.
AREA_CITIES = {
    "hillsborough": ["Tampa", "Brandon", "Riverview", "Plant City", "Temple Terrace", "Valrico", "Seffner", "Lutz",
                     "Apollo Beach", "Ruskin", "Sun City Center", "Wimauma", "Gibsonton", "Thonotosassa", "Dover",
                     "Lithia", "Odessa", "Town N Country"],
    "pinellas": ["Saint Petersburg", "St Petersburg", "Clearwater", "Largo", "Pinellas Park", "Dunedin", "Palm Harbor",
                 "Tarpon Springs", "Seminole", "Oldsmar", "Safety Harbor", "Gulfport", "Treasure Island",
                 "St Pete Beach", "Madeira Beach", "Belleair", "Kenneth City", "Indian Rocks Beach"],
    "pasco": ["New Port Richey", "Port Richey", "Hudson", "Holiday", "Zephyrhills", "Dade City", "Wesley Chapel",
              "Land O Lakes", "Trinity", "San Antonio"],
    "manatee": ["Bradenton", "Palmetto", "Ellenton", "Parrish", "Lakewood Ranch", "Holmes Beach", "Anna Maria",
                "Bradenton Beach", "Myakka City"],
    "hernando": ["Brooksville", "Spring Hill", "Weeki Wachee", "Ridge Manor"],
    "sarasota": ["Sarasota", "Venice", "North Port", "Osprey", "Nokomis", "Englewood", "Longboat Key"],
    "polk": ["Lakeland", "Winter Haven", "Bartow", "Haines City", "Auburndale", "Lake Wales", "Davenport", "Mulberry"],
    "citrus": ["Inverness", "Crystal River", "Homosassa", "Beverly Hills", "Lecanto"],
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
    # Beyond the trades: any kind of local business.
    "restaurant": {"label": "Restaurants", "selectors": ['["amenity"~"^(restaurant|fast_food|food_court)$"]']},
    "cafe_bakery": {"label": "Cafés & bakeries", "selectors": ['["amenity"~"^(cafe|ice_cream)$"]', '["shop"~"^(bakery|coffee|pastry|confectionery|deli)$"]']},
    "bar_brewery": {"label": "Bars, breweries & nightlife", "selectors": ['["amenity"~"^(bar|pub|biergarten|nightclub)$"]', '["craft"~"^(brewery|winery|distillery)$"]']},
    "insurance": {"label": "Insurance agencies", "selectors": ['["office"="insurance"]']},
    "bank_finance": {"label": "Banks, credit unions & finance", "selectors": ['["amenity"="bank"]', '["office"~"^(financial|financial_advisor)$"]']},
    "accounting_tax": {"label": "Accounting & tax", "selectors": ['["office"~"^(accountant|tax_advisor)$"]']},
    "legal": {"label": "Law offices", "selectors": ['["office"~"^(lawyer|notary)$"]']},
    "real_estate": {"label": "Real estate", "selectors": ['["office"~"^(estate_agent|property_management)$"]']},
    "dental": {"label": "Dentists", "selectors": ['["amenity"="dentist"]', '["healthcare"="dentist"]']},
    "medical": {"label": "Medical & therapy practices", "selectors": ['["amenity"~"^(doctors|clinic)$"]', '["healthcare"~"^(doctor|physiotherapist|chiropractor|psychotherapist|optometrist|alternative|clinic)$"]', '["shop"="optician"]']},
    "veterinary_pet": {"label": "Veterinary & pet services", "selectors": ['["amenity"~"^(veterinary|animal_boarding)$"]', '["shop"~"^(pet|pet_grooming)$"]']},
    "beauty_salon": {"label": "Salons, spas & beauty", "selectors": ['["shop"~"^(hairdresser|beauty|tattoo|massage|cosmetics)$"]']},
    "fitness": {"label": "Gyms & fitness studios", "selectors": ['["leisure"~"^(fitness_centre|sports_centre)$"]', '["amenity"="dojo"]']},
    "lodging": {"label": "Hotels & lodging", "selectors": ['["tourism"~"^(hotel|motel|guest_house|hostel)$"]']},
    "childcare_education": {"label": "Childcare & schools", "selectors": ['["amenity"~"^(childcare|kindergarten|driving_school|music_school|language_school|prep_school)$"]']},
    "entertainment": {"label": "Venues & entertainment", "selectors": ['["amenity"~"^(events_venue|theatre|cinema)$"]', '["leisure"~"^(bowling_alley|escape_game|amusement_arcade)$"]']},
    "auto_sales": {"label": "Auto, boat & motorcycle sales", "selectors": ['["shop"~"^(car|motorcycle|boat)$"]']},
    "car_wash": {"label": "Car washes", "selectors": ['["amenity"="car_wash"]']},
    "funeral": {"label": "Funeral homes", "selectors": ['["shop"="funeral_directors"]']},
    "storage": {"label": "Self storage", "selectors": ['["shop"="storage_rental"]']},
    # Catch-alls: checked last, so a business gets the most specific category that fits.
    "retail": {"label": "Other retail shops", "selectors": ['["shop"]']},
    "professional_office": {"label": "Other offices & professional services", "selectors": ['["office"]']},
    "trades_other": {"label": "Other trades & crafts", "selectors": ['["craft"]']},
}
