"""
The crawl's work list, its seeded counterbalancing, and the resume predicate.

Imports stdlib, `dv_tracking.constants`, and `dv_tracking.sites` only -- no
`openwpm`. `openwpm_run.py` is the only caller; splitting this out of it is
what lets the seed-reuse-on-resume behaviour, the logic most likely to be
silently wrong, run under pytest without an OpenWPM install.
"""

from __future__ import annotations

import itertools
import json
import os
import random
import secrets
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from dv_tracking.constants import Condition
from dv_tracking.sites import DVSupportSite


@dataclass(frozen=True)
class WorkItem:
    """One (site, condition, replicate) unit of crawl work, at a fixed work-list position."""

    site: DVSupportSite
    condition: Condition
    replicate: int
    arm_position: int
    position: int


def build_work_list(
    sites: list[DVSupportSite],
    seed: int,
    conditions: tuple[Condition, ...],
    replicate_count: int = 1,
) -> list[WorkItem]:
    """
    Build the (site, condition, replicate) work list in seeded, permuted-block order.

    Sites are shuffled by the seed. Arm order is assigned from one of
    `len(conditions)!` permuted blocks, indexed by each site's shuffled
    position modulo that count -- permuted blocks rather than a per-site
    random draw, so every order is realised an equal (or off-by-one) number
    of times instead of merely in expectation.

    Args:
        sites: The sites to crawl, already filtered to one geolocation.
        seed: The crawl's ordering seed.
        conditions: The active conditions to cross with every site.
        replicate_count: Replicates per (site, condition).
    Returns:
        work_list: One WorkItem per (site, condition, replicate), in run order.
    """
    ordered_sites = list(sites)
    random.Random(seed).shuffle(ordered_sites)
    blocks = list(itertools.permutations(conditions))
    work_list: list[WorkItem] = []
    position = 0
    for site_position, site in enumerate(ordered_sites):
        order = blocks[site_position % len(blocks)]
        for arm_position, condition in enumerate(order):
            for replicate in range(1, replicate_count + 1):
                work_list.append(WorkItem(site, condition, replicate, arm_position, position))
                position += 1
    return work_list


def _visit_identities(visits_jsonl_path: Path) -> dict[int, tuple[str, str, int]]:
    """
    Map visit_id to (site_id, condition, replicate) from the sidecar manifest.

    A line that fails to parse (a kill mid-write, bounded to the final line
    by the writer's flock) is skipped rather than raising.

    Args:
        visits_jsonl_path: The sidecar visit-identity JSONL path.
    Returns:
        identities: visit_id -> (site_id, condition, replicate), for every
            line that parsed.
    """
    identities: dict[int, tuple[str, str, int]] = {}
    if not visits_jsonl_path.is_file():
        return identities
    for line in visits_jsonl_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        identities[row["visit_id"]] = (row["site_id"], row["condition"], row["replicate"])
    return identities


def attempt_counts(visits_jsonl_path: Path) -> dict[tuple[str, str, int], int]:
    """
    Count prior visit_id rows per (site_id, condition, replicate).

    Args:
        visits_jsonl_path: The sidecar visit-identity JSONL path.
    Returns:
        counts: (site_id, condition, replicate) -> number of prior attempts.
    """
    counts: dict[tuple[str, str, int], int] = {}
    for key in _visit_identities(visits_jsonl_path).values():
        counts[key] = counts.get(key, 0) + 1
    return counts


