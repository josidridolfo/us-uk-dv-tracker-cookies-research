"""
Small helpers shared across the crawl and analysis modules.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable
from pathlib import Path

import tldextract

# Never fetches, but falls back to whichever snapshot the installed tldextract
# bundles, so this list moves with a dependency bump. The pinned list lives in
# dv_tracking.analysis.party, which the classify stage does not yet use.
EXTRACT: tldextract.TLDExtract = tldextract.TLDExtract(suffix_list_urls=())


def registered_domain_for_host(host: str) -> str:
    """
    Registered domain of a host under tldextract's bundled public suffix list.

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
