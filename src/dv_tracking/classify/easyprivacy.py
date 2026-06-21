"""
EasyPrivacy tracker classifier (host-only).

Parses `||host^` rules from a pinned EasyPrivacy snapshot, with or without the
`$third-party`, `$3p`, or `$document` options. A host is flagged as a tracker
when it, or a parent domain, is on the block list. Element-hiding rules
(`##selector`) and exception rules (`@@||...^`) are dropped.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import tldextract

HOST_RULE_PREFIX: str = "||"
HOST_RULE_SUFFIXES: tuple[str, ...] = ("^", "^$third-party", "^$3p", "^$document")

# Pinned public-suffix snapshot bundled with tldextract; suffix_list_urls=() never fetches.
EXTRACT: tldextract.TLDExtract = tldextract.TLDExtract(suffix_list_urls=())


@dataclass(frozen=True)
class EasyPrivacyMatcher:
    """Host-only EasyPrivacy tracker classifier."""

    host_rule_count: int
    snapshot_path: Path
    exact_hosts: frozenset[str]
    suffix_hosts: frozenset[str]

    @staticmethod
    def parse_host_rule(rule: str) -> str | None:
        """
        Extract the host from a `||host^...` rule.

        Args:
            rule: One EasyPrivacy rule line beginning with the host-rule prefix.
        Returns:
            host: The lowercased host, or None when the line is not a plain host rule.
        """
        body = rule[len(HOST_RULE_PREFIX):]
        suffix_pos = body.find("^")
        if suffix_pos < 0:
            return None
        host = body[:suffix_pos]
        remainder = body[suffix_pos:]
        if not host or "/" in host or "*" in host:
            return None
        if remainder not in HOST_RULE_SUFFIXES and not remainder.startswith("^$"):
            return None
        extracted = EXTRACT(host)
        if not extracted.domain or not extracted.suffix:
            return None
        return host.lower()

    @classmethod
    def from_snapshot(cls, path: Path) -> EasyPrivacyMatcher:
        """
        Load the host rules from an EasyPrivacy snapshot.

        Args:
            path: Path to the EasyPrivacy text snapshot.
        Returns:
            matcher: The host-only matcher built from the snapshot's host rules.
        """
        if not path.is_file():
            raise FileNotFoundError(f"EasyPrivacy snapshot not found at {path}")
        hosts: set[str] = set()
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("!") or line.startswith("["):
                continue
            if not line.startswith(HOST_RULE_PREFIX):
                continue
            host = cls.parse_host_rule(line)
            if host is None:
                continue
            hosts.add(host)
        return cls(
            host_rule_count=len(hosts),
            snapshot_path=path,
            exact_hosts=frozenset(hosts),
            suffix_hosts=frozenset(hosts),
        )

    def is_tracker(self, host: str) -> bool:
        """
        Whether a host matches a block rule directly or via a parent domain.

        Args:
            host: The request host.
        Returns:
            tracker: True when the host or a parent domain is on the block list.
        """
        if not host:
            return False
        cleaned = host.lower().strip(".")
        if cleaned in self.exact_hosts or cleaned in self.suffix_hosts:
            return True
        parts = cleaned.split(".")
        for index in range(1, len(parts)):
            candidate = ".".join(parts[index:])
            if candidate in self.suffix_hosts:
                return True
        return False

    def is_tracker_domain(self, registered_domain: str) -> bool:
        """
        Whether a registered domain matches a block rule.

        Args:
            registered_domain: The registered domain to test.
        Returns:
            tracker: True when the registered domain is on the block list.
        """
        if not registered_domain:
            return False
        candidate = registered_domain.lower()
        return candidate in self.exact_hosts or candidate in self.suffix_hosts
