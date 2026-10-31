"""
Container-side CLI: deduplicate one cell's content store into a sibling store.

Invocation: `python -m dv_tracking.crawl.compact_content_store <cell_dir>`,
the same `<cell_dir>` `dv_tracking.crawl.detect_blocks` takes. Reads
`crawl-data.sqlite` and `content-store.leveldb`; needs `plyvel`, hence
container-only, same as `dv_tracking.crawl.content_store`.

`http_responses.content_hash` is not the hash of a response body. It is
the sha256 of only the first chunk `filterResponseData` hands the browser
extension's `ondata` listener, per
`Extension/src/lib/response-body-listener.ts` at the pinned OpenWPM
commit: `resolveContentHash` is a promise resolver, so only the first
`ondata` call's digest is kept however many chunks follow, while
`responseBody` keeps accumulating every chunk underneath it. Chunk
boundaries are a function of network timing, not of the body, so two
fetches of the exact same bytes can register under two different
`content_hash` values, and `LevelDbProvider.store_blob` -- keyed on that
value, not on the bytes it is given -- then stores the same body twice.

This pass never opens `content-store.leveldb` for writing. It reads every
key OpenWPM wrote there, and for each one writes two entries into a new,
sibling directory, `content-store-dedup.leveldb`: the body under
`body:<its own real sha256>`, and `hash:<the original key>` mapping to
that digest (`dv_tracking.crawl.content_store_keys`). The crawl's own
output -- the only real capture this project holds -- is never replaced,
deleted, or opened in a mode that could mutate it; the sibling is what a
disk-saving pass is allowed to risk, not the original. Compaction is safe
to run again: writing the same key under the same value a second time is
a no-op, so a cell already compacted reports zero new keys.

The pass does not take the resulting store's correctness on faith. Before
it reports success, it checks that the set of distinct body digests
written is exactly the set of distinct digests the original bytes hash
to, and that every `content_hash` in `http_responses` -- not just every
key in the content store -- resolves, through the new store, to the exact
bytes it resolved to before. Either check failing raises rather than
writing `content-store-compaction.json`, so a cell with no sidecar is a
cell compaction has not vouched for, not one that silently has the wrong
bytes behind some key.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import plyvel

from dv_tracking.crawl.content_store import resolve_blob
from dv_tracking.crawl.content_store_keys import BODY_KEY_PREFIX, HASH_KEY_PREFIX

ORIGINAL_STORE_DIRNAME: str = "content-store.leveldb"
DEDUP_STORE_DIRNAME: str = "content-store-dedup.leveldb"
DB_FILENAME: str = "crawl-data.sqlite"
COMPACTION_LOG_FILENAME: str = "content-store-compaction.json"
COMPACTION_SCHEMA_VERSION: str = "1"

_BODY_KEY_PREFIX_BYTES: bytes = BODY_KEY_PREFIX.encode("ascii")
_HASH_KEY_PREFIX_BYTES: bytes = HASH_KEY_PREFIX.encode("ascii")


class LosslessnessViolatedError(Exception):
    """
    Raised when compaction's own verification finds a body the new store does not reproduce.

    The sibling store is left in place for inspection rather than removed
    automatically. Recovery is deleting `content-store-dedup.leveldb` and
    rerunning: compaction always rebuilds from the original, so a
    corrupted sibling is never the original store's problem.
    """


@dataclass(frozen=True)
class CompactionResult:
    """
    The figures one compaction run measured and verified.

    A pure function of the cell's own data: two calls against an
    unchanged cell produce an equal result, with no timestamp or other
    run-specific field folded in here -- `write_compaction_log` stamps
    those at the point of writing, the same split `block_detection.py`'s
    `ExclusionReport` and `detect_blocks.py`'s `write_exclusion_report`
    already keep.

    Attributes:
        blobs_before: Distinct keys in the original, flat-layout store --
            one per distinct `content_hash` OpenWPM ever wrote there.
        bytes_before: Sum of the stored blob sizes before dedup, counting
            a body once per key that held it.
        distinct_bodies: Distinct body digests after dedup -- the number
            of `body:` keys in the sibling store.
        bytes_after: Sum of the stored blob sizes after dedup, counting
            a body once regardless of how many keys referenced it.
        duplicates_removed: `blobs_before - distinct_bodies`.
        bytes_saved: `bytes_before - bytes_after`.
        responses_checked: Rows in `http_responses` carrying a non-null
            `content_hash`, the population the verification step covers.
        verified_ok: Of those rows, how many resolved through the sibling
            store to the exact bytes the original store held for the
            same `content_hash` -- including a row whose `content_hash`
            was never actually captured in either store, which counts as
            a match (`None` equals `None`) rather than as unverified.
        new_body_keys_written: `body:` keys this run actually wrote,
            as opposed to finding already present. Zero on a cell
            already compacted.
        new_hash_keys_written: `hash:` keys this run actually wrote.
        dedup_store_pre_existed: Whether the sibling directory was
            already there when this run started.
    """

    blobs_before: int
    bytes_before: int
    distinct_bodies: int
    bytes_after: int
    duplicates_removed: int
    bytes_saved: int
    responses_checked: int
    verified_ok: int
    new_body_keys_written: int
    new_hash_keys_written: int
    dedup_store_pre_existed: bool

    @property
    def nothing_to_do(self) -> bool:
        """Report whether this run found every key already in place."""
        return self.dedup_store_pre_existed and self.new_body_keys_written == 0 and self.new_hash_keys_written == 0

    def to_json(self) -> dict[str, object]:
        """Project the result to the dict `write_compaction_log` folds into the sidecar."""
        return {**asdict(self), "nothing_to_do": self.nothing_to_do}


def _put_if_absent(db: plyvel.DB, key: bytes, value: bytes) -> bool:
    """Write `value` under `key` unless already present; return whether it was newly written."""
    if db.get(key) is not None:
        return False
    db.put(key, value)
    return True


def _fetch_response_content_hashes(db_path: Path) -> list[str]:
    """Read every non-null `content_hash` from `http_responses`, including repeats."""
    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as con:
        cur = con.execute("select content_hash from http_responses where content_hash is not null")
        return [row[0] for row in cur.fetchall()]


def compact_cell(cell_dir: Path) -> CompactionResult | None:
    """
    Deduplicate one cell's content store into a sibling store, verifying losslessness first-hand.

    Args:
        cell_dir: The cell's openwpm directory.
    Returns:
        result: The measured and verified figures, or None when the cell
            has no content store to compact (save_content was never
            enabled, or captured nothing).
    Raises:
        LosslessnessViolatedError: When the distinct-digest sets before
            and after disagree, or any `http_responses.content_hash`
            resolves to different bytes through the new store than it did
            through the original. Neither the sidecar nor a return value
            is produced in that case.
    """
    original_path = cell_dir / ORIGINAL_STORE_DIRNAME
    if not original_path.is_dir():
        return None

    dedup_path = cell_dir / DEDUP_STORE_DIRNAME
    dedup_store_pre_existed = dedup_path.is_dir()

    original_db = plyvel.DB(str(original_path), create_if_missing=False, compression="snappy")
    dedup_db = plyvel.DB(str(dedup_path), create_if_missing=True, compression="snappy")
    try:
        blobs_before = 0
        bytes_before = 0
        distinct_digests_before: set[str] = set()
        new_body_keys_written = 0
        new_hash_keys_written = 0

        for key, blob in original_db.iterator():
            blobs_before += 1
            bytes_before += len(blob)
            digest = hashlib.sha256(blob).hexdigest()
            distinct_digests_before.add(digest)
            if _put_if_absent(dedup_db, _BODY_KEY_PREFIX_BYTES + digest.encode("ascii"), blob):
                new_body_keys_written += 1
            if _put_if_absent(dedup_db, _HASH_KEY_PREFIX_BYTES + key, digest.encode("ascii")):
                new_hash_keys_written += 1

        distinct_digests_after = {
            key[len(_BODY_KEY_PREFIX_BYTES) :].decode("ascii")
            for key, _ in dedup_db.iterator()
            if key.startswith(_BODY_KEY_PREFIX_BYTES)
        }
        if distinct_digests_after != distinct_digests_before:
            raise LosslessnessViolatedError(
                f"distinct body digests changed under compaction: "
                f"{len(distinct_digests_before)} before, {len(distinct_digests_after)} after"
            )
        bytes_after = sum(len(dedup_db.get(_BODY_KEY_PREFIX_BYTES + digest.encode("ascii"))) for digest in distinct_digests_after)

        response_hashes = _fetch_response_content_hashes(cell_dir / DB_FILENAME)
        verified_ok = 0
        for content_hash in response_hashes:
            before = original_db.get(content_hash.encode("ascii"))
            after = resolve_blob(dedup_db, content_hash)
            if after != before:
                raise LosslessnessViolatedError(
                    f"content_hash {content_hash!r} resolves to different bytes after compaction"
                )
            verified_ok += 1
    finally:
        original_db.close()
        dedup_db.close()

    return CompactionResult(
        blobs_before=blobs_before,
        bytes_before=bytes_before,
        distinct_bodies=len(distinct_digests_after),
        bytes_after=bytes_after,
        duplicates_removed=blobs_before - len(distinct_digests_after),
        bytes_saved=bytes_before - bytes_after,
        responses_checked=len(response_hashes),
        verified_ok=verified_ok,
        new_body_keys_written=new_body_keys_written,
        new_hash_keys_written=new_hash_keys_written,
        dedup_store_pre_existed=dedup_store_pre_existed,
    )


def write_compaction_log(cell_dir: Path, result: CompactionResult) -> Path:
    """
    Write content-store-compaction.json into the cell directory, overwriting any prior run.

    Stamps run-level provenance -- the compaction schema version, the
    repo commit this pass ran at (`DV_REPO_COMMIT`, the same env var
    `openwpm_run.py` and `detect_blocks.py` require; `None` outside a
    built image), and the instant this run's verification passed -- onto
    `result`'s own fields, the same split `detect_blocks.write_exclusion_report`
    keeps between a deterministic result and its run-stamped envelope.

    Args:
        cell_dir: The cell's openwpm directory.
        result: The verified result to write.
    Returns:
        path: The written content-store-compaction.json path.
    """
    payload: dict[str, object] = {
        "schema_version": COMPACTION_SCHEMA_VERSION,
        "compacted_at_utc": datetime.now(UTC).isoformat(),
        "repo_commit": os.environ.get("DV_REPO_COMMIT"),
        **result.to_json(),
    }
    path = cell_dir / COMPACTION_LOG_FILENAME
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def parse_arguments(argv: list[str] | None) -> argparse.Namespace:
    """
    Parse the dv-compact-content-store command-line arguments.

    Args:
        argv: The argument vector; defaults to sys.argv when None.
    Returns:
        arguments: The parsed argument namespace.
    """
    parser = argparse.ArgumentParser(
        description="Deduplicate one OpenWPM cell's content store into a verified sibling store."
    )
    parser.add_argument("cell_dir", type=Path, help="The cell's openwpm directory.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """
    Compact one cell's content store and write the content-store-compaction.json sidecar.

    Args:
        argv: The argument vector; defaults to sys.argv when None.
    Returns:
        exit_code: 0 on success or when there was no content store to
            compact, 2 when the cell directory does not exist, 3 when
            compaction's own verification failed. A LevelDB open failure
            or a malformed `crawl-data.sqlite` propagates uncaught rather
            than resolving to one of these codes: a cell in that state is
            not a case compaction should quietly step around.
    """
    arguments = parse_arguments(argv)
    cell_dir = arguments.cell_dir
    if not cell_dir.is_dir():
        print(f"ERROR: cell directory not found: {cell_dir}")
        return 2

    try:
        result = compact_cell(cell_dir)
    except LosslessnessViolatedError as error:
        print(f"ERROR: {error}")
        print("Refusing to write content-store-compaction.json.")
        return 3

    if result is None:
        print(f"{cell_dir}: no {ORIGINAL_STORE_DIRNAME} present; nothing to compact")
        return 0

    if result.nothing_to_do:
        print(f"{cell_dir}: already compacted, nothing to do")
    print(
        f"{cell_dir}: {result.blobs_before} blobs ({result.bytes_before / 1e6:.2f} MB) -> "
        f"{result.distinct_bodies} distinct bodies ({result.bytes_after / 1e6:.2f} MB); "
        f"{result.duplicates_removed} duplicates removed, {result.bytes_saved / 1e6:.2f} MB saved"
    )
    print(f"verified {result.verified_ok}/{result.responses_checked} http_responses rows resolve unchanged")
    log_path = write_compaction_log(cell_dir, result)
    print(f"wrote {log_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
