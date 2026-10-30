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

All three conditions in `constants.CONDITIONS` are active. Detection and,
for `reject_all`/`accept_all`, the decision itself run through
`ConsentInteractionCommand`, which depends only on the
`dv_tracking.crawl.consent_interaction.ConsentInteractor` interface, not on
`AutoconsentInteractor` specifically -- a different engine means replacing
this module's one `CONSENT_INTERACTOR` construction, not touching the
command. `_crawl_cell` still refuses before opening a browser, rather than
guessing, if it is ever asked to run a condition whose action the
configured interactor does not support.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import secrets
import time
import traceback
from collections.abc import Mapping
from datetime import date, datetime
from enum import IntEnum
from pathlib import Path

from openwpm.command_sequence import CommandSequence
from openwpm.commands.browser_commands import FinalizeCommand
from openwpm.commands.types import BaseCommand
from openwpm.config import ALL_RESOURCE_TYPES, BrowserParams, ManagerParams
from openwpm.errors import CommandExecutionError
from openwpm.storage.leveldb import LevelDbProvider
from openwpm.storage.sql_provider import SQLiteStorageProvider
from openwpm.task_manager import TaskManager
from openwpm.utilities.platform_utils import get_version

from dv_tracking.constants import CONDITIONS, GEOLOCATIONS, TRACKER_LIST_SNAPSHOTS, Condition
from dv_tracking.crawl.consent_interaction import (
    AutoconsentInteractor,
    ConsentAction,
    ConsentInteractionResult,
    ConsentInteractor,
    ScriptRunner,
)
from dv_tracking.crawl.resume import (
    attempt_counts,
    build_work_list,
    done_work_items,
    load_or_create_manifest,
    read_base_provenance,
    write_manifest_atomically,
)
from dv_tracking.egress import (
    ExitCheck,
    ProxyConfigurationError,
    RelayAddress,
    check_exit,
    load_env_file,
    utcnow_iso,
)
from dv_tracking.sites import DVSupportSite

INSTRUMENT: str = "openwpm"
SCHEMA_VERSION: str = "1"

# The interactor this cell drives for consent detection/decision. The one
# construction below is this module's sole concrete-engine dependency; every
# other reference to it, here and in ConsentInteractionCommand, is against
# the ConsentInteractor interface, so a different engine means replacing
# this one line, not touching the command. SUPPORTED_CONDITION_ACTIONS is
# derived from it, not hand-kept in sync, so widening ACTIVE_CONDITIONS
# beyond what the interactor supports is caught by
# _unsupported_condition_actions rather than assumed away.
CONSENT_INTERACTOR: ConsentInteractor = AutoconsentInteractor()
SUPPORTED_CONDITION_ACTIONS: frozenset[str] = CONSENT_INTERACTOR.supported_actions
ACTIVE_CONDITIONS = CONDITIONS
REPLICATE_COUNT: int = 1
# Hard bound on one visit's consent-interaction call; see
# consent_interaction.ConsentInteractor.run. A banner that never resolves
# ends the call at this bound, recorded as decision_status="timeout",
# rather than hanging the visit.
CONSENT_INTERACTION_TIMEOUT_S: float = 20.0
CONSENT_INTERACTION_TIMEOUT_MARGIN_S: int = 10

# xvfb, not headless: headless has no WebGL (a tracker signal and a bot
# signal) and its computed style/layout differ, which is what Consent-O-
# Matic's visibility matching and the candidate inventory will depend on. A
# headless calibration pass could not be compared against an xvfb sweep.
DISPLAY_MODE = "xvfb"

NAVIGATION_TIMEOUT_S: int = 90
DWELL_TIMEOUT_MARGIN_S: int = 10
FINALIZE_COMMAND_NAME: str = FinalizeCommand.__name__
# FinalizeCommand's own sleep, hardcoded at command_sequence.py:200 of the
# pinned commit (`FinalizeCommand(sleep=5)`) rather than exposed as an
# importable constant. The tab is already torn down by the time this sleep
# starts, but storage keeps attributing records to this visit_id for its
# duration -- see the module note on FINALIZE_DRAIN_TAIL_S's use below.
FINALIZE_DRAIN_TAIL_S: float = 5.0

