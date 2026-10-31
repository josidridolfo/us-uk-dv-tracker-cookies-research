"""
Reads main-document HTML out of OpenWPM's LevelDB content store.

The only module in the block-detection pass that imports `plyvel`. It is
installed in the crawl container's conda environment and not in this
project's `.venv` (see the module docstring of `dv_tracking.crawl.openwpm_run`
for the container's own import boundary), so every other module in the
pass takes HTML as a plain string argument instead of reading the content
store itself, and only the container-side CLI
(`dv_tracking.crawl.detect_blocks`) imports this one.

Every cell holds OpenWPM's own content store, `content-store.leveldb`,
untouched: a bare key, the ASCII-encoded value OpenWPM itself calls
`content_hash`, mapping to the stored bytes. That key is not the sha256 of
those bytes -- it is the sha256 of only the first chunk the browser
extension streamed for that response (see
`dv_tracking.crawl.compact_content_store`'s module docstring for the
upstream source this is traced to), so the same body fetched twice can
land under two different keys, and the same store ends up holding the
same bytes more than once. `dv_tracking.crawl.compact_content_store` is
the post-crawl pass that removes that duplication: it writes a sibling
directory, `content-store-dedup.leveldb`, never touching the original,
where a body lives once under `body:<its own real sha256>` and
`hash:<content_hash>` maps to the digest that resolves it.

`resolve_blob` tries that sibling's indirection first and falls back to a
bare-key lookup in whichever directory it was given, so the same function
reads a compacted cell and an uncompacted one without being told which it
has. The two forms cannot collide: no bare key OpenWPM has ever written
begins with `body:` or `hash:`, and no indirection key is a bare hex
digest, so trying the wrong form for a given directory always misses
cleanly rather than returning another key's bytes.
"""

from __future__ import annotations

from pathlib import Path

import plyvel

from dv_tracking.crawl.content_store_keys import BODY_KEY_PREFIX, HASH_KEY_PREFIX


def resolve_blob(db: plyvel.DB, content_hash: str) -> bytes | None:
    """
    Resolve one content hash against an open store, deduplicated or not.

    Args:
        db: An open LevelDB handle, either layout.
        content_hash: The sha256 hex digest from `http_responses.content_hash`.
    Returns:
        blob: The stored bytes, or None when `content_hash` is not present
            under either form.
    """
    digest = db.get((HASH_KEY_PREFIX + content_hash).encode("ascii"))
    if digest is not None:
        return db.get(BODY_KEY_PREFIX.encode("ascii") + digest)
    return db.get(content_hash.encode("ascii"))


def read_main_document_html(content_store_path: Path, content_hash: str) -> str | None:
    """
    Look up one content hash in the LevelDB content store and decode it as UTF-8.

    Args:
        content_store_path: Path to a cell's content store directory, either
            `content-store.leveldb` or `content-store-dedup.leveldb`.
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
        blob = resolve_blob(db, content_hash)
    finally:
        db.close()
    if blob is None:
        return None
    try:
        return blob.decode("utf-8")
    except UnicodeDecodeError:
        return None
