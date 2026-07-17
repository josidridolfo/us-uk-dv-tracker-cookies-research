"""
Small helpers shared across the crawl and analysis modules.
"""

from __future__ import annotations

import dataclasses
import json
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import tldextract

from dv_tracking import PROJECT_ROOT

EXTRACT: tldextract.TLDExtract = tldextract.TLDExtract(suffix_list_urls=())


def registered_domain_for_host(host: str) -> str:
    """
    Registered domain of a host under the pinned public suffix list.

    Args:
        host: The request host or cookie domain.
    Returns:
        registered_domain: The registered domain, or the bare host when no suffix is known.
    """
    return EXTRACT(host).top_domain_under_public_suffix or host


def is_third_party_to_origin(registered_domain: str, page_origin_domain: str) -> bool:
    """
    Whether a registered domain differs from the visited page's origin domain.

    Args:
        registered_domain: The record's registered domain.
        page_origin_domain: The visited page's registered domain.
    Returns:
        third_party: True when the registered domain is non-empty and differs from the origin.
    """
    return registered_domain != page_origin_domain and registered_domain != ""


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


def load_env_file(path: Path = PROJECT_ROOT / ".env") -> None:
    """
    Loads KEY=value lines from a .env file into os.environ without clobbering.

    Real exported variables win: each key is set only when absent, so exporting
    or `source`-ing overrides the file. A missing file is a no-op; no value is
    ever logged.

    Args:
        path: The .env file to read; defaults to the repo-root .env.
    Returns:
        None
    """
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)