# Bounds consecutive command failures before OpenWPM gives up on the whole
# invocation. The default (2*num_browsers+10 = 12 here) is tuned for a 100k-
# site Tranco crawl; on a ~250-site corpus with a handful of dead sites it
# is a methodological choice, not an inherited one.
FAILURE_LIMIT: int = 25

CONTENT_STORE_DIRNAME: str = "content-store.leveldb"
# main_frame is the page HTML the probe must classify. sub_frame is
# included because consent platforms commonly render their banner in an
# iframe; omitting it would systematically miss exactly what the probe
# needs to detect. script captures the CMP's own code and xmlhttprequest
# the configuration it fetches after load. Images, media, fonts and
# stylesheets are excluded to keep the store tractable at full-sweep
# scale; a calibration pass can widen this set with the flag below.
DEFAULT_SAVE_CONTENT_TYPES: str = "main_frame,sub_frame,script,xmlhttprequest"

DEFAULT_EXIT_RECHECK_SECONDS: int = 300
# A recheck cadence equal to the relay session lifetime sits exactly on the
# boundary the guard exists to police: a rotation landing just after a
# check would go unobserved for a full session. The margin forces a real
# gap, not just a non-negative one.
EXIT_RECHECK_MARGIN_S: int = 30

# Nothing upstream touches these (configure_firefox.privacy() only ever sets
# network.cookie.cookieBehavior); they must be pinned through
# BrowserParams.prefs directly or Firefox's own defaults govern them.
PROTECTION_PREFS: dict[str, bool] = {
    "privacy.trackingprotection.enabled": False,
    "privacy.trackingprotection.cryptomining.enabled": False,
    "privacy.trackingprotection.fingerprinting.enabled": False,
}

# Closes the two paths that leave the tunnel without going through
# network.proxy.* at all: DNS-over-HTTPS and WebRTC's ICE negotiation.
# See proxy_prefs() for why network.proxy.socks_remote_dns is not here too.
LEAK_CLOSURE_PREFS: dict[str, bool | int] = {
    "network.trr.mode": 5,
    "media.peerconnection.enabled": False,
}
# configure_firefox.privacy() maps tp_cookies="always" to
# network.cookie.cookieBehavior=0 (accept all, no partitioning): dFPI/Total
# Cookie Protection never engages. This is BrowserParams' own default, so
# nothing needs to set it, only confirm it has not drifted.
PINNED_TP_COOKIES: str = "always"


class ExitCode(IntEnum):
    """Process exit codes `main` returns.

    OK, NO_SITES_MATCHED, RELAY_OR_EXIT_GATE_FAILED,
    PREFS_NOT_PINNED, FAILURE_BUDGET_EXCEEDED, CELL_LOCKED, and
    EXIT_RECHECK_EXCEEDS_LIFETIME keep their historical values (0-6) from
    before this type existed. UNSUPPORTED_CONDITION and UNEXPECTED_FAILURE
    are new: the first is a refusal raised before any browser opens, and the
    second is `main`'s catch-all, so an unanticipated fault gets its own
    distinct, retryable code instead of colliding with NO_SITES_MATCHED on
    a bare traceback's exit(1).
    """

    OK = 0
    NO_SITES_MATCHED = 1
    RELAY_OR_EXIT_GATE_FAILED = 2
    PREFS_NOT_PINNED = 3
    FAILURE_BUDGET_EXCEEDED = 4
    CELL_LOCKED = 5
    EXIT_RECHECK_EXCEEDS_LIFETIME = 6
    UNSUPPORTED_CONDITION = 7
    UNEXPECTED_FAILURE = 8


class ExitVerificationError(Exception):
    """Raised when the pre-crawl exit gate could not confirm a passing exit."""

    def __init__(self, message: str, check: ExitCheck | None = None) -> None:
        super().__init__(message)
        self.check = check


class PrefsNotPinnedError(Exception):
    """Base for a refusal to crawl because a pinned browser configuration is missing."""


class ProxyPrefsNotPinnedError(PrefsNotPinnedError):
    """Raised when BrowserParams.prefs does not pin a manual proxy at the verified relay."""


class ProtectionPrefsNotPinnedError(PrefsNotPinnedError):
    """Raised when a browser protection this study requires pinned off is not configured that way."""


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


