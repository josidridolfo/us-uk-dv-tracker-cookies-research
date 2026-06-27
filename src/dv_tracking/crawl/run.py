"""
The dv-crawl entrypoint: crawl a corpus at one geolocation and save raw trials.

Loads the site corpus, crawls each site once per trial through a Crawler, and
writes each raw trial to a RawStore. A per-site crawl failure is reported and
the run continues; the VPN exit gate is a separate command (dv-validate-vpn).
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

from dv_tracking import PROJECT_ROOT
from dv_tracking.constants import DEFAULT_CONSENTOMATIC_RULES_PATH, GEOLOCATIONS
from dv_tracking.crawl.crawler import Crawler
from dv_tracking.crawl.models import ConsentomaticBundle
from dv_tracking.persistence import RawStore
from dv_tracking.sites import DEFAULT_SITES_PATH, DVSupportSite

DEFAULT_RAW_ROOT: Path = PROJECT_ROOT / "data" / "raw"
DEFAULT_TRIALS: int = 3


def parse_arguments(argv: list[str] | None) -> argparse.Namespace:
    """
    Parse the dv-crawl command-line arguments.

    Args:
        argv: The argument vector; defaults to sys.argv when None.
    Returns:
        arguments: The parsed argument namespace.
    """
    parser = argparse.ArgumentParser(description="Crawl the DV support corpus at one geolocation.")
    parser.add_argument("geolocation", choices=GEOLOCATIONS)
    parser.add_argument(
        "--crawl-date",
        default=datetime.now(tz=UTC).strftime("%Y-%m-%d"),
        help="Crawl date used in the cell directory name; defaults to today (UTC).",
    )
    parser.add_argument("--sites", type=Path, default=DEFAULT_SITES_PATH)
    parser.add_argument("--raw-root", type=Path, default=DEFAULT_RAW_ROOT)
    parser.add_argument("--trials", type=int, default=DEFAULT_TRIALS)
    parser.add_argument(
        "--exit-provider",
        default=None,
        help="Optional VPN exit provider recorded as provenance in the metadata.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, *, crawler: object | None = None) -> int:
    """
    Run the crawl over the corpus and save every trial to the raw store.

    Args:
        argv: The argument vector; defaults to sys.argv when None.
        crawler: An object exposing crawl(site, geolocation, trial) -> RawTrial;
            when None, a real Crawler is constructed.
    Returns:
        exit_code: 0 on completion (per-site failures are reported, not fatal).
    """
    arguments = parse_arguments(argv)
    all_sites = DVSupportSite.load_all(arguments.sites)
    # Crawl only sites in the target jurisdiction; a UK exit must not reach US sites.
    sites = [s for s in all_sites if s.country.strip().lower() == arguments.geolocation]
    print(f"[{arguments.geolocation}] {len(sites)} of {len(all_sites)} sites match the geolocation")
    if not sites:
        print(f"[{arguments.geolocation}] no matching sites; nothing to crawl")
        return 0

    consentomatic_bundle = ConsentomaticBundle.from_rules_file(DEFAULT_CONSENTOMATIC_RULES_PATH)
    crawler = (
        Crawler(consentomatic_bundle=consentomatic_bundle) if crawler is None else crawler
    )
    store = RawStore(arguments.raw_root)
    exit_info = {"provider": arguments.exit_provider} if arguments.exit_provider else {}

    # Round-robin: every site once per trial (dwell) before the next pass, so a
    # site's repeat visits are spaced across the run rather than back-to-back.
    for trial in range(1, arguments.trials + 1):
        for site in sites:
            try:
                trial_record = crawler.crawl(site, arguments.geolocation, trial)
                trial_dir = store.save(trial_record, exit_info, arguments.crawl_date)
            except Exception as error:
                print(f"[{arguments.geolocation}] {site.site_id} trial {trial}: ERROR {error}")
                continue
            print(f"[{arguments.geolocation}] {site.site_id} trial {trial}: saved {trial_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
