"""
Category-filtered lookup over a pinned DDG Tracker Radar snapshot.

A domain counts as a known tracker when its DDG categories intersect
TRACKER_CATEGORIES. Service categorisations (CDN, Embedded Content, Federated
Login, etc.) do not fire is_tracker; downstream classification unions this with
EasyPrivacy so uncategorised trackers are still caught there.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import tldextract

# DDG categories that count as tracking behaviour.
TRACKER_CATEGORIES: frozenset[str] = frozenset(
    {
        "Advertising",
        "Ad Motivated Tracking",
        "Ad Fraud",
        "Analytics",
        "Audience Measurement",
        "Third-Party Analytics Marketing",
        "Session Replay",
        "Fingerprinting",
        "Action Pixels",
    }
)

# Pinned public-suffix snapshot bundled with tldextract; suffix_list_urls=() never fetches.
EXTRACT: tldextract.TLDExtract = tldextract.TLDExtract(suffix_list_urls=())


@dataclass(frozen=True)
class TrackerEntry:
    """One DDG Tracker Radar domain entry."""

    domain: str
    owner: str | None
    categories: tuple[str, ...]

    @classmethod
    def from_index_entry(cls, domain: str, entry: dict) -> TrackerEntry:
        """
        Builds a tracker entry from one DDG index record.

        Args:
            domain: The registered domain the record is keyed under.
            entry: The decoded DDG entry with owner and categories.
        Returns:
            tracker_entry: The normalised tracker entry.
        """
        return cls(
            domain=domain,
            owner=entry.get("owner"),
            categories=tuple(entry.get("categories", ())),
        )

    @property
    def is_tracking(self) -> bool:
        """
        Whether this entry's categories intersect the tracking categories.

        Args:
            None
        Returns:
            tracking: True when any category is a tracking category.
        """
        return any(category in TRACKER_CATEGORIES for category in self.categories)


@dataclass(frozen=True)
class TrackerRadar:
    """In-memory index over a pinned DDG snapshot, filtered at query time."""

    snapshot_path: Path
    entries: dict[str, TrackerEntry]

    @property
    def domain_count(self) -> int:
        """
        The number of domains in the index.

        Args:
            None
        Returns:
            count: The index size.
        """
        return len(self.entries)

    @classmethod
    def from_index(cls, path: Path) -> TrackerRadar:
        """
        Loads the compact DDG Tracker Radar index.

        Args:
            path: Path to the DDG index JSON snapshot.
        Returns:
            radar: The loaded tracker-radar index.
        """
        if not path.is_file():
            raise FileNotFoundError(f"Tracker Radar index not found at {path}")
        raw = json.loads(path.read_text(encoding="utf-8"))
        entries = {domain: TrackerEntry.from_index_entry(domain, entry) for domain, entry in raw.items()}
        return cls(snapshot_path=path, entries=entries)

    def lookup(self, host: str) -> TrackerEntry | None:
        """
        Returns the DDG entry for a host, falling back to its registered domain.

        Args:
            host: The request host.
        Returns:
            entry: The matching tracker entry, or None when the domain is unknown.
        """
        if not host:
            return None
        cleaned = host.lower().strip(".")
        entry = self.entries.get(cleaned)
        if entry is not None:
            return entry
        extracted = EXTRACT(cleaned)
        if extracted.suffix and extracted.domain:
            return self.entries.get(f"{extracted.domain}.{extracted.suffix}")
        return None

    def is_tracker(self, host: str) -> bool:
        """
        Whether a host's DDG categories mark it as a known tracker.

        Args:
            host: The request host.
        Returns:
            tracker: True when the matched entry has a tracking category.
        """
        entry = self.lookup(host)
        return entry is not None and entry.is_tracking
