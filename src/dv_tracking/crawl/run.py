"""
The dv-crawl entrypoint: crawl a corpus at one geolocation and save raw trials.

Loads the site corpus, crawls each site once per trial through a Crawler, and
writes each raw trial to a RawStore. A per-site crawl failure is reported and
the run continues; the VPN exit gate is a separate command (dv-validate-vpn).
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from dv_tracking import PROJECT_ROOT
from dv_tracking.constants import DEFAULT_CONSENTOMATIC_RULES_PATH, GEOLOCATIONS
from dv_tracking.crawl.crawler import Crawler, ProxyConfigurationError
from dv_tracking.crawl.models import ConsentomaticBundle
from dv_tracking.persistence import RawStore
from dv_tracking.sites import DEFAULT_SITES_PATH, DVSupportSite
from dv_tracking.utils import load_env_file
from dv_tracking.vpn import (
    ExitCheckError,
    VpnLookupError,
    VpnValidation,
    decide_crawl_egress,
    look_up_exit_ip_and_country,
    look_up_exit_through_page,
)

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
        help="Optional VPN exit provider label recorded alongside the observed exit "
        "facts in the metadata provenance.",
    )
    return parser.parse_args(argv)


def main(
    argv: list[str] | None = None,
    *,
    crawler: object | None = None,
    look_up: Callable[[], tuple[str, str, str]] = look_up_exit_ip_and_country,
    look_up_through_page: Callable[..., tuple[str, str, str]] = look_up_exit_through_page,
) -> int:
    """
    Run the crawl over the corpus and save every trial to the raw store.

    Args:
        argv: The argument vector; defaults to sys.argv when None.
        crawler: An object exposing crawl(site, geolocation, trial) -> RawTrial;
            when None, a real Crawler is constructed after the egress decision and
            residential check.
        look_up: Callable returning the machine's exit IP, country, and org, used
            by the egress decision on the real-crawl path.
        look_up_through_page: Callable reading the exit through the crawl's own page
            egress, used by the blocking residential check on the real-crawl path.
    Returns:
        exit_code: 0 on completion (per-site failures are reported, not fatal);
            2 when the egress requires the proxy but its credentials are missing.
    Raises:
        ExitCheckError: On the real-crawl path when the observed exit fails the
            country/residential check; no site is crawled.
    """
    # Load repo-root .env before any credential read; exported vars still win.
    load_env_file()
    arguments = parse_arguments(argv)
    all_sites = DVSupportSite.load_all(arguments.sites)
    # Crawl only sites in the target jurisdiction; a UK exit must not reach US sites.
    sites = [s for s in all_sites if s.country.strip().lower() == arguments.geolocation]
    print(f"[{arguments.geolocation}] {len(sites)} of {len(all_sites)} sites match the geolocation")
    if not sites:
        print(f"[{arguments.geolocation}] no matching sites; nothing to crawl")
        return 0

    # Default provenance for the injected-crawler path (tests); the real-crawl
    # branch below overrides it with the gate-verified exit facts.
    exit_info = {"provider": arguments.exit_provider} if arguments.exit_provider else {}

    # Real-crawl path: direct only when already in-country on a residential exit,
    # else proxy; a missing proxy credential fails closed before any request.
    if crawler is None:
        try:
            use_proxy = decide_crawl_egress(arguments.geolocation, os.environ, look_up=look_up)
        except ProxyConfigurationError as error:
            print(f"ERROR: {error}")
            print("Do NOT run the crawl.")
            return 2

        # Hard block: observe the actual exit and refuse to crawl unless it reads
        # residential-in-target (pre-run gate on one rotated exit; see README).
        try:
            validation = VpnValidation.check(
                arguments.geolocation,
                use_proxy=use_proxy,
                look_up_through_page=look_up_through_page,
            )
        except VpnLookupError as error:
            raise ExitCheckError(
                f"{arguments.geolocation} exit could not be observed for the residential "
                f"check ({error}); crawl blocked"
            ) from error
        if not validation.passed:
            raise ExitCheckError(
                f"{arguments.geolocation} exit failed the residential check "
                f"(country={validation.resolved_country_code}, "
                f"residential={validation.exit_is_residential}); crawl blocked"
            )
        # Record the gate-verified exit as the run's provenance, shared by every trial.
        exit_info = validation.as_provenance(arguments.exit_provider)
        consentomatic_bundle = ConsentomaticBundle.from_rules_file(DEFAULT_CONSENTOMATIC_RULES_PATH)
        crawler = Crawler(use_proxy=use_proxy, consentomatic_bundle=consentomatic_bundle)

    store = RawStore(arguments.raw_root)

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
