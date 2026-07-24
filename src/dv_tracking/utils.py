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
from dv_tracking.constants import COUNTRY_CODE_TO_GEOLOCATION, DATACENTER_KEYWORDS

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


def is_datacenter(org: str) -> bool:
    """
    Reports whether an ipinfo org string names a known datacenter or cloud host.

    Args:
        org: The ipinfo `org` field for the exit IP.
    Returns:
        datacenter: True when any datacenter keyword appears in the lowercased org.
    """
    lowered = org.lower()
    datacenter = any(keyword in lowered for keyword in DATACENTER_KEYWORDS)
    return datacenter


def geolocation_for_country(country_code: str) -> str | None:
    """
    Maps an ISO country code to the project geolocation label.

    Args:
        country_code: The ISO 3166-1 alpha-2 country code.
    Returns:
        geolocation: The mapped label, or None when the country is unmapped.
    """
    return COUNTRY_CODE_TO_GEOLOCATION.get(country_code)


def keep_known_fields(cls: type, mapping: dict) -> dict:
    """
    Filters a mapping down to the keys a dataclass accepts.

    Args:
        cls: The dataclass whose field names bound the result.
        mapping: The raw key/value mapping to filter.
    Returns:
        filtered: The mapping restricted to keys that name a field of cls.
    """
    allowed = {field.name for field in dataclasses.fields(cls)}
    return {key: value for key, value in mapping.items() if key in allowed}


LATEX_SPECIAL_CHARS: dict[str, str] = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}


def escape_latex(text: str) -> str:
    """
    Escapes the LaTeX special characters in a string for use in a table cell.

    A single pass over the source characters, so braces introduced by the
    backslash replacement are never re-escaped.

    Args:
        text: The raw string to escape.
    Returns:
        escaped: The string with LaTeX special characters replaced by their escaped forms.
    """
    escaped = "".join(LATEX_SPECIAL_CHARS.get(character, character) for character in text)
    return escaped


def format_latex_number(value: float | int, decimals: int) -> str:
    """
    Formats a numeric value at a fixed number of decimal places.

    Args:
        value: The numeric value to format.
        decimals: The number of decimal places to render.
    Returns:
        formatted: The value as a fixed-point string, with no percent sign and no escaping.
    """
    formatted = f"{value:.{decimals}f}"
    return formatted


def latex_column_spec(alignments: tuple[str, ...]) -> str:
    """
    Joins per-column alignment letters into a tabular column specification.

    Args:
        alignments: One alignment letter per column, each in {"l", "r", "c"}.
    Returns:
        spec: The concatenated column specification, e.g. ("l", "r", "r") -> "lrr".
    Raises:
        ValueError: When any element is not one of "l", "r", or "c".
    """
    for alignment in alignments:
        if alignment not in {"l", "r", "c"}:
            raise ValueError(f"invalid column alignment: {alignment!r}")
    spec = "".join(alignments)
    return spec


def read_jsonl(path: Path, build: Callable[[dict], object]) -> list:
    """
    Reads a JSONL file into a list of records via a builder.

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
