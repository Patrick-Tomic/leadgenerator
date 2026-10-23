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
    # Base points by signal strength (see app/findings.py).
    "tier_points": {"strong": 55, "moderate": 22, "weak": 8, "optional": 2},
    "confidence_mult": {"verified": 1.0, "probable": 0.65, "unverified": 0.3},
    # Weight of the 1st, 2nd, 3rd strongest finding; later ones add nothing.
    "decay": [1.0, 0.35, 0.15],
    # All optional-feature findings together add at most this many points.
    "optional_cap": 4,
    # Multiplier by how well the contact details are verified (see scoring.contact_status).
    "contact_mult": {"verified": 1.0, "listed": 0.9, "site_only": 0.85, "none": 0.5},
    # Per-finding base points that replace the tier points (before confidence/actionability).
    "point_overrides": {},
    "grade_thresholds": {"A": 60, "B": 40, "C": 20},
    # Multiplier by how well known the business is (app/visibility.py estimate).
    "visibility_mult": {"low": 1.0, "medium": 0.85, "high": 0.5, "unknown": 1.0},
}
# v0.2 profiles used high/medium/low confidence keys; they map onto the new names.
_LEGACY_CONFIDENCE = {"high": "verified", "medium": "probable", "low": "unverified"}
_LEGACY_SCORING_KEYS = ("severity_points", "contact_bonus", "unreachable_multiplier")


def _scoring(raw: dict) -> dict:
    out = {**DEFAULT_SCORING, **{k: v for k, v in (raw or {}).items() if k not in _LEGACY_SCORING_KEYS}}
    conf = out["confidence_mult"]
    if any(k in conf for k in _LEGACY_CONFIDENCE):
        out["confidence_mult"] = {**DEFAULT_SCORING["confidence_mult"],
                                  **{_LEGACY_CONFIDENCE.get(k, k): v for k, v in conf.items()}}
    for key in ("tier_points", "confidence_mult", "contact_mult", "grade_thresholds", "visibility_mult"):
        out[key] = {**DEFAULT_SCORING[key], **out[key]}
    return out


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
        for field in ("label", "codes", "pitch"):  # "cap" from v0.2 profiles is accepted and ignored
            if field not in offer:
                raise ProfileError(f"{p['slug']}: offering '{key}' is missing '{field}'")
    p["scoring"] = _scoring(p.get("scoring", {}))
    p.setdefault("exclusions", {})
    for k in ("name_patterns", "domains", "website_statuses"):
        p["exclusions"].setdefault(k, [])
    p.setdefault("description", "")
    p.setdefault("region_label", config.REGION_LABEL)
    p.setdefault("active", True)
    p.setdefault("min_score", 1)
    # With a Google Places key: skip leads with more Google reviews than this (None = don't check).
    p.setdefault("max_google_reviews", 50)
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
