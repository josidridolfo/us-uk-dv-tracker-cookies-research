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
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Self, TypeAlias

from dv_tracking.constants import Condition
from dv_tracking.sites import DVSupportSite

# (site_id, condition name, replicate) -- the identity crawl_history has no
# notion of; appears at every join point between the sidecar manifest and
# OpenWPM's own tables.
VisitIdentity: TypeAlias = tuple[str, str, int]


@dataclass(frozen=True, kw_only=True)
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
                work_list.append(
                    WorkItem(
                        site=site,
                        condition=condition,
                        replicate=replicate,
                        arm_position=arm_position,
                        position=position,
                    )
                )
                position += 1
    return work_list


def _visit_identities(visits_jsonl_path: Path) -> dict[int, VisitIdentity]:
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
    identities: dict[int, VisitIdentity] = {}
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


def attempt_counts(visits_jsonl_path: Path) -> dict[VisitIdentity, int]:
    """
    Count prior visit_id rows per (site_id, condition, replicate).

    Args:
        visits_jsonl_path: The sidecar visit-identity JSONL path.
    Returns:
        counts: (site_id, condition, replicate) -> number of prior attempts.
    """
    counts: dict[VisitIdentity, int] = {}
    for key in _visit_identities(visits_jsonl_path).values():
        counts[key] = counts.get(key, 0) + 1
    return counts


@dataclass(frozen=True)
class VisitOutcome:
    """A visit_id's terminal status and whether storage itself marked it incomplete.

    `status` is "completed" for a clean finalize, otherwise the literal
    `command_status` of the row that ended the visit ("timeout", "critical",
    "error", or "neterror") -- never collapsed, since a DNS failure and an
    instrument crash need to stay distinguishable downstream. `incomplete`
    is independent: it is True whenever OpenWPM's own storage layer wrote
    this visit_id to `incomplete_visits`, which happens for every
    non-"completed" status and marks data that may be partial or empty.
    """

    status: str
    incomplete: bool


def _terminal_outcomes(db_path: Path, finalize_command_name: str) -> dict[int, VisitOutcome]:
    """
    Reduce crawl_history to one terminal outcome per visit_id that reached one.

    Terminal is `(last row is the finalize command, status "ok")` or
    `(last row status is anything but "ok")` -- OpenWPM's BrowserManager
    only ever breaks out of a live sequence after writing a non-ok row, so
    with the upstream default `memory_watchdog=False` those are the only two
    ways a visit ends while the process is alive. A visit stuck on a last
    row that is "ok" but not the finalize command (e.g. only Initialize and
    Get, the finalize never written) is not terminal: that is what process
    death mid-sequence looks like, and it must stay invisible to the done
    set so it gets re-crawled.

    Connects read-only, since OpenWPM holds this file open for the life of
    the crawl; only the presence of `crawl_history` is probed defensively
    (the schema may not exist yet on a freshly created database). Every
    other sqlite3 error -- a locked database, a hot journal that `mode=ro`
    cannot roll back, a file that is not a database at all -- propagates
    rather than reading as "nothing done", since resuming on that silence
    would re-crawl a cell that is actually mid-journal-replay.

    Args:
        db_path: The cell's crawl-data.sqlite path.
        finalize_command_name: The class name OpenWPM records for its
            auto-appended finalize command (pass `FinalizeCommand.__name__`,
            not a hardcoded string, so an upstream rename cannot silently
            reclassify every clean visit as a failure).
    Returns:
        outcomes: visit_id -> VisitOutcome, terminal visits only.
    """
    if not db_path.is_file() or db_path.stat().st_size == 0:
        return {}
    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as connection:
        has_crawl_history = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'crawl_history'"
        ).fetchone()
        if has_crawl_history is None:
            return {}
        rows = connection.execute(
            "SELECT visit_id, command, command_status FROM crawl_history ORDER BY visit_id, rowid"
        ).fetchall()
        has_incomplete_visits = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'incomplete_visits'"
        ).fetchone()
        incomplete_visit_ids: set[int] = set()
        if has_incomplete_visits is not None:
            incomplete_visit_ids = {
                row[0] for row in connection.execute("SELECT visit_id FROM incomplete_visits")
            }

    last_row: dict[int, tuple[str, str]] = {}
    for visit_id, command, status in rows:
        last_row[visit_id] = (command, status)

    outcomes: dict[int, VisitOutcome] = {}
    for visit_id, (command, status) in last_row.items():
        is_clean_finalize = command == finalize_command_name and status == "ok"
        if not is_clean_finalize and status == "ok":
            continue  # non-terminal: process died before the finalize command ran
        label = "completed" if is_clean_finalize else status
        outcomes[visit_id] = VisitOutcome(status=label, incomplete=visit_id in incomplete_visit_ids)
    return outcomes


