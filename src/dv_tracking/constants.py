"""
Cross-stage constants and shared lookup data for the crawl and analysis stages.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from dv_tracking import PROJECT_ROOT

GEOLOCATIONS: tuple[str, ...] = ("uk", "us")
TRACKER_LISTS_DIR: Path = PROJECT_ROOT / "tracker_lists"
DEFAULT_CONSENTOMATIC_RULES_PATH: Path = TRACKER_LISTS_DIR / "consentomatic" / "Rules.json"


@dataclass(frozen=True)
class TrackerListSnapshot:
    """A pinned tracker-list snapshot: its name, vendored date, and pinned commit."""

    name: str
    snapshot_date: str
    pinned_commit: str | None


# The vendored tracker-list snapshots. Keep in sync with tracker_lists/PROVENANCE.md.
TRACKER_LIST_SNAPSHOTS: tuple[TrackerListSnapshot, ...] = (
    TrackerListSnapshot("DuckDuckGo Tracker Radar", "2026-06-20", "62a93b0"),
    TrackerListSnapshot("EasyPrivacy", "2026-06-20", "d4a1b7e"),
    TrackerListSnapshot("Open Cookie Database", "2026-06-13", None),
    TrackerListSnapshot("Consent-O-Matic", "2026-05-16", "8ca8500"),
)

# UK's ISO 3166-1 alpha-2 code is GB; the project label for that cell is "uk".
COUNTRY_CODE_TO_GEOLOCATION: dict[str, str] = {"GB": "uk", "US": "us"}
# The ISO 3166-1 alpha-2 country each project geolocation must resolve to.
EXPECTED_COUNTRY: dict[str, str] = {"uk": "GB", "us": "US"}

# Residential vs datacenter: best-effort, non-exhaustive keyword match on the ipinfo `org` field.
DATACENTER_KEYWORDS: tuple[str, ...] = (
    "amazon", "aws",
    "google",
    "microsoft", "azure",
    "digitalocean",
    "linode", "akamai",
    "vultr", "choopa",
    "ovh",
    "hetzner",
    "leaseweb",
    "m247",
    "datacamp",
    "cogent",
    "gtt communications",
    "psychz",
    "quadranet",
    "colocrossing",
)
