"""
The dv-crawl entrypoint: drive OpenWPM over the corpus for one (crawl_date, geolocation) cell.

One OpenWPM crawl per cell, not per condition: sites are iterated in a seeded
random order, and each site's active conditions run back to back on the same
browser with a fresh profile between them. The work list is reconciled
against the cell's own `crawl-data.sqlite` on every invocation, so an
interrupted crawl resumes rather than restarts.

Container-side: imports `openwpm`, `selenium` (transitively), and
`dv_tracking.{egress,constants,sites}`, none of which pull `tldextract`. Must
not import `dv_tracking.utils`, `dv_tracking.crawl.models`, or
`dv_tracking.persistence` -- all three do, and all three fail where this
module runs.

Only `no_interaction` is exercised here. `reject_all` and `accept_all` exist
in `constants.CONDITIONS` so this module's shape -- counterbalanced order,
back-to-back arms, a manifest keyed by condition -- is already correct for
the consent command that lands in a later PR; running them now would just
be three identical unclicked visits.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import random
import secrets
import sqlite3
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from openwpm.command_sequence import CommandSequence
from openwpm.commands.types import BaseCommand
from openwpm.config import BrowserParams, ManagerParams
from openwpm.storage.sql_provider import SQLiteStorageProvider
from openwpm.task_manager import TaskManager
from openwpm.utilities.platform_utils import get_version

from dv_tracking.constants import GEOLOCATIONS, NO_INTERACTION, TRACKER_LIST_SNAPSHOTS, Condition
from dv_tracking.egress import ExitCheck, RelayAddress, check_exit, utcnow_iso
from dv_tracking.sites import DVSupportSite

INSTRUMENT: str = "openwpm"
SCHEMA_VERSION: str = "1"

# Only no_interaction is active this PR; see the module docstring.
ACTIVE_CONDITIONS: tuple[Condition, ...] = (NO_INTERACTION,)
REPLICATE_COUNT: int = 1

NAVIGATION_TIMEOUT_S: int = 90
FINALIZE_TERMINAL_COMMAND: str = "FinalizeCommand"
# IPROYAL_LIFETIME defaults to 10m; re-checking at that cadence samples each
# session rotation (about thirty times over a multi-hour cell) rather than
# every visit, which would be roughly 900 ipinfo.io lookups across both
# jurisdictions against its 1,000/day unauthenticated cap.
EXIT_RECHECK_INTERVAL_S: float = float(os.environ.get("DV_EXIT_RECHECK_SECONDS", "600"))


class ExitVerificationError(Exception):
    """Raised when the pre-crawl exit gate does not confirm a passing exit."""


@dataclass(frozen=True)
class WorkItem:
    """One (site, condition, replicate) unit of crawl work."""

    site: DVSupportSite
    condition: Condition
    replicate: int


class StampVisitCommand(BaseCommand):
    """Appends one sidecar-manifest row mapping this visit to its identity.

    OpenWPM assigns `visit_id` and `browser_id` to every command in a
    sequence before `execute` runs (`BaseCommand.set_visit_browser_id`), so
    this command's job is only to pair that assignment with the
    (site_id, condition, replicate) identity it was built with -- OpenWPM has
    no notion of either, and ingest needs the mapping to tell visits in the
    same cell apart.
    """

    def __init__(self, manifest_path: Path, site_id: str, condition: str, replicate: int) -> None:
        self.manifest_path = manifest_path
        self.site_id = site_id
        self.condition = condition
        self.replicate = replicate

    def __repr__(self) -> str:
        return f"StampVisitCommand({self.site_id}, {self.condition}, {self.replicate})"

    def execute(self, webdriver, browser_params, manager_params, extension_socket) -> None:
        """Append one identity row; runs in the browser's own process, so writes are file-locked."""
        row = {
            "visit_id": self.visit_id,
            "browser_id": self.browser_id,
            "site_id": self.site_id,
            "condition": self.condition,
            "replicate": self.replicate,
        }
        with self.manifest_path.open("a", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.write(json.dumps(row) + "\n")
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def counterbalanced_conditions(site_id: str, seed: int, conditions: tuple[Condition, ...]) -> list[Condition]:
    """
    Permute a site's active conditions deterministically from the run seed.

    Keyed on site_id so the arm order varies site to site while staying
    reproducible from the recorded seed; vacuous with one active condition,
    correct once more are.

    Args:
        site_id: The site whose arm order is being decided.
        seed: The crawl's ordering seed.
        conditions: The active conditions to permute.
    Returns:
        ordered: The conditions in this site's counterbalanced order.
    """
    rng = random.Random(f"{seed}:{site_id}")
    ordered = list(conditions)
    rng.shuffle(ordered)
    return ordered


def build_work_list(
    sites: list[DVSupportSite], seed: int, conditions: tuple[Condition, ...] = ACTIVE_CONDITIONS
) -> list[WorkItem]:
    """
    Build the full (site, condition, replicate) work list in seeded, counterbalanced order.

    Sites are shuffled by the seed; each site's active conditions run back
    to back in their own counterbalanced order immediately after it.

    Args:
        sites: The sites to crawl, already filtered to one geolocation.
        seed: The crawl's ordering seed.
        conditions: The active conditions to cross with every site.
    Returns:
        work_list: One WorkItem per (site, condition, replicate), in run order.
    """
    ordered_sites = list(sites)
    random.Random(seed).shuffle(ordered_sites)
    work_list = []
    for site in ordered_sites:
        for condition in counterbalanced_conditions(site.site_id, seed, conditions):
            for replicate in range(1, REPLICATE_COUNT + 1):
                work_list.append(WorkItem(site, condition, replicate))
    return work_list


def _visit_identities(manifest_jsonl_path: Path) -> dict[int, tuple[str, str, int]]:
    """Map visit_id to (site_id, condition, replicate) from the sidecar manifest, if present."""
    identities: dict[int, tuple[str, str, int]] = {}
    if not manifest_jsonl_path.is_file():
        return identities
    for line in manifest_jsonl_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        identities[row["visit_id"]] = (row["site_id"], row["condition"], row["replicate"])
    return identities


def _terminal_outcomes(db_path: Path) -> dict[int, str]:
    """
    Reduce crawl_history to one terminal outcome per visit_id that reached one.

    A command failure (any status but "ok") makes BrowserManager call
    finalize_visit_id and break out of the sequence without running the rest,
    so FinalizeCommand's own row exists only for a visit that ran clean; the
    last crawl_history row for any other visit is therefore its failure.

    Args:
        db_path: The cell's crawl-data.sqlite path.
    Returns:
        outcomes: visit_id -> "completed" | "timeout" | "error", terminal visits only.
    """
    if not db_path.is_file():
        return {}
    last_row: dict[int, tuple[str, str]] = {}
    with sqlite3.connect(str(db_path)) as connection:
        rows = connection.execute(
            "SELECT visit_id, command, command_status FROM crawl_history ORDER BY visit_id, rowid"
        ).fetchall()
    for visit_id, command, status in rows:
        last_row[visit_id] = (command, status)
    outcomes = {}
    for visit_id, (command, status) in last_row.items():
        if command == FINALIZE_TERMINAL_COMMAND and status == "ok":
            outcomes[visit_id] = "completed"
        elif status == "timeout":
            outcomes[visit_id] = "timeout"
        else:
            outcomes[visit_id] = "error"
    return outcomes


def done_work_items(db_path: Path, manifest_jsonl_path: Path) -> set[tuple[str, str, int]]:
    """
    Reconcile the cell's SQLite against the sidecar manifest for the resume predicate.

    The terminal outcome lives in crawl_history, OpenWPM's own primary
    artifact; the sidecar manifest only supplies the (site_id, condition,
    replicate) identity crawl_history has no notion of. Together they are
    the complete resume predicate -- no separate ledger file is kept.

    Args:
        db_path: The cell's crawl-data.sqlite path.
        manifest_jsonl_path: The sidecar visit-identity JSONL path.
    Returns:
        done: Every (site_id, condition, replicate) with a terminal outcome,
            successful or not -- a recorded failure is a result, not a retry.
    """
    identities = _visit_identities(manifest_jsonl_path)
    outcomes = _terminal_outcomes(db_path)
    return {identities[visit_id] for visit_id in outcomes if visit_id in identities}


def verify_exit(geolocation: str) -> ExitCheck:
    """
    Verify the relay's observed exit, raising on anything short of a clean pass.

    Any failure aborts rather than narrows to a specific cause: a config
    error, a read failure, a country/datacenter mismatch, or any other
    exception the gate raises all mean the same thing here, which is that
    the crawl must not touch the network. Catching bare Exception rather
    than just ProxyConfigurationError/ExitReadError is deliberate: an
    unhandled exception in the gate is itself a reason to abort, not a
    third category to let escape as a traceback.

    Args:
        geolocation: The geolocation the crawl must egress from ("uk" or "us").
    Returns:
        check: The passing exit check.
    Raises:
        ExitVerificationError: When the gate does not confirm a pass.
    """
    try:
        relay = RelayAddress.from_environ(os.environ)
        check = check_exit(geolocation, relay)
    except Exception as cause:
        raise ExitVerificationError(f"exit gate raised: {cause}") from cause
    if not check.passed:
        raise ExitVerificationError(
            f"exit gate did not pass: {check.observed.ip} "
            f"({check.observed.country}, {check.observed.exit_class})"
        )
    return check


def _required_env(name: str) -> str:
    """Read a required environment variable, failing closed rather than defaulting."""
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is required and is not set")
    return value


def _read_base_provenance(path: Path) -> dict[str, str]:
    """
    Parse scripts/fetch-openwpm.sh's key=value provenance file.

    Args:
        path: The provenance file path.
    Returns:
        provenance: The file's key=value pairs.
    Raises:
        RuntimeError: When the file is absent -- an unidentifiable instrument
            must not produce data.
    """
    if not path.is_file():
        raise RuntimeError(
            f"required base-image provenance file missing: {path} "
            "(run scripts/fetch-openwpm.sh first)"
        )
    provenance = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        key, _, value = line.partition("=")
        provenance[key.strip()] = value.strip()
    return provenance


def load_or_create_manifest(manifest_path: Path, geolocation: str, data_root: Path) -> dict:
    """
    Load the cell manifest if a resumed run already wrote one, else build a fresh one.

    The seed lives in this manifest, so resuming must reuse the existing one
    rather than regenerate it -- a new seed would make the counterbalancing
    unrecoverable after the fact.

    Args:
        manifest_path: The cell manifest JSON path.
        geolocation: The geolocation this cell crawls.
        data_root: The data root, used to locate the base-image provenance file.
    Returns:
        manifest: The manifest dict, loaded or newly built.
    """
    if manifest_path.is_file():
        return json.loads(manifest_path.read_text(encoding="utf-8"))

    base_provenance = _read_base_provenance(data_root / "openwpm-base-provenance.txt")
    # get_version() returns str on one internal path (a VERSION file) and
    # bytes on the other (subprocess.check_output, undecoded); normalise both.
    openwpm_version, firefox_version = (
        value.decode("utf-8") if isinstance(value, bytes) else value for value in get_version()
    )
    manifest = {
        "instrument": INSTRUMENT,
        "schema_version": SCHEMA_VERSION,
        "geolocation": geolocation,
        "openwpm_tag": base_provenance.get("openwpm_tag"),
        "openwpm_commit": base_provenance.get("openwpm_commit"),
        "openwpm_version_string": openwpm_version,
        "base_image": base_provenance.get("image"),
        "base_image_id": base_provenance.get("image_id"),
        "firefox_version": firefox_version,
        # PR 4 (firefox-prefs) has not landed: no pref set is pinned yet, so
        # there is nothing to fingerprint. Absent rather than faked.
        "pref_fingerprint": None,
        "repo_commit": _required_env("DV_REPO_COMMIT"),
        "tracker_list_snapshots": [
            {"name": s.name, "snapshot_date": s.snapshot_date, "pinned_commit": s.pinned_commit}
            for s in TRACKER_LIST_SNAPSHOTS
        ],
        "ordering_seed": secrets.randbits(63),
        "created_at_utc": utcnow_iso(),
        "exit_checks": [],
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return manifest


def _append_exit_checks(manifest_path: Path, checks: list[ExitCheck]) -> None:
    """Append this invocation's exit-check history to the manifest on disk."""
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["exit_checks"].extend(check.as_provenance() for check in checks)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")


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
    # No default: DVSupportSite's own default resolves relative to the
    # package install location, which is not where the corpus mounts inside
    # this container (the data/ directory is a runtime volume, not baked
    # into the image), so a silent default would point at a path that never
    # exists here.
    parser.add_argument("--sites", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, default=Path(os.environ.get("DV_DATA_ROOT", "/data")))
    parser.add_argument("--num-browsers", type=int, default=1)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """
    Run the crawl over the corpus for one (crawl_date, geolocation) cell.

    Args:
        argv: The argument vector; defaults to sys.argv when None.
    Returns:
        exit_code: 0 on completion (per-visit failures are recorded, not
            fatal), 2 when the exit gate does not confirm a pass.
    """
    arguments = parse_arguments(argv)
    geolocation = arguments.geolocation

    all_sites = DVSupportSite.load_all(arguments.sites)
    sites = [s for s in all_sites if s.country.strip().lower() == geolocation]
    print(f"[{geolocation}] {len(sites)} of {len(all_sites)} sites match the geolocation")
    if not sites:
        print(f"[{geolocation}] no matching sites; nothing to crawl")
        return 0

    cell_dir = arguments.data_root / "raw" / f"{arguments.crawl_date}_{geolocation}" / "openwpm"
    cell_dir.mkdir(parents=True, exist_ok=True)
    db_path = cell_dir / "crawl-data.sqlite"
    manifest_path = cell_dir / "manifest.json"
    visits_path = cell_dir / "visits.jsonl"

    manifest = load_or_create_manifest(manifest_path, geolocation, arguments.data_root)
    seed = manifest["ordering_seed"]

    work_list = build_work_list(sites, seed)
    already_done = done_work_items(db_path, visits_path)
    remaining = [
        item for item in work_list
        if (item.site.site_id, item.condition.name, item.replicate) not in already_done
    ]
    print(
        f"[{geolocation}] {len(work_list)} total visits, {len(already_done)} already terminal, "
        f"{len(remaining)} remaining"
    )
    if not remaining:
        return 0

    exit_checks: list[ExitCheck] = []
    try:
        exit_checks.append(verify_exit(geolocation))
    except ExitVerificationError as error:
        print(f"ERROR: {error}")
        print("Do NOT run the crawl.")
        return 2
    last_checked_at = time.monotonic()

    browser_params = [BrowserParams(display_mode="headless") for _ in range(arguments.num_browsers)]
    for params in browser_params:
        params.http_instrument = True
        params.cookie_instrument = True
        params.navigation_instrument = True
        params.dns_instrument = True
    manager_params = ManagerParams(num_browsers=arguments.num_browsers)
    manager_params.data_directory = cell_dir
    manager_params.log_path = cell_dir / "openwpm.log"

    try:
        with TaskManager(
            manager_params, browser_params, SQLiteStorageProvider(db_path), None
        ) as manager:
            for index, item in enumerate(remaining):
                if time.monotonic() - last_checked_at >= EXIT_RECHECK_INTERVAL_S:
                    try:
                        exit_checks.append(verify_exit(geolocation))
                    except ExitVerificationError as error:
                        print(f"ERROR: {error}")
                        print("Do NOT run the crawl. Aborting with visits remaining.")
                        return 2
                    last_checked_at = time.monotonic()

                dwell = item.condition.pre_dwell_s + item.condition.post_dwell_s
                sequence = CommandSequence(
                    item.site.url,
                    reset=True,
                    site_rank=index,
                    callback=lambda ok, site_id=item.site.site_id: print(
                        f"[{geolocation}] {site_id}: {'ok' if ok else 'failed'}"
                    ),
                )
                sequence.get(sleep=dwell, timeout=max(NAVIGATION_TIMEOUT_S, int(dwell) + 30))
                sequence.append_command(
                    StampVisitCommand(visits_path, item.site.site_id, item.condition.name, item.replicate)
                )
                manager.execute_command_sequence(sequence)
    finally:
        _append_exit_checks(manifest_path, exit_checks)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