def _parse_save_content_types(value: str) -> str:
    """
    Validate a comma-separated save_content resource-type list against ALL_RESOURCE_TYPES.

    Runs as an argparse `type`, so it is applied during argument parsing --
    before any browser opens -- rather than letting OpenWPM's own
    ConfigError surface mid-crawl once the TaskManager is already up.

    Args:
        value: The raw --save-content-types argument.
    Returns:
        canonical: The requested types, stripped and deduplicated but in
            the given order, as a comma-separated string. OpenWPM's own
            split on `BrowserParams.save_content` does not strip
            whitespace, so returning the input unchanged would let a value
            like "main_frame, script" pass this check on its stripped form
            and then fail OpenWPM's own validation on the unstripped one.
    Raises:
        argparse.ArgumentTypeError: Naming the type(s) not in ALL_RESOURCE_TYPES.
    """
    requested = list(dict.fromkeys(t.strip() for t in value.split(",") if t.strip()))
    unknown = sorted(set(requested) - ALL_RESOURCE_TYPES)
    if unknown:
        raise argparse.ArgumentTypeError(
            f"unrecognised save_content resource type(s) {unknown}; valid types are "
            f"{sorted(ALL_RESOURCE_TYPES)}"
        )
    return ",".join(requested)


def _required_env(name: str) -> str:
    """Read a required environment variable, failing closed rather than defaulting."""
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is required and is not set")
    return value


def _normalize_version(value: str | bytes) -> str:
    """Decode get_version()'s bytes return (one of its two internal paths does not)."""
    return value.decode("utf-8") if isinstance(value, bytes) else value


def _unsupported_condition_actions(conditions: tuple[Condition, ...]) -> list[str]:
    """Return the distinct unsupported action names among the given conditions, sorted."""
    return sorted({c.action for c in conditions} - SUPPORTED_CONDITION_ACTIONS)


def _recheck_margin_is_sufficient(exit_recheck_seconds: int, iproyal_lifetime_s: int) -> bool:
    """Report whether the recheck cadence leaves EXIT_RECHECK_MARGIN_S before a session could rotate unnoticed."""
    return exit_recheck_seconds <= iproyal_lifetime_s - EXIT_RECHECK_MARGIN_S


