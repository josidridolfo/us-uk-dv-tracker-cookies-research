"""
Drive OpenWPM over the corpus for one (crawl_date, geolocation) cell.

Invocation: `python -m dv_tracking.crawl.openwpm_run <geolocation> --sites ...`
inside the crawl container. Not a console script: the container never
pip-installs this project (source rides PYTHONPATH), so a `pyproject.toml`
entry would be dead code, same as `dv-check-exit` before it.

Container-side: imports `openwpm` (and `selenium` transitively) plus
`dv_tracking.{egress,constants,sites,crawl.resume}`, none of which pull
`tldextract`. Must not import `dv_tracking.utils`, `dv_tracking.crawl.models`,
or `dv_tracking.persistence` -- all three do, and all three fail here.

Only `no_interaction` runs. `reject_all`/`accept_all` exist in
`constants.CONDITIONS` so the counterbalancing and sequence shape already
fit the consent command landing later; this module raises rather than
guessing if it is ever asked to run a condition it has no command for.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import time
from datetime import date
from pathlib import Path

from openwpm.command_sequence import CommandSequence
from openwpm.commands.browser_commands import FinalizeCommand
from openwpm.commands.types import BaseCommand
from openwpm.config import BrowserParams, ManagerParams
from openwpm.errors import CommandExecutionError
from openwpm.storage.sql_provider import SQLiteStorageProvider
from openwpm.task_manager import TaskManager
from openwpm.utilities.platform_utils import get_version

from dv_tracking.constants import GEOLOCATIONS, NO_INTERACTION, TRACKER_LIST_SNAPSHOTS
from dv_tracking.crawl.resume import (
    attempt_counts,
    build_work_list,
    done_work_items,
    load_or_create_manifest,
    read_base_provenance,
    write_refusal,
)
from dv_tracking.egress import (
    ExitCheck,
    ProxyConfigurationError,
    RelayAddress,
    check_exit,
    utcnow_iso,
)
from dv_tracking.sites import DVSupportSite

INSTRUMENT: str = "openwpm"
SCHEMA_VERSION: str = "1"

# Only no_interaction is active this PR; see the module docstring.
ACTIVE_CONDITIONS = (NO_INTERACTION,)
REPLICATE_COUNT: int = 1

# xvfb, not headless: headless has no WebGL (a tracker signal and a bot
# signal) and its computed style/layout differ, which is what Consent-O-
# Matic's visibility matching and the candidate inventory will depend on. A
# headless calibration pass could not be compared against an xvfb sweep.
DISPLAY_MODE = "xvfb"

NAVIGATION_TIMEOUT_S: int = 90
DWELL_TIMEOUT_MARGIN_S: int = 10
FINALIZE_COMMAND_NAME: str = FinalizeCommand.__name__

# Bounds consecutive command failures before OpenWPM gives up on the whole
# invocation. The default (2*num_browsers+10 = 12 here) is tuned for a 100k-
# site Tranco crawl; on a ~250-site corpus with a handful of dead sites it
# is a methodological choice, not an inherited one.
FAILURE_LIMIT: int = 25

DEFAULT_EXIT_RECHECK_SECONDS: int = 600


class ExitVerificationError(Exception):
    """Raised when the pre-crawl exit gate could not confirm a passing exit."""

    def __init__(self, message: str, check: ExitCheck | None = None) -> None:
        super().__init__(message)
        self.check = check


class ProxyPrefsNotPinnedError(Exception):
    """Raised when BrowserParams.prefs does not pin a manual proxy at the verified relay."""


def _append_jsonl(path: Path, row: dict) -> None:
    """Append one JSON line to a sidecar log, flushing before the lock releases."""
    with path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.write(json.dumps(row) + "\n")
            handle.flush()
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _parse_duration_seconds(value: str) -> int:
    """Parse a duration like "10m", "600s", or a bare integer of seconds."""
    value = value.strip()
    multipliers = {"s": 1, "m": 60, "h": 3600}
    if value and value[-1] in multipliers:
        return int(value[:-1]) * multipliers[value[-1]]
    return int(value)


def _required_env(name: str) -> str:
    """Read a required environment variable, failing closed rather than defaulting."""
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is required and is not set")
    return value


def _normalize_version(value: str | bytes) -> str:
    """Decode get_version()'s bytes return (one of its two internal paths does not)."""
    return value.decode("utf-8") if isinstance(value, bytes) else value


def _exit_check_row(check: ExitCheck) -> dict:
    """Project an ExitCheck to the row shape written to exit-checks.jsonl."""
    return {
        **check.as_provenance(),
        "expected_geolocation": check.expected_geolocation,
        "passed": check.passed,
    }


