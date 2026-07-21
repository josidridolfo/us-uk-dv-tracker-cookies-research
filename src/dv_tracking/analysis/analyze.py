"""
The dv-analyze entrypoint: classify the raw crawl and print the descriptive summary.

Loads the raw trials, builds the classifiers from the pinned tracker-list
snapshots, derives one SitePrivacyFindings per trial, collapses to one row per
site at the longest dwell, and prints the aggregate-only jurisdiction summary.
Art 9 transmission rates use the count of Art-9-implicated sites as the
denominator, not all sites.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import polars as pl

from dv_tracking import PROJECT_ROOT
from dv_tracking.analysis.models import SitePrivacyFindings
from dv_tracking.analysis.statistics import DistributionSummary
from dv_tracking.classify.banner import BannerClassifier
from dv_tracking.classify.cookies import CookieDatabase
from dv_tracking.classify.easyprivacy import EasyPrivacyMatcher
from dv_tracking.classify.models import AnalyzedTrial
from dv_tracking.classify.platform import HostingPlatformClassifier
from dv_tracking.classify.tracker_radar import TrackerRadar
from dv_tracking.constants import DEFAULT_CONSENTOMATIC_RULES_PATH, TRACKER_LISTS_DIR
from dv_tracking.crawl.models import ConsentomaticBundle
from dv_tracking.persistence import RawStore
from dv_tracking.sites import DEFAULT_SITES_PATH, DVSupportSite

DEFAULT_RAW_ROOT: Path = PROJECT_ROOT / "data" / "raw"
DEFAULT_COOKIE_DATABASE_PATH: Path = (
    TRACKER_LISTS_DIR / "open_cookie_database" / "open-cookie-database.csv"
)
DEFAULT_EASYPRIVACY_PATH: Path = TRACKER_LISTS_DIR / "easyprivacy" / "easyprivacy.txt"
DEFAULT_TRACKER_RADAR_INDEX_PATH: Path = TRACKER_LISTS_DIR / "ddg_tracker_radar" / "index.json"


def parse_arguments(argv: list[str] | None) -> argparse.Namespace:
    """
    Parse the dv-analyze command-line arguments.

    Args:
        argv: The argument vector; defaults to sys.argv when None.
    Returns:
        arguments: The parsed argument namespace.
    """
    parser = argparse.ArgumentParser(description="Analyze the raw crawl into per-site findings.")
    parser.add_argument("--raw-root", type=Path, default=DEFAULT_RAW_ROOT)
    parser.add_argument("--sites", type=Path, default=DEFAULT_SITES_PATH)
    parser.add_argument("--cookie-database", type=Path, default=DEFAULT_COOKIE_DATABASE_PATH)
    parser.add_argument("--easyprivacy", type=Path, default=DEFAULT_EASYPRIVACY_PATH)
    parser.add_argument("--tracker-radar-index", type=Path, default=DEFAULT_TRACKER_RADAR_INDEX_PATH)
    parser.add_argument("--consentomatic-rules", type=Path, default=DEFAULT_CONSENTOMATIC_RULES_PATH)
    return parser.parse_args(argv)


def art_9_denominator(frame: pl.DataFrame) -> int:
    """
    The denominator for Art 9 transmission rates: the count of implicated sites.

    Args:
        frame: A site-level findings frame.
    Returns:
        denominator: The number of Art-9-implicated sites, or the row count if the column is absent.
    """
    if "art_9_implicated" not in frame.columns:
        return frame.height
    return int(frame.filter(pl.col("art_9_implicated")).height)


def print_summary(frame: pl.DataFrame) -> None:
    """
    Print the aggregate-only jurisdiction summary from a site-level frame.

    Args:
        frame: The successful site-level findings frame.
    Returns:
        None
    """
    print("=== descriptive analysis: DV support sites (aggregate-only) ===")
    print(f"successful sites: {frame.height}")
    geolocations = sorted({str(value) for value in frame.get_column("geolocation").to_list() if value})
    for geolocation in geolocations:
        cell = frame.filter(pl.col("geolocation") == geolocation)
        trackers = DistributionSummary.from_values(cell.get_column("tracker_request_count").to_list())
        implicated = art_9_denominator(cell)
        transmission = int(cell.filter(pl.col("art_9_transmission_present")).height)
        print(f"[{geolocation}] sites={cell.height} tracker_requests_median={trackers.median}")
        print(
            f"[{geolocation}] art_9_transmission_present: {transmission}/{implicated} implicated sites"
        )


def main(argv: list[str] | None = None) -> int:
    """
    Analyze the raw crawl and print the descriptive summary.

    Assumes a single crawl per geolocation under raw_root; multiple crawls for one
    geolocation pool across runs and corrupt the 15s-dwell collapse.

    Args:
        argv: The argument vector; defaults to sys.argv when None.
    Returns:
        exit_code: 0 on success, 1 when no successful sites remain, 2 when the raw root is absent.
    """
    arguments = parse_arguments(argv)
    raw_root = arguments.raw_root
    if not raw_root.is_dir():
        print(f"ERROR: raw-crawls root not found at {raw_root}")
        return 2

    sites_by_id = {site.site_id: site for site in DVSupportSite.load_all(arguments.sites)}
    cookie_db = CookieDatabase.from_csv(arguments.cookie_database)
    ep_matcher = EasyPrivacyMatcher.from_snapshot(arguments.easyprivacy)
    radar = TrackerRadar.from_index(arguments.tracker_radar_index)
    banner_classifier = BannerClassifier()
    platform_classifier = HostingPlatformClassifier()
    consentomatic_bundle = ConsentomaticBundle.from_rules_file(arguments.consentomatic_rules)

    findings = []
    for raw_trial in RawStore(raw_root).iter_trials():
        site = sites_by_id.get(raw_trial.site_id)
        if site is None:
            continue
        analyzed = AnalyzedTrial.classify(
            raw_trial,
            cookie_db=cookie_db,
            ep_matcher=ep_matcher,
            radar=radar,
            banner_classifier=banner_classifier,
            platform_classifier=platform_classifier,
            consentomatic_bundle=consentomatic_bundle,
        )
        findings.append(SitePrivacyFindings.from_trial(analyzed, site))

    if not findings:
        print("ERROR: no analyzable trials found under the raw root")
        return 1

    site_frame = SitePrivacyFindings.site_level_frame(findings)
    successful = site_frame.filter(~pl.col("blocked"))
    if successful.is_empty():
        print("ERROR: no successful sites after dropping blocked")
        return 1

    print_summary(successful)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
