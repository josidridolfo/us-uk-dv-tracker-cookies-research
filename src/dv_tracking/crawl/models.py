"""
Frozen records for the raw network trace captured during a crawl.

A raw record holds what a single browser or network observation yields,
including the registered domain and third-party status resolved against the
visited page. Tracker classification belongs to the analysis stage.
"""

from __future__ import annotations

from dataclasses import dataclass

from dv_tracking.utils import keep_known_fields


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
    """A single cookie observed during the crawl, kept by its identity fields."""

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
        return cls(**fields)