def done_work_items(
    db_path: Path, visits_jsonl_path: Path, finalize_command_name: str
) -> dict[VisitIdentity, VisitOutcome]:
    """
    Reconcile the cell's SQLite against the sidecar manifest for the resume predicate.

    The terminal outcome lives in crawl_history, OpenWPM's own primary
    artifact; the sidecar manifest only supplies the (site_id, condition,
    replicate) identity crawl_history has no notion of. Together they are
    the complete resume predicate -- no separate ledger file is kept. When
    retries have left more than one visit_id for the same identity, the
    highest visit_id's outcome wins, since `rows` is ordered by visit_id and
    later entries overwrite earlier ones -- that is also the authoritative
    attempt an ingest stage should read.

    Args:
        db_path: The cell's crawl-data.sqlite path.
        visits_jsonl_path: The sidecar visit-identity JSONL path.
        finalize_command_name: Passed through to `_terminal_outcomes`.
    Returns:
        done: Every (site_id, condition, replicate) with a terminal outcome,
            successful or not -- a recorded failure is a result, not a
            retry -- mapped to that outcome so a caller can act on more than
            membership.
    """
    identities = _visit_identities(visits_jsonl_path)
    outcomes = _terminal_outcomes(db_path, finalize_command_name)
    done: dict[VisitIdentity, VisitOutcome] = {}
    for visit_id, outcome in outcomes.items():
        if visit_id in identities:
            done[identities[visit_id]] = outcome
    return done


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


@dataclass(frozen=True)
class CellManifest:
    """The cell's pinned identity: the ordering seed and the fields that must not drift.

    `instrument`, `schema_version`, and `geolocation` are fixed at creation
    and must match every later invocation exactly; a mismatch means this
    invocation is pointed at the wrong cell, or the schema moved underneath
    an existing one, and either way the seed it would otherwise reuse makes
    the counterbalancing unrecoverable if the mismatch goes unnoticed.
    """

    instrument: str
    schema_version: str
    geolocation: str
    ordering_seed: int
    created_at_utc: str
    refused: bool = False
    refusal_reason: str | None = None
    refused_at_utc: str | None = None

    @classmethod
    def from_json(
        cls, payload: dict[str, object], *, geolocation: str, instrument: str, schema_version: str
    ) -> Self:
        """
        Build a manifest from its on-disk JSON, refusing on drift in a pinned field.

        Args:
            payload: The manifest JSON, already parsed.
            geolocation: This invocation's geolocation; must match the stored value.
            instrument: This invocation's instrument name; must match the stored value.
            schema_version: This invocation's schema version; must match the stored value.
        Returns:
            manifest: The validated manifest.
        Raises:
            ValueError: When geolocation, instrument, or schema_version read
                back from disk differs from this invocation's own pinned values.
        """
        pinned = {
            "geolocation": geolocation,
            "instrument": instrument,
            "schema_version": schema_version,
        }
        mismatches = {
            field: (payload.get(field), expected)
            for field, expected in pinned.items()
            if payload.get(field) != expected
        }
        if mismatches:
            raise ValueError(
                f"manifest does not match this invocation's pinned fields: {mismatches}"
            )
        return cls(
            instrument=payload["instrument"],
            schema_version=payload["schema_version"],
            geolocation=payload["geolocation"],
            ordering_seed=payload["ordering_seed"],
            created_at_utc=payload["created_at_utc"],
            refused=payload.get("refused", False),
            refusal_reason=payload.get("refusal_reason"),
            refused_at_utc=payload.get("refused_at_utc"),
        )

    def to_json(self) -> dict[str, object]:
        """Project the manifest to the dict written to disk."""
        payload = {
            "instrument": self.instrument,
            "schema_version": self.schema_version,
            "geolocation": self.geolocation,
            "ordering_seed": self.ordering_seed,
            "created_at_utc": self.created_at_utc,
        }
        if self.refused:
            payload["refused"] = True
            payload["refusal_reason"] = self.refusal_reason
            payload["refused_at_utc"] = self.refused_at_utc
        return payload

    def refused_with(self, reason: str, at: str) -> Self:
        """Return a copy marked refused, keeping every other field."""
        return replace(self, refused=True, refusal_reason=reason, refused_at_utc=at)

    def cleared_of_refusal(self) -> Self:
        """Return a copy with any prior refusal cleared, keeping every other field.

        A refusal describes this invocation's own gate check, not the cell:
        once a later invocation passes that gate, the cell is no longer
        refused even though an earlier one was, and nothing else clears the
        flag on disk.
        """
        return replace(self, refused=False, refusal_reason=None, refused_at_utc=None)


def load_or_create_manifest(
    manifest_path: Path, geolocation: str, instrument: str, schema_version: str
) -> CellManifest:
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
        manifest: The manifest, loaded and validated, or newly created.
    Raises:
        ValueError: When an existing manifest's pinned fields do not match
            this invocation's geolocation, instrument, or schema_version.
    """
    if manifest_path.is_file():
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        return CellManifest.from_json(
            payload, geolocation=geolocation, instrument=instrument, schema_version=schema_version
        )

    manifest = CellManifest(
        instrument=instrument,
        schema_version=schema_version,
        geolocation=geolocation,
        ordering_seed=secrets.randbits(63),
        created_at_utc=datetime.now(tz=UTC).isoformat(),
    )
    write_manifest_atomically(manifest_path, manifest)
    return manifest


def write_manifest_atomically(manifest_path: Path, manifest: CellManifest) -> None:
    """
    Write the manifest via a temp file plus os.replace, never leaving a partial file.

    Args:
        manifest_path: The cell manifest JSON path.
        manifest: The manifest to write.
    """
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = manifest_path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(manifest.to_json(), indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp_path, manifest_path)
