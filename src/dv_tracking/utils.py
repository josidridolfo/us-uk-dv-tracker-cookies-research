"""
Small helpers shared across the crawl and analysis modules.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path


def utcnow_iso() -> str:
    """
    Current UTC time as an ISO 8601 string.

    Args:
        None
    Returns:
        timestamp: The current UTC instant in ISO 8601 format.
    """
    return datetime.now(tz=UTC).isoformat()


def keep_known_fields(cls: type, mapping: dict) -> dict:
    """
    Filter a mapping down to the keys a dataclass accepts.

    Args:
        cls: The dataclass whose field names bound the result.
        mapping: The raw key/value mapping to filter.
    Returns:
        filtered: The mapping restricted to keys that name a field of cls.
    """
    allowed = {field.name for field in dataclasses.fields(cls)}
    return {key: value for key, value in mapping.items() if key in allowed}


def read_jsonl(path: Path, build: Callable[[dict], object]) -> list:
    """
    Read a JSONL file into a list of records via a builder.

    Args:
        path: The JSONL file to read; a missing file yields an empty list.
        build: A callable turning one decoded JSON object into a record.
    Returns:
        records: One built record per non-blank line, in file order.
    """
    if not path.is_file():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(build(json.loads(line)))
    return records
