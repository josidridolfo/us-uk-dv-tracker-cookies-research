"""
Frozen records for the raw network trace captured during a crawl.

A raw record holds only what a single browser or network observation yields
directly. Derived fields (registered domain, third-party status, tracker
classification) belong to the analysis stage.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from dv_tracking.utils import keep_known_fields, read_jsonl


@dataclass(frozen=True)
class RawRequest:
    """A single captured network request; statically-observable fields only."""

    url: str
    method: str
    resource_type: str
    host: str

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
    """A single cookie observed during the crawl, captured with its value."""

    name: str
    domain: str
    path: str
    expires_unix: float
    is_session: bool
    is_secure: bool
    is_http_only: bool
    same_site: str
    value: str

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
    def from_playwright(cls, raw: dict) -> RawCookie:
        """
        Map a Playwright cookie dict to a RawCookie, storing the value verbatim.

        Args:
            raw: One cookie dict as returned by BrowserContext.cookies().
        Returns:
            cookie: The raw cookie carrying its value.
        """
        expires = raw.get("expires", -1.0)
        if expires is None:
            expires = -1.0
        expires = float(expires)
        return cls(
            name=raw.get("name", ""),
            domain=(raw.get("domain") or "").lstrip("."),
            path=raw.get("path", "/"),
            expires_unix=expires,
            is_session=expires < 0,
            is_secure=bool(raw.get("secure", False)),
            is_http_only=bool(raw.get("httpOnly", False)),
            same_site=str(raw.get("sameSite") or "Unspecified"),
            value=raw.get("value", ""),
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
class BannerSignals:
    """Raw DOM and JS-API probe outputs for consent-banner detection."""

    consentomatic_matches: tuple[str, ...]
    dom_heuristic_matched: bool
    dom_heuristic_evidence: str | None
    usp_api_present: bool
    gpp_api_present: bool
    close_button_present: bool
    quick_exit_present: bool
    error: str | None

    @classmethod
    def from_dict(cls, mapping: dict) -> BannerSignals:
        """
        Builds banner signals from a decoded JSON object.

        Args:
            mapping: The decoded signals record; unknown keys are dropped.
        Returns:
            signals: The banner probe signals.
        """
        fields = keep_known_fields(cls, mapping)
        if "consentomatic_matches" in fields:
            fields["consentomatic_matches"] = tuple(fields["consentomatic_matches"])
        return cls(**fields)


@dataclass(frozen=True)
class LocalStorageSnapshot:
    """The visited page's first-party (top-origin) localStorage entries and aggregate stats."""

    entries: tuple[tuple[str, str], ...]
    key_count: int
    total_bytes: int
    error: str | None

    @classmethod
    def from_dict(cls, mapping: dict) -> LocalStorageSnapshot:
        """
        Builds a localStorage snapshot from a decoded JSON object.

        Args:
            mapping: The decoded snapshot record; unknown keys are dropped.
        Returns:
            snapshot: The localStorage snapshot.
        """
        fields = keep_known_fields(cls, mapping)
        if "entries" in fields:
            fields["entries"] = tuple(tuple(entry) for entry in fields["entries"])
        return cls(**fields)


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
    banner_signals: BannerSignals | None
    local_storage: LocalStorageSnapshot | None
    crawl_start_utc: str
    crawl_end_utc: str
    time_on_page_since_page_load: float | None

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
        banner_signals = mapping.get("banner_signals")
        fields["banner_signals"] = (
            BannerSignals.from_dict(banner_signals) if banner_signals is not None else None
        )
        local_storage = mapping.get("local_storage")
        fields["local_storage"] = (
            LocalStorageSnapshot.from_dict(local_storage) if local_storage is not None else None
        )
        return cls(**fields)

    @classmethod
    def from_disk(cls, trial_dir: Path) -> RawTrial | None:
        """
        Rebuild a raw trial from its saved metadata and request/cookie traces.

        Args:
            trial_dir: The trial directory holding metadata.json, requests.jsonl and cookies.jsonl.
        Returns:
            trial: The rebuilt raw trial, or None when metadata is absent or unreadable.
        """
        metadata_file = trial_dir / "metadata.json"
        if not metadata_file.is_file():
            return None
        try:
            metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        requests = read_jsonl(trial_dir / "requests.jsonl", RawRequest.from_dict)
        cookies = read_jsonl(trial_dir / "cookies.jsonl", RawCookie.from_dict)
        tcf_probe = metadata.get("tcf_probe")
        local_storage = metadata.get("local_storage")
        banner_signals = metadata.get("banner_signals")
        return cls(
            site_id=metadata["site_id"],
            url=metadata["url"],
            geolocation=metadata["geolocation"],
            trial=metadata["trial"],
            navigation_status=metadata.get("navigation_status"),
            navigation_error=metadata.get("navigation_error"),
            blocked=metadata.get("blocked", False),
            requests=tuple(requests),
            cookies=tuple(cookies),
            tcf_probe=TCFProbeResult.from_dict(tcf_probe) if tcf_probe is not None else None,
            banner_signals=(
                BannerSignals.from_dict(banner_signals) if banner_signals is not None else None
            ),
            local_storage=(
                LocalStorageSnapshot.from_dict(local_storage) if local_storage is not None else None
            ),
            crawl_start_utc=metadata["crawl_start_utc"],
            crawl_end_utc=metadata["crawl_end_utc"],
            time_on_page_since_page_load=metadata.get("time_on_page_since_page_load"),
        )


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
