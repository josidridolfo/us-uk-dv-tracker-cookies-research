"""
Cookie classifier against a pinned Open Cookie Database snapshot.

Open Cookie Database categories map onto a normalised taxonomy via
CATEGORY_MAP. Under ePrivacy Article 5(3) / PECR, non-strictly-necessary
cookies (analytics, marketing) require prior consent, so a pre-consent match
on those categories is flagged.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path

from dv_tracking.crawl.models import RawCookie

COOKIE_CATEGORIES: tuple[str, ...] = (
    "strictly_necessary",
    "functional",
    "analytics",
    "marketing",
    "preferences",
    "unknown",
)

# Open Cookie Database 'Category' values mapped onto the normalised taxonomy.
CATEGORY_MAP: dict[str, str] = {
    "Functional": "strictly_necessary",
    "Strictly Necessary": "strictly_necessary",
    "Analytics": "analytics",
    "Marketing": "marketing",
    "Preferences": "preferences",
}

# Value shapes the crawler labels as encoded visitor identifiers.
IDENTIFIER_VALUE_SHAPES: tuple[str, ...] = ("uuid", "ulid", "hex", "base64", "dotted_id")


def compile_cookie_pattern(name: str) -> re.Pattern[str] | None:
    """
    Compile a wildcard/prefix cookie name into a full-match pattern.

    Args:
        name: The wildcard cookie name; a literal `*` maps to `.*`, otherwise
            the name is treated as a prefix (e.g. `_ga_` matches `_ga_ABC123`).
    Returns:
        pattern: The compiled pattern, or None when the name fails to compile.
    """
    body = re.escape(name).replace(r"\*", ".*")
    if "*" not in name:
        body = body + ".*"
    try:
        return re.compile("^" + body + "$")
    except re.error:
        return None


def is_identifier_like(cookie: RawCookie) -> bool:
    """
    Whether a cookie value looks like an encoded visitor identifier.

    Args:
        cookie: The raw cookie carrying its value-shape surrogate.
    Returns:
        identifier_like: True when the value's shape is an identifier shape.
    """
    return cookie.value_shape in IDENTIFIER_VALUE_SHAPES


@dataclass(frozen=True)
class CookieDatabase:
    """In-memory cookie classifier backed by the Open Cookie Database."""

    snapshot_path: Path
    exact: dict[str, tuple[str, str]]
    wildcards: tuple[tuple[re.Pattern[str], str, str], ...]

    @classmethod
    def from_csv(cls, path: Path) -> CookieDatabase:
        """
        Load the Open Cookie Database CSV into an in-memory classifier.

        Args:
            path: Path to the Open Cookie Database CSV snapshot.
        Returns:
            database: The loaded cookie database.
        """
        if not path.is_file():
            raise FileNotFoundError(f"Open Cookie Database CSV not found at {path}")
        exact: dict[str, tuple[str, str]] = {}
        wildcards: list[tuple[re.Pattern[str], str, str]] = []
        with path.open("r", encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                name = (row.get("Cookie / Data Key name") or row.get("Cookie Key") or "").strip()
                if not name:
                    continue
                category = CATEGORY_MAP.get((row.get("Category") or "").strip(), "unknown")
                vendor = (row.get("Platform") or "").strip()
                if (row.get("Wildcard match") or "").strip() == "1":
                    pattern = compile_cookie_pattern(name)
                    if pattern is not None:
                        wildcards.append((pattern, category, vendor))
                else:
                    exact[name] = (category, vendor)
        return cls(snapshot_path=path, exact=exact, wildcards=tuple(wildcards))

    def classify(self, cookie: RawCookie) -> tuple[bool, str, str]:
        """
        Classify a cookie against the exact then wildcard rules.

        Args:
            cookie: The observed raw cookie.
        Returns:
            classification: A (matched, category, vendor) tuple; matched is False when no rule fits.
        """
        rule = self.exact.get(cookie.name)
        if rule is not None:
            return (True, rule[0], rule[1])
        for pattern, category, vendor in self.wildcards:
            if pattern.fullmatch(cookie.name):
                return (True, category, vendor)
        return (False, "unknown", "")