def verify_exit(geolocation: str, relay: RelayAddress) -> ExitCheck:
    """
    Verify the relay's observed exit.

    Catches bare Exception, not just the gate's own error types: an
    unhandled exception in the gate is itself a reason to abort.

    Args:
        geolocation: The geolocation the crawl must egress from ("uk" or "us").
        relay: The relay address to check through.
    Returns:
        check: The passing exit check.
    Raises:
        ExitVerificationError: When the gate does not confirm a pass. Carries
            the ExitCheck when one was produced (e.g. a country mismatch),
            so the failure reaches exit-checks.jsonl rather than only stdout.
    """
    try:
        check = check_exit(geolocation, relay)
    except Exception as cause:
        raise ExitVerificationError(f"exit gate raised: {cause}") from cause
    if not check.passed:
        raise ExitVerificationError(
            f"exit gate did not pass: {check.observed.ip} "
            f"({check.observed.country}, {check.observed.exit_class})",
            check=check,
        )
    return check


def assert_proxy_prefs_pinned(prefs: dict, relay: RelayAddress) -> None:
    """
    Assert prefs pin a manual proxy at the verified relay's host and port.

    Proves configuration, not behaviour. deploy_firefox() reapplies prefs on
    every relaunch, so this means something continuously rather than only at
    crawl start -- but WebRTC and DNS-over-HTTPS bypass network.proxy.*
    entirely, and OpenWPM's own optimize_prefs() touches neither. Only an
    in-browser confirmation (reading the exit from inside a page load) can
    prove the browser's traffic actually took this path; that is a later PR.

    Args:
        prefs: One browser's BrowserParams.prefs.
        relay: The relay address the gate just verified.
    Raises:
        ProxyPrefsNotPinnedError: When prefs do not match.
    """
    expected = {
        "network.proxy.type": 1,
        "network.proxy.http": relay.host,
        "network.proxy.http_port": relay.port,
        "network.proxy.ssl": relay.host,
        "network.proxy.ssl_port": relay.port,
    }
    mismatches = {k: (prefs.get(k), v) for k, v in expected.items() if prefs.get(k) != v}
    if mismatches:
        raise ProxyPrefsNotPinnedError(
            f"prefs do not pin a manual proxy at {relay.host}:{relay.port}: {mismatches}"
        )


class StampVisitCommand(BaseCommand):
    """Appends the (site, condition, replicate) identity for this visit_id.

    Placed first in the sequence, before the get: BrowserManager breaks out
    of a sequence on any non-ok command and never reaches later ones, so a
    stamp placed after the get would leave a failed visit with no identity
    row, invisible to done_work_items, re-crawled forever.
    """

    def __init__(
        self,
        visits_path: Path,
        site_id: str,
        condition: str,
        replicate: int,
        arm_position: int,
        exit_check_index: int,
    ) -> None:
        self.visits_path = visits_path
        self.site_id = site_id
        self.condition = condition
        self.replicate = replicate
        self.arm_position = arm_position
        self.exit_check_index = exit_check_index

    def __repr__(self) -> str:
        return f"StampVisitCommand({self.site_id}, {self.condition}, {self.replicate})"

    def execute(self, webdriver, browser_params, manager_params, extension_socket) -> None:
        _append_jsonl(
            self.visits_path,
            {
                "visit_id": self.visit_id,
                "browser_id": self.browser_id,
                "site_id": self.site_id,
                "condition": self.condition,
                "replicate": self.replicate,
                "arm_position": self.arm_position,
                "exit_check_index": self.exit_check_index,
                "stamped_at_utc": utcnow_iso(),
            },
        )


class BoundaryCommand(BaseCommand):
    """Records the synthetic phase-boundary instant for an unclicked visit.

    The no_interaction arm clicks nothing, but the matched-offset comparison
    the plan requires needs a boundary instant at the same point in its
    sequence as a real click would occupy; nothing wrote it before this.
    """

    def __init__(self, boundaries_path: Path) -> None:
        self.boundaries_path = boundaries_path

    def __repr__(self) -> str:
        return "BoundaryCommand()"

    def execute(self, webdriver, browser_params, manager_params, extension_socket) -> None:
        _append_jsonl(
            self.boundaries_path, {"visit_id": self.visit_id, "boundary_utc": utcnow_iso()}
        )


