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

# An identifier-like value: long enough, not purely numeric, with real variety.
MINIMUM_IDENTIFIER_LENGTH: int = 8
MINIMUM_IDENTIFIER_DISTINCT_CHARS: int = 5


def is_identifier_like(cookie: RawCookie) -> bool:
    """
    Whether a cookie value looks like an encoded visitor identifier.

    Args:
        cookie: The raw cookie carrying its value.
    Returns:
        identifier_like: True when the value is long, non-numeric, and varied.
    """
    return (
        len(cookie.value) >= MINIMUM_IDENTIFIER_LENGTH
        and not cookie.value.isdigit()
        and len(set(cookie.value)) >= MINIMUM_IDENTIFIER_DISTINCT_CHARS
    )


@dataclass(frozen=True)
class CookieRule:
    """One row of the Open Cookie Database, normalised."""

    name_or_pattern: str
    is_wildcard: bool
    vendor: str
    raw_category: str
    normalised_category: str
    description: str
    retention: str
    pattern: re.Pattern[str] | None

    @staticmethod
    def parse_rule(row: dict[str, str]) -> CookieRule | None:
        """
        Build a normalised rule from one Open Cookie Database CSV row.

        Args:
            row: The decoded CSV row keyed by OCD column names.
        Returns:
            rule: The normalised rule, or None when the row names no cookie.
        """
        name = (row.get("Cookie / Data Key name") or row.get("Cookie Key") or "").strip()
        if not name:
            return None
        is_wildcard = (row.get("Wildcard match") or "").strip() == "1"
        pattern = CookieRule.compile_pattern(name) if is_wildcard else None
        return CookieRule(
            name_or_pattern=name,
            is_wildcard=is_wildcard,
            vendor=(row.get("Platform") or "").strip(),
            raw_category=(row.get("Category") or "").strip(),
            normalised_category=CATEGORY_MAP.get((row.get("Category") or "").strip(), "unknown"),
            description=(row.get("Description") or "").strip(),
            retention=(row.get("Retention period") or "").strip(),
            pattern=pattern,
        )

    @staticmethod
    def compile_pattern(name: str) -> re.Pattern[str] | None:
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


@dataclass(frozen=True)
class CookieDatabase:
    """In-memory cookie classifier backed by the Open Cookie Database."""

    snapshot_path: Path
    exact_rules: dict[str, CookieRule]
    wildcard_rules: tuple[CookieRule, ...]

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
        exact: dict[str, CookieRule] = {}
        wildcards: list[CookieRule] = []
        with path.open("r", encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                rule = CookieRule.parse_rule(row)
                if rule is None:
                    continue
                if rule.is_wildcard:
                    wildcards.append(rule)
                else:
                    exact[rule.name_or_pattern] = rule
        return cls(snapshot_path=path, exact_rules=exact, wildcard_rules=tuple(wildcards))

    def classify(self, cookie: RawCookie) -> CookieRule | None:
        """
        Classify a cookie against the exact then wildcard rules.

        Args:
            cookie: The observed raw cookie.
        Returns:
            rule: The matched rule, or None when no rule fits.
        """
        if not cookie.name:
            return None
        rule = self.exact_rules.get(cookie.name)
        if rule is not None:
            return rule
        for wildcard in self.wildcard_rules:
            if wildcard.pattern is not None and wildcard.pattern.fullmatch(cookie.name):
                return wildcard
        return None