def _stamped_at_utc(visits_path: Path, visit_id: int) -> str | None:
    """
    Look up one visit_id's own stamped_at_utc from the sidecar identity log.

    Scans from the end: the matching row is always the most recently
    appended one, written immediately before the get this lookup follows.

    Args:
        visits_path: The sidecar visit-identity JSONL path.
        visit_id: The visit_id to find.
    Returns:
        stamped_at_utc: The stamp instant, or None if no row matched.
    """
    if not visits_path.is_file():
        return None
    for line in reversed(visits_path.read_text(encoding="utf-8").splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("visit_id") == visit_id:
            return row.get("stamped_at_utc")
    return None


def _exit_check_row(check: ExitCheck, run_id: str) -> dict[str, object]:
    """Project an ExitCheck to the full row shape written to exit-checks.jsonl.

    The sole owner of that row shape: nothing downstream patches fields
    onto `check.as_provenance()` afterward.
    """
    return {
        "run_id": run_id,
        "expected_geolocation": check.expected_geolocation,
        "ip": check.observed.ip,
        "country": check.observed.country,
        "org": check.observed.org,
        "exit_class": check.observed.exit_class,
        "checked_at_utc": check.checked_at_utc,
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


def proxy_prefs(relay: RelayAddress) -> dict[str, str | int]:
    """
    Build the manual-proxy prefs a verified relay must be pinned at.

    The sole source for these five keys: `_crawl_cell` writes them into
    `BrowserParams.prefs` and `assert_prefs_pinned` checks against this same
    mapping, so the two cannot drift apart by one of them changing alone.

    `network.proxy.socks_remote_dns` is deliberately absent. It governs DNS
    resolution only when `network.proxy.socks` is configured, which this
    relay never is (HTTP/HTTPS only, the two keys above). An HTTP proxy
    never resolves the target hostname locally in the first place -- the
    browser sends it an absolute-form request line for plain HTTP and a
    CONNECT host:port for HTTPS -- so pinning a SOCKS-only pref here would
    assert a protection this proxy type does not need. `assert_prefs_pinned`
    refuses outright if `network.proxy.socks` ever appears, rather than
    leaving that as an assumption this function's absence only narrates.

    Args:
        relay: The relay address the exit gate just verified.
    Returns:
        prefs: The network.proxy.* keys a manual HTTP/HTTPS proxy needs pinned at that relay.
    """
    return {
        "network.proxy.type": 1,
        "network.proxy.http": relay.host,
        "network.proxy.http_port": relay.port,
        "network.proxy.ssl": relay.host,
        "network.proxy.ssl_port": relay.port,
    }


def assert_prefs_pinned(browser_params: BrowserParams, relay: RelayAddress) -> None:
    """
    Assert the browser is configured so its own defaults cannot distort this study.

    Proves configuration, not behaviour: nothing today reads the exit IP
    from inside a page load the way the gate itself does from outside, so a
    pref pinned here is not proof the browser's own traffic took this path.
    Proving that is a later PR.

    Args:
        browser_params: The browser's parameter object (prefs and tp_cookies).
        relay: The relay address the gate just verified.
    Raises:
        ProxyPrefsNotPinnedError: When prefs do not pin a manual proxy at the relay.
        ProtectionPrefsNotPinnedError: When a browser protection that would
            block or partition exactly what this study counts, or a leak
            path around the proxy, is not pinned off.
    """
    prefs = browser_params.prefs
    if "network.proxy.socks" in prefs:
        raise ProxyPrefsNotPinnedError(
            "prefs configure network.proxy.socks; this relay is HTTP/HTTPS only and "
            "network.proxy.socks_remote_dns is not pinned to cover a SOCKS proxy"
        )
    expected_proxy = proxy_prefs(relay)
    proxy_mismatches = {k: (prefs.get(k), v) for k, v in expected_proxy.items() if prefs.get(k) != v}
    if proxy_mismatches:
        raise ProxyPrefsNotPinnedError(
            f"prefs do not pin a manual proxy at {relay.host}:{relay.port}: {proxy_mismatches}"
        )

    expected_protections = PROTECTION_PREFS | LEAK_CLOSURE_PREFS
    protection_mismatches: dict[str, tuple[object, object]] = {
        k: (prefs.get(k), v) for k, v in expected_protections.items() if prefs.get(k) != v
    }
    if browser_params.tp_cookies != PINNED_TP_COOKIES:
        protection_mismatches["tp_cookies"] = (browser_params.tp_cookies, PINNED_TP_COOKIES)
    if protection_mismatches:
        raise ProtectionPrefsNotPinnedError(
            f"browser protections are not pinned off for measurement: {protection_mismatches}"
        )


def pref_fingerprint(prefs: Mapping[str, object], tp_cookies: str) -> str:
    """
    Hash the pinned prefs and tp_cookies into a short, stable fingerprint.

    Keys are sorted before hashing, so two cells pinned to the same values
    in a different insertion order still compare equal. Callers pass the
    subset of `BrowserParams.prefs` expected to be invariant across cells --
    `PROTECTION_PREFS | LEAK_CLOSURE_PREFS`, not the full dict. The proxy
    target (`proxy_prefs(relay)`) varies by design between the UK and US
    arms, each pointed at a different relay; folding it in would make every
    cell's fingerprint differ even when the protections are identical,
    defeating the cross-cell comparison this exists for. The full dict,
    proxy target included, is recorded separately as `pinned_prefs`.

    Args:
        prefs: The subset of pinned prefs expected to be invariant across cells.
        tp_cookies: The pinned `tp_cookies` value.
    Returns:
        fingerprint: A hex SHA-256 digest of the canonical JSON encoding.
    """
    canonical = json.dumps({"prefs": prefs, "tp_cookies": tp_cookies}, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class StampVisitCommand(BaseCommand):
    """Append the (site, condition, replicate) identity for this visit_id.

    Placed first in the sequence, before the get: BrowserManager breaks out
    of a sequence on any non-ok command and never reaches later ones, so a
    stamp placed after the get would leave a failed visit with no identity
    row, invisible to done_work_items, re-crawled forever.
    """

    def __init__(
        self,
        *,
        visits_path: Path,
        site_id: str,
        condition: str,
        replicate: int,
        arm_position: int,
        work_list_position: int,
        run_id: str,
        checked_at_utc: str,
    ) -> None:
        self.visits_path = visits_path
        self.site_id = site_id
        self.condition = condition
        self.replicate = replicate
        self.arm_position = arm_position
        self.work_list_position = work_list_position
        self.run_id = run_id
        self.checked_at_utc = checked_at_utc

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
                "work_list_position": self.work_list_position,
                "run_id": self.run_id,
                "checked_at_utc": self.checked_at_utc,
                "stamped_at_utc": utcnow_iso(),
            },
        )


class ConsentInteractionCommand(BaseCommand):
    """Detect a consent banner and, for an active consent arm, apply its decision.

    Every condition runs `interactor.run()` at the same point in the
    sequence -- `action="none"` detects and never decides -- so the three
    arms' pre-consent phases stay comparable; only the decision differs.
    Folds in the old `BoundaryCommand`'s role: `boundary_utc` is the
    engine-reported instant of a successful decision when there is one,
    and this command's own timestamp -- a synthetic marker at the
    equivalent point in the sequence -- otherwise (no decision requested,
    no banner, or a failed/timed-out attempt). `GetCommand`'s own alert
    probe (up to 0.5s, plus a further 1s when a modal is present) sits
    between the pre-dwell ending and this command running, so the offset
    from the preceding stamp is recorded rather than assumed fixed.
    """

    def __init__(
        self,
        *,
        interactor: ConsentInteractor,
        action: ConsentAction,
        timeout_s: float,
        boundaries_path: Path,
        interactions_path: Path,
        visits_path: Path,
    ) -> None:
        self.interactor = interactor
        self.action = action
        self.timeout_s = timeout_s
        self.boundaries_path = boundaries_path
        self.interactions_path = interactions_path
        self.visits_path = visits_path

    def __repr__(self) -> str:
        return f"ConsentInteractionCommand({self.action})"

    def execute(self, webdriver: ScriptRunner, browser_params, manager_params, extension_socket) -> None:
        result: ConsentInteractionResult = self.interactor.run(webdriver, self.action, self.timeout_s)
        boundary_utc = result.clicked_at_utc or utcnow_iso()
        stamped_at = _stamped_at_utc(self.visits_path, self.visit_id)
        offset_from_get_start_s = (
            (datetime.fromisoformat(boundary_utc) - datetime.fromisoformat(stamped_at)).total_seconds()
            if stamped_at is not None
            else None
        )
        _append_jsonl(
            self.boundaries_path,
            {
                "visit_id": self.visit_id,
                "boundary_utc": boundary_utc,
                "offset_from_get_start_s": offset_from_get_start_s,
                "boundary_source": "engine_reported" if result.clicked_at_utc else "synthetic",
            },
        )
        _append_jsonl(self.interactions_path, {"visit_id": self.visit_id, **result.to_json()})


class DwellCommand(BaseCommand):
    """Sleep without navigating, for the post-boundary dwell window."""

    def __init__(self, *, seconds: float) -> None:
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
        type=_parse_duration_seconds,
        default=os.environ.get("DV_EXIT_RECHECK_SECONDS", str(DEFAULT_EXIT_RECHECK_SECONDS)),
    )
    # Same duration format as --exit-recheck-seconds, deliberately: the two
    # are compared directly, and a bare-int side next to a "10m"-style side
    # was the kind of mismatch that let them drift apart silently.
    parser.add_argument(
        "--iproyal-lifetime", type=_parse_duration_seconds, default=os.environ.get("IPROYAL_LIFETIME", "10m")
    )
    parser.add_argument("--purpose", choices=("calibration", "sweep"), default="sweep")
    parser.add_argument(
        "--save-content-types",
        type=_parse_save_content_types,
        default=os.environ.get("DV_SAVE_CONTENT_TYPES", DEFAULT_SAVE_CONTENT_TYPES),
    )
    return parser.parse_args(argv)


