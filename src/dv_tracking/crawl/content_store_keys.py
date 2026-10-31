"""
Content-store key prefixes shared between the writer and the reader.

Pulled out on their own to break an import cycle: `compact_content_store.py`
imports `resolve_blob` from `content_store.py` to verify what it writes,
and `resolve_blob` needs these same two prefixes to do its hash:/body:
indirection -- so the prefixes cannot live in either module without one
importing the other's own importer. A shared module also keeps the writer
and the reader from drifting onto different prefixes, which is exactly
the kind of silent mismatch `resolve_blob`'s own docstring argues cannot
happen.
"""

from __future__ import annotations

BODY_KEY_PREFIX: str = "body:"
HASH_KEY_PREFIX: str = "hash:"
