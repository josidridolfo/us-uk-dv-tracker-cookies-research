"""
Reads main-document HTML out of OpenWPM's LevelDB content store.

The only module in the block-detection pass that imports `plyvel`. It is
installed in the crawl container's conda environment and not in this
project's `.venv` (see the module docstring of `dv_tracking.crawl.openwpm_run`
for the container's own import boundary), so every other module in the
pass takes HTML as a plain string argument instead of reading the content
store itself, and only the container-side CLI
(`dv_tracking.crawl.detect_blocks`) imports this one.
"""

from __future__ import annotations

from pathlib import Path

import plyvel


def read_main_document_html(content_store_path: Path, content_hash: str) -> str | None:
    """
    Look up one content hash in the LevelDB content store and decode it as UTF-8.

    The store's value is the exact bytes OpenWPM captured, keyed by the
    ASCII-encoded sha256 hex digest of those bytes, with no further framing
    or compression layered on top by the project's own code (confirmed
    against a captured block page from this corpus's own content store,
    and against OpenWPM v0.38.0's content-store tests, which assert the
    retrieved bytes hash back to the same key).

    Args:
        content_store_path: Path to the cell's content-store.leveldb directory.
        content_hash: The sha256 hex digest from `http_responses.content_hash`.
    Returns:
        html: The decoded document, or None when the store directory is
            absent, opening it failed, the hash is not present in it, or
            the stored bytes do not decode as UTF-8 -- every case reported
            as "not retrievable", never as a crash.
    """
    if not content_store_path.is_dir():
        return None
    try:
        db = plyvel.DB(str(content_store_path), create_if_missing=False, compression="snappy")
    except OSError:
        return None
    try:
        blob = db.get(content_hash.encode("ascii"))
    finally:
        db.close()
    if blob is None:
        return None
    try:
        return blob.decode("utf-8")
    except UnicodeDecodeError:
        return None