def _crawl_cell(arguments: argparse.Namespace, cell_dir: Path) -> ExitCode:
    """Run one cell's crawl once the cell lock is held; see main() for the exit-code contract."""
    geolocation = arguments.geolocation
    db_path = cell_dir / "crawl-data.sqlite"
    content_store_path = cell_dir / CONTENT_STORE_DIRNAME
    manifest_path = cell_dir / "manifest.json"
    visits_path = cell_dir / "visits.jsonl"
    boundaries_path = cell_dir / "boundaries.jsonl"
    interactions_path = cell_dir / "interactions.jsonl"
    exit_checks_path = cell_dir / "exit-checks.jsonl"
    runs_path = cell_dir / "runs.jsonl"
    run_id = secrets.token_hex(8)

    def _finish(exit_code: ExitCode, reason: str, remaining_count: int | None = None) -> ExitCode:
        """Append this invocation's terminal runs.jsonl row and return its exit code."""
        _append_jsonl(
            runs_path,
            {
                "run_id": run_id,
                "ended_at_utc": utcnow_iso(),
                "exit_code": int(exit_code),
                "reason": reason,
                "remaining_count": remaining_count,
            },
        )
        return exit_code

    def _run() -> ExitCode:
        """Run the cell's crawl body; wrapped by _crawl_cell so any uncaught exception still ends with a terminal runs.jsonl row."""

        all_sites = DVSupportSite.load_all(arguments.sites)
        sites = [s for s in all_sites if s.country.strip().lower() == geolocation]
        print(f"[{geolocation}] {len(sites)} of {len(all_sites)} sites match the geolocation")
        if not sites:
            print(f"[{geolocation}] no matching sites; refusing rather than returning a silent 0")
            return _finish(ExitCode.NO_SITES_MATCHED, "no sites matched this geolocation")

        # Validated before anything opens a browser: build_work_list interleaves
        # conditions per site, so discovering an unsupported one mid-crawl would
        # leave a partially populated cell and a runs.jsonl row claiming every
        # active condition ran.
        unsupported_actions = _unsupported_condition_actions(ACTIVE_CONDITIONS)
        if unsupported_actions:
            reason = (
                f"unsupported condition action(s), not in {CONSENT_INTERACTOR.name}'s "
                f"supported_actions: {unsupported_actions}"
            )
            print(f"[{geolocation}] ERROR: {reason}")
            return _finish(ExitCode.UNSUPPORTED_CONDITION, reason)

        manifest = load_or_create_manifest(manifest_path, geolocation, INSTRUMENT, SCHEMA_VERSION)
        seed = manifest.ordering_seed

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

        # Cheapest deterministic checks first: resolving the relay address and
        # asserting prefs needs no network I/O, unlike the exit gate below it,
        # so a misconfiguration is caught before spending a metered proxy read.
        try:
            relay = RelayAddress.from_environ(os.environ)
        except ProxyConfigurationError as error:
            print(f"ERROR: {error}")
            print("Do NOT run the crawl.")
            return _finish(ExitCode.RELAY_OR_EXIT_GATE_FAILED, str(error), len(remaining))

        browser_params = BrowserParams(display_mode=DISPLAY_MODE)
        browser_params.http_instrument = True
        browser_params.cookie_instrument = True
        browser_params.navigation_instrument = True
        # Inert rather than wrong: behind a manual HTTP proxy Firefox hands
        # hostnames to the proxy instead of resolving them locally, so
        # dns_responses would be near-empty while provenance implied DNS was
        # measured.
        browser_params.dns_instrument = False
        browser_params.save_content = arguments.save_content_types
        # The one write site for these three dicts; assert_prefs_pinned reads
        # proxy_prefs(relay) and the same PROTECTION_PREFS/LEAK_CLOSURE_PREFS
        # constants back, so this and the gate cannot drift apart silently.
        browser_params.prefs = proxy_prefs(relay) | PROTECTION_PREFS | LEAK_CLOSURE_PREFS

        try:
            assert_prefs_pinned(browser_params, relay)
        except PrefsNotPinnedError as error:
            manifest = manifest.refused_with(str(error), utcnow_iso())
            write_manifest_atomically(manifest_path, manifest)
            print(f"ERROR: {error}")
            print("Refusing to crawl: browser configuration is not pinned as required.")
            return _finish(ExitCode.PREFS_NOT_PINNED, str(error), len(remaining))

        # The gate just passed, so any refusal recorded by an earlier invocation
        # no longer describes this cell.
        if manifest.refused:
            manifest = manifest.cleared_of_refusal()
            write_manifest_atomically(manifest_path, manifest)

        if not _recheck_margin_is_sufficient(arguments.exit_recheck_seconds, arguments.iproyal_lifetime):
            reason = (
                f"--exit-recheck-seconds ({arguments.exit_recheck_seconds}) leaves less than "
                f"{EXIT_RECHECK_MARGIN_S}s of margin before --iproyal-lifetime "
                f"({arguments.iproyal_lifetime}s); a rotation could pass unverified. This value is "
                "read from this container's own argument/environment, not queried from the relay "
                "itself, so it is only as accurate as the two were kept in sync."
            )
            print(f"ERROR: {reason}")
            return _finish(ExitCode.EXIT_RECHECK_EXCEEDS_LIFETIME, reason, len(remaining))

        openwpm_version, firefox_version = (_normalize_version(v) for v in get_version())
        base_provenance = read_base_provenance(arguments.data_root / "openwpm-base-provenance.txt")
        invariant_prefs = PROTECTION_PREFS | LEAK_CLOSURE_PREFS
        _append_jsonl(
            runs_path,
            {
                "run_id": run_id,
                "started_at_utc": utcnow_iso(),
                "crawl_date": arguments.crawl_date,
                "repo_commit": _required_env("DV_REPO_COMMIT"),
                "openwpm_tag": base_provenance.get("openwpm_tag"),
                "openwpm_commit": base_provenance.get("openwpm_commit"),
                "openwpm_version_string": openwpm_version,
                "base_image": base_provenance.get("image"),
                "base_image_id": base_provenance.get("image_id"),
                "firefox_version": firefox_version,
                "pref_fingerprint": pref_fingerprint(invariant_prefs, browser_params.tp_cookies),
                "pinned_prefs": browser_params.prefs,
                "tp_cookies": browser_params.tp_cookies,
                "instruments": {
                    "http_instrument": browser_params.http_instrument,
                    "cookie_instrument": browser_params.cookie_instrument,
                    "navigation_instrument": browser_params.navigation_instrument,
                    "dns_instrument": browser_params.dns_instrument,
                },
                "save_content_types": browser_params.save_content,
                "content_store_path": str(content_store_path),
                "consent_interactor": {
                    "name": CONSENT_INTERACTOR.name,
                    "version": CONSENT_INTERACTOR.version,
                    "supported_actions": sorted(CONSENT_INTERACTOR.supported_actions),
                },
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
                "finalize_drain_tail_s": FINALIZE_DRAIN_TAIL_S,
                "exit_recheck_seconds": arguments.exit_recheck_seconds,
                "iproyal_lifetime_s": arguments.iproyal_lifetime,
                "failure_limit": FAILURE_LIMIT,
                "sites_path": str(arguments.sites),
                "sites_sha256": hashlib.sha256(arguments.sites.read_bytes()).hexdigest(),
                "site_count": len(sites),
                "purpose": arguments.purpose,
            },
        )

        if not remaining:
            return _finish(ExitCode.OK, "nothing remaining", 0)

        try:
            check = verify_exit(geolocation, relay)
        except ExitVerificationError as error:
            if error.check is not None:
                _append_jsonl(exit_checks_path, _exit_check_row(error.check, run_id))
            print(f"ERROR: {error}")
            print("Do NOT run the crawl.")
            return _finish(ExitCode.RELAY_OR_EXIT_GATE_FAILED, str(error), len(remaining))
        _append_jsonl(exit_checks_path, _exit_check_row(check, run_id))
        checked_at_utc = check.checked_at_utc
        last_checked_at = time.monotonic()

        manager_params = ManagerParams(num_browsers=1)
        manager_params.data_directory = cell_dir
        manager_params.log_path = cell_dir / "openwpm.log"
        manager_params.failure_limit = FAILURE_LIMIT

        attempts = attempt_counts(visits_path)
        try:
            with TaskManager(
                manager_params,
                [browser_params],
                SQLiteStorageProvider(db_path),
                LevelDbProvider(content_store_path),
            ) as manager:
                for index, item in enumerate(remaining):
                    if time.monotonic() - last_checked_at >= arguments.exit_recheck_seconds:
                        try:
                            check = verify_exit(geolocation, relay)
                        except ExitVerificationError as error:
                            if error.check is not None:
                                _append_jsonl(exit_checks_path, _exit_check_row(error.check, run_id))
                            print(f"ERROR: {error}")
                            print("Do NOT run the crawl. Aborting with visits remaining.")
                            return _finish(
                                ExitCode.RELAY_OR_EXIT_GATE_FAILED, str(error), len(remaining) - index
                            )
                        _append_jsonl(exit_checks_path, _exit_check_row(check, run_id))
                        checked_at_utc = check.checked_at_utc
                        last_checked_at = time.monotonic()

                    key = (item.site.site_id, item.condition.name, item.replicate)
                    sequence = CommandSequence(
                        item.site.url,
                        reset=True,
                        retry_number=attempts.get(key, 0) + 1,
                        callback=lambda ok, site_id=item.site.site_id: print(
                            f"[{geolocation}] {site_id}: {'ok' if ok else 'failed'}"
                        ),
                    )
                    sequence.append_command(
                        StampVisitCommand(
                            visits_path=visits_path,
                            site_id=item.site.site_id,
                            condition=item.condition.name,
                            replicate=item.replicate,
                            arm_position=item.arm_position,
                            work_list_position=item.position,
                            run_id=run_id,
                            checked_at_utc=checked_at_utc,
                        )
                    )
                    sequence.get(
                        sleep=item.condition.pre_dwell_s,
                        timeout=NAVIGATION_TIMEOUT_S + int(item.condition.pre_dwell_s),
                    )
                    sequence.append_command(
                        ConsentInteractionCommand(
                            interactor=CONSENT_INTERACTOR,
                            action=item.condition.action,
                            timeout_s=CONSENT_INTERACTION_TIMEOUT_S,
                            boundaries_path=boundaries_path,
                            interactions_path=interactions_path,
                            visits_path=visits_path,
                        ),
                        timeout=int(CONSENT_INTERACTION_TIMEOUT_S) + CONSENT_INTERACTION_TIMEOUT_MARGIN_S,
                    )
                    sequence.append_command(
                        DwellCommand(seconds=item.condition.post_dwell_s),
                        timeout=int(item.condition.post_dwell_s) + DWELL_TIMEOUT_MARGIN_S,
                    )
                    manager.execute_command_sequence(sequence)
        except CommandExecutionError as error:
            print(f"ERROR: crawl failure budget exceeded: {error}")
            return _finish(ExitCode.FAILURE_BUDGET_EXCEEDED, str(error))

        try:
            final_check = verify_exit(geolocation, relay)
            _append_jsonl(exit_checks_path, _exit_check_row(final_check, run_id))
        except ExitVerificationError as error:
            if error.check is not None:
                _append_jsonl(exit_checks_path, _exit_check_row(error.check, run_id))
            print(f"WARNING: final exit check did not pass: {error}")

        return _finish(ExitCode.OK, "completed", 0)

    try:
        return _run()
    except Exception as error:  # noqa: BLE001 -- see main()'s docstring: must retire with a traced runs.jsonl row, not a bare traceback.
        # UNEXPECTED_FAILURE is by definition the case nobody anticipated, so
        # the full trace goes to stdout; only the one-line reason fits in runs.jsonl.
        print(traceback.format_exc())
        return _finish(ExitCode.UNEXPECTED_FAILURE, f"unexpected failure: {error!r}")


