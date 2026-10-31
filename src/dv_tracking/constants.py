"""
Cross-stage constants and shared lookup data for the crawl and analysis stages.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from dv_tracking import PROJECT_ROOT

GEOLOCATIONS: tuple[str, ...] = ("uk", "us")
TRACKER_LISTS_DIR: Path = PROJECT_ROOT / "tracker_lists"
DEFAULT_CONSENTOMATIC_RULES_PATH: Path = TRACKER_LISTS_DIR / "consentomatic" / "Rules.json"
CMP_VENDOR_HOSTS_PATH: Path = TRACKER_LISTS_DIR / "cmp_vendors" / "cmp_vendors.csv"


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
    TrackerListSnapshot("CMP vendor hosts", "2026-07-25", None),
    TrackerListSnapshot("DuckDuckGo autoconsent", "2026-10-30", "v17.1.0"),
)


@dataclass(frozen=True)
class PublicSuffixSnapshot:
    """The pinned public-suffix snapshot the analysis stage resolves registered domains under."""

    path: Path
    list_version: str
    pinned_commit: str
    sha256: str
    include_private_domains: bool


# The vendored Mozilla Public Suffix List snapshot, checked by
# dv_tracking.analysis.party before it resolves anything. Analysis-stage only:
# the crawl container has no tldextract and never reads this.
PUBLIC_SUFFIX_SNAPSHOT: PublicSuffixSnapshot = PublicSuffixSnapshot(
    path=TRACKER_LISTS_DIR / "public_suffix_list" / "public_suffix_list.dat",
    list_version="2026-10-28_13-15-55_UTC",
    pinned_commit="9dfbe81931367b3f0af21035c36ae7eeb830dd88",
    sha256="1f96bae40adfc22ca2474d7c0099bbcfcb25aefd3e2547b5c2147c0dd2e3ebd9",
    include_private_domains=False,
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


# Shared with dv_tracking.crawl.consent_interaction, which drives the
# interactor on this same vocabulary; defined here rather than duplicated
# there, since Condition.action is the vocabulary's point of origin.
ConsentAction = Literal["none", "reject", "accept"]


@dataclass(frozen=True)
class Condition:
    """One consent-arm condition: its dwell schedule and which control, if any, it clicks."""

    name: str
    pre_dwell_s: float
    post_dwell_s: float
    action: ConsentAction


# Dwell is provisional until the calibration pass sets it from measured banner
# latency; 10s pre and 15s post is the working split pending that pass.
NO_INTERACTION: Condition = Condition(
    name="no_interaction", pre_dwell_s=10.0, post_dwell_s=15.0, action="none"
)
REJECT_ALL: Condition = Condition(
    name="reject_all", pre_dwell_s=10.0, post_dwell_s=15.0, action="reject"
)
ACCEPT_ALL: Condition = Condition(
    name="accept_all", pre_dwell_s=10.0, post_dwell_s=15.0, action="accept"
)
# All three arms, in their canonical (not yet counterbalanced) order.
CONDITIONS: tuple[Condition, ...] = (NO_INTERACTION, REJECT_ALL, ACCEPT_ALL)