def _terminal_outcomes(db_path: Path, finalize_command_name: str) -> dict[int, str]:
    """
    Reduce crawl_history to one terminal outcome per visit_id that reached one.

    A command failure (any status but "ok") makes BrowserManager call
    finalize_visit_id and break out of its sequence without running the
    rest, so the finalize command's own row exists only for a visit that
    ran clean; the last crawl_history row for any other visit is its
    failure instead. Connects read-only, since OpenWPM holds this file open
    for the life of the crawl.

    Args:
        db_path: The cell's crawl-data.sqlite path.
        finalize_command_name: The class name OpenWPM records for its
            auto-appended finalize command (pass `FinalizeCommand.__name__`,
            not a hardcoded string, so an upstream rename cannot silently
            reclassify every clean visit as a failure).
    Returns:
        outcomes: visit_id -> "completed" | "timeout" | "error", terminal visits only.
    """
    if not db_path.is_file() or db_path.stat().st_size == 0:
        return {}
    try:
        with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as connection:
            rows = connection.execute(
                "SELECT visit_id, command, command_status FROM crawl_history ORDER BY visit_id, rowid"
            ).fetchall()
    except sqlite3.OperationalError:
        # Not yet a real database, or the crawl_history table doesn't exist
        # yet (schema freshly created, nothing attempted).
        return {}
    last_row: dict[int, tuple[str, str]] = {}
    for visit_id, command, status in rows:
        last_row[visit_id] = (command, status)
    outcomes: dict[int, str] = {}
    for visit_id, (command, status) in last_row.items():
        if command == finalize_command_name and status == "ok":
            outcomes[visit_id] = "completed"
        elif status == "timeout":
            outcomes[visit_id] = "timeout"
        else:
            outcomes[visit_id] = "error"
    return outcomes


def done_work_items(
    db_path: Path, visits_jsonl_path: Path, finalize_command_name: str
) -> set[tuple[str, str, int]]:
    """
    Reconcile the cell's SQLite against the sidecar manifest for the resume predicate.

    The terminal outcome lives in crawl_history, OpenWPM's own primary
    artifact; the sidecar manifest only supplies the (site_id, condition,
    replicate) identity crawl_history has no notion of. Together they are
    the complete resume predicate -- no separate ledger file is kept.

    Args:
        db_path: The cell's crawl-data.sqlite path.
        visits_jsonl_path: The sidecar visit-identity JSONL path.
        finalize_command_name: Passed through to `_terminal_outcomes`.
    Returns:
        done: Every (site_id, condition, replicate) with a terminal outcome,
            successful or not -- a recorded failure is a result, not a retry.
    """
    identities = _visit_identities(visits_jsonl_path)
    outcomes = _terminal_outcomes(db_path, finalize_command_name)
    return {identities[visit_id] for visit_id in outcomes if visit_id in identities}


def read_base_provenance(path: Path) -> dict[str, str]:
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


def load_or_create_manifest(
    manifest_path: Path, geolocation: str, instrument: str, schema_version: str
) -> dict:
    """
    Load the cell manifest if an earlier invocation already wrote one, else create it.

    The seed lives here and must be reused on resume, not regenerated -- a
    new seed would make the counterbalancing unrecoverable after the fact.
    Written once; per-invocation facts (repo commit, versions, run
    parameters) live in `runs.jsonl` instead, since those can legitimately
    differ between a cell's invocations and this file is not meant to
    reopen for writing in the ordinary case.

    Args:
        manifest_path: The cell manifest JSON path.
        geolocation: The geolocation this cell crawls.
        instrument: The pinned instrument name for a freshly created manifest.
        schema_version: The pinned schema version for a freshly created manifest.
    Returns:
        manifest: The manifest dict, loaded or newly created.
    """
    if manifest_path.is_file():
        return json.loads(manifest_path.read_text(encoding="utf-8"))

    manifest = {
        "instrument": instrument,
        "schema_version": schema_version,
        "geolocation": geolocation,
        "ordering_seed": secrets.randbits(63),
        "created_at_utc": datetime.now(tz=UTC).isoformat(),
    }
    write_manifest_atomically(manifest_path, manifest)
    return manifest


def write_manifest_atomically(manifest_path: Path, manifest: dict) -> None:
    """
    Write the manifest via a temp file plus os.replace, never leaving a partial file.

    Args:
        manifest_path: The cell manifest JSON path.
        manifest: The manifest dict to write.
    """
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = manifest_path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp_path, manifest_path)


def write_refusal(manifest_path: Path, manifest: dict, reason: str, refused_at_utc: str) -> None:
    """
    Record a refusal to crawl synchronously and atomically in the cell manifest.

    Refusal is the one case this manifest reopens for after creation: a
    refused cell must stay distinguishable from one never attempted.

    Args:
        manifest_path: The cell manifest JSON path.
        manifest: The manifest dict loaded for this invocation.
        reason: Why the crawl refused to proceed.
        refused_at_utc: The refusal instant.
    """
    refused = dict(manifest)
    refused["refused"] = True
    refused["refusal_reason"] = reason
    refused["refused_at_utc"] = refused_at_utc
    write_manifest_atomically(manifest_path, refused)
