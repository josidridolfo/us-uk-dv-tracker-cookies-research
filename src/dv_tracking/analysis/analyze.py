"""
The dv-analyze entrypoint: classify two explicit runs into per-site findings and write the parquet outputs and the LaTeX summary table.

Each run is a (crawl-date, geolocation) pair selected with --run. For each run the raw trials of exactly that one cell directory are loaded, the classifiers built from the pinned tracker-list snapshots derive one SitePrivacyFindings per trial, the per-site rows collapse to one row per site at the longest dwell, and the two runs are compared in one aggregate-only summary. Art 9 transmission rates use the count of Art-9-implicated sites as the denominator, not all sites.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from dv_tracking import PROJECT_ROOT
from dv_tracking.analysis.models import SitePrivacyFindings
from dv_tracking.analysis.parquet_store import FindingsParquetStore
from dv_tracking.analysis.summary import JurisdictionSummary, SummaryRun
from dv_tracking.classify.banner import BannerClassifier
from dv_tracking.classify.cookies import CookieDatabase
from dv_tracking.classify.easyprivacy import EasyPrivacyMatcher
from dv_tracking.classify.models import AnalyzedTrial
from dv_tracking.classify.platform import HostingPlatformClassifier
from dv_tracking.classify.tracker_radar import TrackerRadar
from dv_tracking.constants import DEFAULT_CONSENTOMATIC_RULES_PATH, GEOLOCATIONS, TRACKER_LISTS_DIR
from dv_tracking.crawl.models import ConsentomaticBundle
from dv_tracking.persistence import RawStore
from dv_tracking.sites import DEFAULT_SITES_PATH, DVSupportSite

DEFAULT_RAW_ROOT: Path = PROJECT_ROOT / "data" / "raw"
DEFAULT_OUTPUT_DIR: Path = PROJECT_ROOT / "data" / "processed"
DEFAULT_COOKIE_DATABASE_PATH: Path = (
    TRACKER_LISTS_DIR / "open_cookie_database" / "open-cookie-database.csv"
)
DEFAULT_EASYPRIVACY_PATH: Path = TRACKER_LISTS_DIR / "easyprivacy" / "easyprivacy.txt"
DEFAULT_TRACKER_RADAR_INDEX_PATH: Path = TRACKER_LISTS_DIR / "ddg_tracker_radar" / "index.json"


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    """
    Parses and validates the dv-analyze command-line arguments.

    Args:
        argv: The argument vector; defaults to sys.argv when None.
    Returns:
        arguments: The parsed argument namespace, with exactly two validated runs.
    """
    parser = argparse.ArgumentParser(description="Compare two runs of the raw crawl into per-site findings.")
    parser.add_argument("--raw-root", type=Path, default=DEFAULT_RAW_ROOT)
    parser.add_argument("--sites", type=Path, default=DEFAULT_SITES_PATH)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--run", nargs=2, metavar=("DATE", "GEO"), action="append", required=True,
        help="A run to summarise as crawl-date and geolocation; give exactly twice.",
    )
    parser.add_argument("--cookie-database", type=Path, default=DEFAULT_COOKIE_DATABASE_PATH)
    parser.add_argument("--easyprivacy", type=Path, default=DEFAULT_EASYPRIVACY_PATH)
    parser.add_argument("--tracker-radar-index", type=Path, default=DEFAULT_TRACKER_RADAR_INDEX_PATH)
    parser.add_argument("--consentomatic-rules", type=Path, default=DEFAULT_CONSENTOMATIC_RULES_PATH)
    arguments = parser.parse_args(argv)
    if len(arguments.run) != 2:
        parser.error("--run must be given exactly twice, once per run to compare")
    for _, geolocation in arguments.run:
        if geolocation not in GEOLOCATIONS:
            parser.error(f"unknown geolocation {geolocation!r}; expected one of {GEOLOCATIONS}")
    return arguments


def main(argv: list[str] | None = None) -> int:
    """
    Analyzes two explicit runs of the raw crawl and writes the parquet outputs and the LaTeX summary table.

    Each --run selects one (crawl-date, geolocation) cell directory under the raw
    root; the two runs are classified independently and compared side by side.

    Args:
        argv: The argument vector; defaults to sys.argv when None.
    Returns:
        exit_code: 0 on success, 1 when neither run produced findings, 2 when the raw root is absent.
    """
    arguments = parse_args(argv)
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

    store = RawStore(raw_root)
    findings_store = FindingsParquetStore(arguments.output_dir)
    summary_runs: list[SummaryRun] = []
    for crawl_date, geolocation in arguments.run:
        run_findings = []
        run_trials = []
        for raw_trial in store.iter_run(crawl_date, geolocation):
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
            run_trials.append(analyzed)
            run_findings.append(SitePrivacyFindings.from_trial(analyzed, site, crawl_date=crawl_date))
        site_path = findings_store.save_site_findings(run_findings, crawl_date, geolocation)
        cookie_path = findings_store.save_cookie_findings(run_trials, crawl_date, geolocation)
        exfil_path = findings_store.save_exfiltration_edges(run_trials, crawl_date, geolocation)
        print(f"wrote {site_path}")
        print(f"wrote {cookie_path}")
        print(f"wrote {exfil_path}")
        summary_runs.append(SummaryRun(run_findings, crawl_date, geolocation))

    summary = JurisdictionSummary.from_runs(summary_runs[0], summary_runs[1])
    if not summary.rows:
        print("ERROR: neither run produced any successful sites")
        return 1
    (date_a, geo_a), (date_b, geo_b) = arguments.run
    tex_path = summary.write_latex(
        arguments.output_dir / f"jurisdiction_summary_{date_a}_{geo_a}__{date_b}_{geo_b}.tex"
    )
    print(f"wrote {tex_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
