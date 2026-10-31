"""
Container-side CLI: classify every visit in one OpenWPM cell for blocking.

Invocation: `python -m dv_tracking.crawl.detect_blocks <cell_dir>`, where
`<cell_dir>` is the `.../<crawl_date>_<geolocation>/openwpm` directory
`dv_tracking.crawl.openwpm_run` writes. Reads `crawl-data.sqlite`,
`visits.jsonl`, and -- when present -- `content-store.leveldb` (needs
`plyvel`, hence this module is container-only; see
`dv_tracking.crawl.content_store`). Writes two sidecars into the same
directory, following the `visits.jsonl`/`boundaries.jsonl` pattern:

    block-verdicts.jsonl    one row per visit (dv_tracking.crawl.block_detection.VisitVerdict)
    exclusion-report.json   per-arm/per-site reason-code counts and the excluded site list

Both are plain JSON; the analysis stage reads them with `json` alone, with
no `plyvel` import required.

Idempotent per cell: each run overwrites both sidecars in full rather than
appending, since this is a derived classification pass over data that is
already final, not an append-only crawl log.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from dv_tracking.crawl.block_detection import (
    BLOCK_DETECTION_SCHEMA_VERSION,
    ExclusionReport,
    VisitEvidence,
    VisitVerdict,
    build_exclusion_report,
    fetch_visit_evidence,
)
from dv_tracking.crawl.content_store import read_main_document_html
from dv_tracking.crawl.resume import read_visit_identities

CONTENT_STORE_DIRNAME: str = "content-store.leveldb"
DB_FILENAME: str = "crawl-data.sqlite"
VISITS_FILENAME: str = "visits.jsonl"
VERDICTS_FILENAME: str = "block-verdicts.jsonl"
REPORT_FILENAME: str = "exclusion-report.json"


def build_verdicts(cell_dir: Path) -> list[VisitVerdict]:
    """
    Classify every visit in one cell, fetching HTML from the content store where it exists.

    A visit named in visits.jsonl with no matching sqlite evidence at all
    (the main_frame request itself never landed) is classified from an
    empty, all-zero VisitEvidence -- no status and no network signature
    fired, and there is no HTML to check, so it resolves to `not_assessable`
    like any other visit the body route could not reach.

    Args:
        cell_dir: The cell's openwpm directory.
    Returns:
        verdicts: One verdict per visit named in visits.jsonl, in visit_id order.
    """
    identities = read_visit_identities(cell_dir / VISITS_FILENAME)
    evidence_by_visit = fetch_visit_evidence(cell_dir / DB_FILENAME)
    content_store_path = cell_dir / CONTENT_STORE_DIRNAME

    verdicts = []
    for visit_id, (site_id, condition, replicate) in sorted(identities.items()):
        evidence = evidence_by_visit.get(visit_id) or VisitEvidence(
            visit_id=visit_id,
            statuses=(),
            content_hash=None,
            response_header_blobs=(),
            request_hosts=frozenset(),
            total_request_count=0,
            first_party_request_count=0,
        )
        html_text = None
        if evidence.content_hash is not None:
            html_text = read_main_document_html(content_store_path, evidence.content_hash)
        verdicts.append(
            VisitVerdict.build(
                site_id=site_id,
                condition=condition,
                replicate=replicate,
                evidence=evidence,
                html_text=html_text,
            )
        )
    return verdicts


def write_verdicts(cell_dir: Path, verdicts: list[VisitVerdict]) -> Path:
    """Write block-verdicts.jsonl into the cell directory, overwriting any prior run."""
    path = cell_dir / VERDICTS_FILENAME
    path.write_text("".join(json.dumps(v.to_json()) + "\n" for v in verdicts), encoding="utf-8")
    return path


def write_exclusion_report(cell_dir: Path, report: ExclusionReport) -> Path:
    """
    Write exclusion-report.json into the cell directory, overwriting any prior run.

    Stamps run-level provenance -- the block-detection schema version, the
    repo commit this pass ran at (from `DV_REPO_COMMIT`, the same env var
    `openwpm_run.py` requires; None outside a built image, e.g. local
    development against a bind-mounted source tree), and the instant this
    pass ran -- so a signature set added mid-study leaves a trace of which
    version classified a given cell, rather than a silent overwrite.

    Args:
        cell_dir: The cell's openwpm directory.
        report: The exclusion report to write.
    Returns:
        path: The written exclusion-report.json path.
    """
    payload: dict[str, object] = {
        "schema_version": BLOCK_DETECTION_SCHEMA_VERSION,
        "classified_at_utc": datetime.now(UTC).isoformat(),
        "repo_commit": os.environ.get("DV_REPO_COMMIT"),
        **report.to_json(),
    }
    path = cell_dir / REPORT_FILENAME
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def parse_arguments(argv: list[str] | None) -> argparse.Namespace:
    """
    Parse the dv-detect-blocks command-line arguments.

    Args:
        argv: The argument vector; defaults to sys.argv when None.
    Returns:
        arguments: The parsed argument namespace.
    """
    parser = argparse.ArgumentParser(
        description="Classify every visit in one OpenWPM cell for blocking, and write the exclusion report."
    )
    parser.add_argument("cell_dir", type=Path, help="The cell's openwpm directory.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """
    Classify one cell's visits and write the block-verdicts and exclusion-report sidecars.

    Args:
        argv: The argument vector; defaults to sys.argv when None.
    Returns:
        exit_code: 0 on success, 2 when the cell directory does not exist.
    """
    arguments = parse_arguments(argv)
    cell_dir = arguments.cell_dir
    if not cell_dir.is_dir():
        print(f"ERROR: cell directory not found: {cell_dir}")
        return 2

    verdicts = build_verdicts(cell_dir)
    report = build_exclusion_report(verdicts)
    verdicts_path = write_verdicts(cell_dir, verdicts)
    report_path = write_exclusion_report(cell_dir, report)

    print(f"{cell_dir}: {len(verdicts)} visits classified")
    for site_id, counts in report.per_site_counts.items():
        print(f"  {site_id}: {counts}")
    if report.excluded_sites:
        print("excluded sites:")
        for exclusion in report.excluded_sites:
            print(f"  {exclusion.site_id}: {exclusion.reason}")
    else:
        print("no sites excluded")
    print(f"wrote {verdicts_path}")
    print(f"wrote {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
