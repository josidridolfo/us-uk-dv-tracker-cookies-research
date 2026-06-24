"""
Frozen records for the raw network trace captured during a crawl.

A raw record holds what a single browser or network observation yields,
including the registered domain and third-party status resolved against the
visited page. Tracker classification belongs to the analysis stage.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from math import log2
from pathlib import Path

from dv_tracking.utils import (
    is_third_party_to_origin,
    keep_known_fields,
    registered_domain_for_host,
)


@dataclass(frozen=True)
class RawRequest:
    """A single captured network request with its registered domain and party."""

    url: str
    method: str
    resource_type: str
    host: str
    registered_domain: str
    is_third_party: bool

    @classmethod
    def from_dict(cls, mapping: dict) -> RawRequest:
        """
        Build a raw request from a decoded JSON object.

        Args:
            mapping: The decoded request record; unknown keys are dropped.
        Returns:
            request: The raw request record.
        """
        return cls(**keep_known_fields(cls, mapping))


@dataclass(frozen=True)
class RawCookie:
    """A single cookie observed during the crawl, kept as a value-entropy surrogate."""

    name: str
    domain: str
    registered_domain: str
    path: str
    expires_unix: float
    is_session: bool
    is_secure: bool
    is_http_only: bool
    same_site: str
    is_third_party: bool
    value_shape: str
    value_entropy_bits: float
    value_length: int

    @staticmethod
    def convert_cookie_value_to_entropy_bits(value: str) -> float:
        """
        Shannon entropy of the value's character distribution, in bits.

        Args:
            value: The cookie value to measure.
        Returns:
            bits: The Shannon entropy in bits; 0.0 for an empty value.
        """
        if not value:
            return 0.0
        counts = Counter(value)
        total = len(value)
        return -sum((count / total) * log2(count / total) for count in counts.values())

    @staticmethod
    def classify_cookie_value_shape(value: str) -> str:
        """
        Coarse structural label for a cookie value; no raw value is retained.

        Args:
            value: The cookie value to classify.
        Returns:
            shape: One of uuid, ulid, hex, base64, dotted_id, numeric, short, empty, other.
        """
        if not value:
            return "empty"
        if len(value) < 6:
            return "short"
        if len(value) == 36 and value.count("-") == 4:
            return "uuid"
        if len(value) == 26 and value.isalnum() and value.isupper():
            return "ulid"
        if value.count(".") >= 2 and any(char.isdigit() for char in value):
            return "dotted_id"
        # hex precedes numeric to match the crawler ladder: long all-digit ids read as hex.
        if len(value) >= 8 and all(char in "0123456789abcdefABCDEF" for char in value):
            return "hex"
        if len(value) >= 12 and all(char.isalnum() or char in "+/=_-" for char in value):
            return "base64"
        if value.isdigit():
            return "numeric"
        return "other"

    @classmethod
    def from_dict(cls, mapping: dict) -> RawCookie:
        """
        Build a raw cookie from a decoded JSON object.

        Args:
            mapping: The decoded cookie record; unknown keys are dropped.
        Returns:
            cookie: The raw cookie record.
        """
        return cls(**keep_known_fields(cls, mapping))

    @classmethod
    def from_playwright(cls, raw: dict, page_origin_domain: str) -> RawCookie:
        """
        Map a Playwright cookie dict to a RawCookie, keeping a value-entropy surrogate.

        Args:
            raw: One cookie dict as returned by BrowserContext.cookies().
            page_origin_domain: The visited page's registered domain.
        Returns:
            cookie: The raw cookie carrying its party and value surrogate.
        """
        expires = raw.get("expires", -1.0)
        if expires is None:
            expires = -1.0
        expires = float(expires)
        domain = (raw.get("domain") or "").lstrip(".")
        value = raw.get("value", "")
        registered_domain = registered_domain_for_host(domain)
        return cls(
            name=raw.get("name", ""),
            domain=domain,
            registered_domain=registered_domain,
            path=raw.get("path", "/"),
            expires_unix=expires,
            is_session=expires < 0,
            is_secure=bool(raw.get("secure", False)),
            is_http_only=bool(raw.get("httpOnly", False)),
            same_site=str(raw.get("sameSite") or "Unspecified"),
            is_third_party=is_third_party_to_origin(registered_domain, page_origin_domain),
            value_shape=cls.classify_cookie_value_shape(value),
            value_entropy_bits=cls.convert_cookie_value_to_entropy_bits(value),
            value_length=len(value),
        )


@dataclass(frozen=True)
class TCFProbeResult:
    """Outcome of probing the page for a TCF consent-management API."""

    api_present: bool
    gdpr_applies: bool | None
    cmp_id: int | None
    cmp_loaded: bool | None
    cmp_status: str | None
    display_status: str | None
    tcf_policy_version: int | None

    @classmethod
    def from_dict(cls, mapping: dict) -> TCFProbeResult:
        """
        Builds a TCF probe result from a decoded JSON object.

        Args:
            mapping: The decoded probe record; unknown keys are dropped.
        Returns:
            probe: The TCF probe result.
        """
        return cls(**keep_known_fields(cls, mapping))


@dataclass(frozen=True)
class RawTrial:
    """The raw result of crawling one site at one geolocation."""

    site_id: str
    url: str
    geolocation: str
    trial: int
    navigation_status: int | None
    navigation_error: str | None
    blocked: bool
    requests: tuple[RawRequest, ...]
    cookies: tuple[RawCookie, ...]
    tcf_probe: TCFProbeResult | None
    crawl_start_utc: str
    crawl_end_utc: str

    @classmethod
    def from_dict(cls, mapping: dict) -> RawTrial:
        """
        Build a raw trial from a decoded JSON object with embedded records.

        Args:
            mapping: The decoded trial record; unknown keys are dropped.
        Returns:
            trial: The raw trial record.
        """
        fields = keep_known_fields(cls, mapping)
        fields["requests"] = tuple(RawRequest.from_dict(row) for row in mapping.get("requests", ()))
        fields["cookies"] = tuple(RawCookie.from_dict(row) for row in mapping.get("cookies", ()))
        tcf_probe = mapping.get("tcf_probe")
        fields["tcf_probe"] = TCFProbeResult.from_dict(tcf_probe) if tcf_probe is not None else None
        return cls(**fields)


@dataclass(frozen=True)
class ConsentomaticBundle:
    """The Consent-O-Matic rule identifiers loaded for a crawl."""

    rules: tuple[str, ...]

    @classmethod
    def from_dict(cls, mapping: dict) -> ConsentomaticBundle:
        """
        Build a Consent-O-Matic bundle from a decoded JSON object.

        Args:
            mapping: The decoded bundle record; unknown keys are dropped.
        Returns:
            bundle: The Consent-O-Matic rule bundle.
        """
        fields = keep_known_fields(cls, mapping)
        if "rules" in fields:
            fields["rules"] = tuple(fields["rules"])
        return cls(**fields)

    @classmethod
    def from_rules_file(cls, path: Path) -> ConsentomaticBundle | None:
        """
        Read the Consent-O-Matic Rules.json and collect its rule identifiers.

        Args:
            path: Path to the Rules.json file, whose top-level keys name the rules.
        Returns:
            bundle: The rule-identifier bundle, or None when the file is absent or unreadable.
        """
        if not path.is_file():
            return None
        try:
            rules = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        if not isinstance(rules, dict):
            return None
        return cls(rules=tuple(sorted(rules)))