class DwellCommand(BaseCommand):
    """Sleeps without navigating, for the post-boundary dwell window."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds

    def __repr__(self) -> str:
        return f"DwellCommand({self.seconds})"

    def execute(self, webdriver, browser_params, manager_params, extension_socket) -> None:
        time.sleep(self.seconds)


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
    parser.add_argument("--crawl-date", default=date.today().isoformat())
    # Required, no default: DVSupportSite's own default resolves relative to
    # the package install location, which this container never populates
    # (data/ is a runtime volume, not baked into the image).
    parser.add_argument("--sites", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, default=Path(os.environ.get("DV_DATA_ROOT", "/data")))
    parser.add_argument(
        "--exit-recheck-seconds",
        type=int,
        default=int(os.environ.get("DV_EXIT_RECHECK_SECONDS", str(DEFAULT_EXIT_RECHECK_SECONDS))),
    )
    parser.add_argument("--purpose", choices=("calibration", "sweep"), default="sweep")
    return parser.parse_args(argv)


def _crawl_cell(arguments: argparse.Namespace, cell_dir: Path) -> int:
    """Run one cell's crawl once the cell lock is held; see main() for the exit-code contract."""
    geolocation = arguments.geolocation
    db_path = cell_dir / "crawl-data.sqlite"
    manifest_path = cell_dir / "manifest.json"
    visits_path = cell_dir / "visits.jsonl"
    boundaries_path = cell_dir / "boundaries.jsonl"
    exit_checks_path = cell_dir / "exit-checks.jsonl"
    runs_path = cell_dir / "runs.jsonl"

    all_sites = DVSupportSite.load_all(arguments.sites)
    sites = [s for s in all_sites if s.country.strip().lower() == geolocation]
    print(f"[{geolocation}] {len(sites)} of {len(all_sites)} sites match the geolocation")
    if not sites:
        print(f"[{geolocation}] no matching sites; refusing rather than returning a silent 0")
        return 1

    manifest = load_or_create_manifest(manifest_path, geolocation, INSTRUMENT, SCHEMA_VERSION)
    seed = manifest["ordering_seed"]

    work_list = build_work_list(sites, seed, ACTIVE_CONDITIONS, REPLICATE_COUNT)
    already_done = done_work_items(db_path, visits_path, FINALIZE_COMMAND_NAME)
    remaining = [
        item
        for item in work_list
        if (item.site.site_id, item.condition.name, item.replicate) not in already_done
    ]
    print(
        f"[{geolocation}] {len(work_list)} total visits, {len(already_done)} already terminal, "
        f"{len(remaining)} remaining"
    )

    openwpm_version, firefox_version = (_normalize_version(v) for v in get_version())
    base_provenance = read_base_provenance(arguments.data_root / "openwpm-base-provenance.txt")
    _append_jsonl(
        runs_path,
        {
            "started_at_utc": utcnow_iso(),
            "crawl_date": arguments.crawl_date,
            "repo_commit": _required_env("DV_REPO_COMMIT"),
            "openwpm_tag": base_provenance.get("openwpm_tag"),
            "openwpm_commit": base_provenance.get("openwpm_commit"),
            "openwpm_version_string": openwpm_version,
            "base_image": base_provenance.get("image"),
            "base_image_id": base_provenance.get("image_id"),
            "firefox_version": firefox_version,
            # PR 4 (firefox-prefs) has not landed: no pref set is pinned yet,
            # so there is nothing to fingerprint. Absent rather than faked.
            "pref_fingerprint": None,
            "tracker_list_snapshots": [
                {"name": s.name, "snapshot_date": s.snapshot_date, "pinned_commit": s.pinned_commit}
                for s in TRACKER_LIST_SNAPSHOTS
            ],
            "display_mode": DISPLAY_MODE,
            "conditions": [
                {
                    "name": c.name,
                    "pre_dwell_s": c.pre_dwell_s,
                    "post_dwell_s": c.post_dwell_s,
                    "action": c.action,
                }
                for c in ACTIVE_CONDITIONS
            ],
            "replicate_count": REPLICATE_COUNT,
            "navigation_timeout_s": NAVIGATION_TIMEOUT_S,
            "exit_recheck_seconds": arguments.exit_recheck_seconds,
            "failure_limit": FAILURE_LIMIT,
            "sites_path": str(arguments.sites),
            "sites_sha256": hashlib.sha256(arguments.sites.read_bytes()).hexdigest(),
            "site_count": len(sites),
            "purpose": arguments.purpose,
        },
    )

    if not remaining:
        return 0

    iproyal_lifetime_s = _parse_duration_seconds(os.environ.get("IPROYAL_LIFETIME", "10m"))
    if arguments.exit_recheck_seconds > iproyal_lifetime_s:
        print(
            f"ERROR: --exit-recheck-seconds ({arguments.exit_recheck_seconds}) exceeds "
            f"IPROYAL_LIFETIME ({iproyal_lifetime_s}s); a rotation could pass unverified"
        )
        return 6

    try:
        relay = RelayAddress.from_environ(os.environ)
    except ProxyConfigurationError as error:
        print(f"ERROR: {error}")
        print("Do NOT run the crawl.")
        return 2

    try:
        check = verify_exit(geolocation, relay)
    except ExitVerificationError as error:
        if error.check is not None:
            _append_jsonl(exit_checks_path, _exit_check_row(error.check))
        print(f"ERROR: {error}")
        print("Do NOT run the crawl.")
        return 2
    _append_jsonl(exit_checks_path, _exit_check_row(check))
    exit_check_index = 0
    last_checked_at = time.monotonic()

    browser_params = BrowserParams(display_mode=DISPLAY_MODE)
    browser_params.http_instrument = True
    browser_params.cookie_instrument = True
    browser_params.navigation_instrument = True
    browser_params.dns_instrument = True

    try:
        assert_proxy_prefs_pinned(browser_params.prefs, relay)
    except ProxyPrefsNotPinnedError as error:
        write_refusal(manifest_path, manifest, str(error), utcnow_iso())
        print(f"ERROR: {error}")
        print("Refusing to crawl: proxy prefs are not pinned to the verified relay.")
        return 3

    manager_params = ManagerParams(num_browsers=1)
    manager_params.data_directory = cell_dir
    manager_params.log_path = cell_dir / "openwpm.log"
    manager_params.failure_limit = FAILURE_LIMIT

    attempts = attempt_counts(visits_path)
    try:
        with TaskManager(
            manager_params, [browser_params], SQLiteStorageProvider(db_path), None
        ) as manager:
            for item in remaining:
                if time.monotonic() - last_checked_at >= arguments.exit_recheck_seconds:
                    try:
                        check = verify_exit(geolocation, relay)
                    except ExitVerificationError as error:
                        if error.check is not None:
                            _append_jsonl(exit_checks_path, _exit_check_row(error.check))
                        print(f"ERROR: {error}")
                        print("Do NOT run the crawl. Aborting with visits remaining.")
                        return 2
                    _append_jsonl(exit_checks_path, _exit_check_row(check))
                    exit_check_index += 1
                    last_checked_at = time.monotonic()

                key = (item.site.site_id, item.condition.name, item.replicate)
                sequence = CommandSequence(
                    item.site.url,
                    reset=True,
                    site_rank=item.position,
                    retry_number=attempts.get(key, 0) + 1,
                    callback=lambda ok, site_id=item.site.site_id: print(
                        f"[{geolocation}] {site_id}: {'ok' if ok else 'failed'}"
                    ),
                )
                sequence.append_command(
                    StampVisitCommand(
                        visits_path,
                        item.site.site_id,
                        item.condition.name,
                        item.replicate,
                        item.arm_position,
                        exit_check_index,
                    )
                )
                sequence.get(
                    sleep=item.condition.pre_dwell_s,
                    timeout=NAVIGATION_TIMEOUT_S + int(item.condition.pre_dwell_s),
                )
                if item.condition.action != "none":
                    raise NotImplementedError(
                        f"condition action {item.condition.action!r} has no command yet"
                    )
                sequence.append_command(BoundaryCommand(boundaries_path))
                sequence.append_command(
                    DwellCommand(item.condition.post_dwell_s),
                    timeout=int(item.condition.post_dwell_s) + DWELL_TIMEOUT_MARGIN_S,
                )
                manager.execute_command_sequence(sequence)
    except CommandExecutionError as error:
        print(f"ERROR: crawl failure budget exceeded: {error}")
        return 4

    try:
        final_check = verify_exit(geolocation, relay)
        _append_jsonl(exit_checks_path, _exit_check_row(final_check))
    except ExitVerificationError as error:
        if error.check is not None:
            _append_jsonl(exit_checks_path, _exit_check_row(error.check))
        print(f"WARNING: final exit check did not pass: {error}")

    return 0


def main(argv: list[str] | None = None) -> int:
    """
    Run the crawl over the corpus for one (crawl_date, geolocation) cell.

    Args:
        argv: The argument vector; defaults to sys.argv when None.
    Returns:
        exit_code: 0 completed; 1 no sites matched; 2 relay gate did not
            pass or raised; 3 refused, proxy prefs not pinned to the
            verified relay; 4 crawl failure budget exceeded; 5 another
            invocation holds this cell's lock; 6 --exit-recheck-seconds
            exceeds IPROYAL_LIFETIME.
    """
    arguments = parse_arguments(argv)
    cell_dir = (
        arguments.data_root / "raw" / f"{arguments.crawl_date}_{arguments.geolocation}" / "openwpm"
    )
    cell_dir.mkdir(parents=True, exist_ok=True)

    lock_handle = (cell_dir / ".lock").open("a")
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(f"ERROR: another invocation holds the lock for this cell ({cell_dir / '.lock'})")
        return 5
    try:
        return _crawl_cell(arguments, cell_dir)
    finally:
        lock_handle.close()


if __name__ == "__main__":
    raise SystemExit(main())