def main(argv: list[str] | None = None) -> int:
    """
    Run the crawl over the corpus for one (crawl_date, geolocation) cell.

    Args:
        argv: The argument vector; defaults to sys.argv when None.
    Returns:
        exit_code: See `ExitCode` for what each value means.
            `ExitCode.UNEXPECTED_FAILURE` covers any exception not handled
            by a more specific path -- an OpenWPM `critical` command status
            re-raises the browser child's own arbitrary exception type, for
            instance -- so a crawl fault never resolves to the same code as
            `ExitCode.NO_SITES_MATCHED`, which signals something that a
            retry cannot fix.
    """
    load_env_file()
    arguments = parse_arguments(argv)
    cell_dir = (
        arguments.data_root / "raw" / f"{arguments.crawl_date}_{arguments.geolocation}" / "openwpm"
    )
    cell_dir.mkdir(parents=True, exist_ok=True)

    lock_handle = (cell_dir / ".lock").open("ab")
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(f"ERROR: another invocation holds the lock for this cell ({cell_dir / '.lock'})")
        return ExitCode.CELL_LOCKED
    try:
        # _crawl_cell catches everything internally so its own runs.jsonl end
        # row is always written; this is a backstop against a fault in that
        # catch itself, not the primary path.
        return _crawl_cell(arguments, cell_dir)
    except Exception as error:  # noqa: BLE001 -- see docstring: must retire distinctly, not as a bare traceback.
        print(traceback.format_exc())
        print(f"ERROR: unexpected failure: {error!r}")
        return ExitCode.UNEXPECTED_FAILURE
    finally:
        lock_handle.close()


if __name__ == "__main__":
    raise SystemExit(main())
