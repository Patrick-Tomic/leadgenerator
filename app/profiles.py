"""Customer profiles: what a customer sells, where, to whom, and how leads are scored.

Profiles are JSON files in PROFILES_DIR (one per customer, see profiles/atomic.json).
They are synced into the `customers` table at startup; the DB copy is what queries
use, so a deleted file never orphans a customer's leads.
"""
import hashlib
import json
from pathlib import Path

from . import config

DEFAULT_SCORING = {
    "severity_points": {"high": 14, "medium": 7, "low": 2},
    "confidence_mult": {"high": 1.0, "medium": 0.7, "low": 0.4},
    "point_overrides": {},
    "contact_bonus": {"phone": 5, "email": 3},
    "unreachable_multiplier": 0.5,
    "grade_thresholds": {"A": 60, "B": 40, "C": 20},
}


class ProfileError(ValueError):
    pass


def validate(raw: dict) -> dict:
    """Fill defaults and check references against the area/industry catalogs."""
    p = json.loads(json.dumps(raw))  # deep copy
    for key in ("slug", "name", "areas", "industries", "offerings"):
        if not p.get(key):
            raise ProfileError(f"profile is missing '{key}'")
    unknown_areas = [a for a in p["areas"] if a not in config.AREAS]
    if unknown_areas:
        raise ProfileError(f"{p['slug']}: unknown areas {unknown_areas}; choose from {sorted(config.AREAS)}")
    unknown_ind = [i for i in p["industries"] if i not in config.INDUSTRIES]
    if unknown_ind:
        raise ProfileError(f"{p['slug']}: unknown industries {unknown_ind}; choose from {sorted(config.INDUSTRIES)}")
    for key, offer in p["offerings"].items():
        for field in ("label", "codes", "pitch", "cap"):
            if field not in offer:
                raise ProfileError(f"{p['slug']}: offering '{key}' is missing '{field}'")
    p["scoring"] = {**DEFAULT_SCORING, **p.get("scoring", {})}
    p.setdefault("exclusions", {})
    for k in ("name_patterns", "domains", "website_statuses"):
        p["exclusions"].setdefault(k, [])
    p.setdefault("description", "")
    p.setdefault("region_label", config.REGION_LABEL)
    p.setdefault("active", True)
    p.setdefault("min_score", 1)
    p.setdefault("ready_pool_target", config.READY_POOL_TARGET)
    return p


def profile_hash(p: dict) -> str:
    return hashlib.sha256(json.dumps(p, sort_keys=True).encode()).hexdigest()[:16]


def load_file(path: Path) -> dict:
    return validate(json.loads(Path(path).read_text()))


def load_all() -> list[dict]:
    return [load_file(f) for f in sorted(config.PROFILES_DIR.glob("*.json"))]


def default_profile() -> dict:
    return load_file(config.PROFILES_DIR / f"{config.DEFAULT_CUSTOMER}.json")
