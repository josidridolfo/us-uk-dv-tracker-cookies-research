"""
Analyzed records: the RAW -> DERIVED boundary for the crawl trace.

The analyzed records carry the raw request and cookie fields, the registered
domain and third-party flag captured during the crawl, and the cookie
classification looked up from the cookie database.
"""

from __future__ import annotations

from dataclasses import dataclass

from dv_tracking.classify.cookies import CookieDatabase
from dv_tracking.classify.easyprivacy import EasyPrivacyMatcher
from dv_tracking.classify.tracker_radar import TrackerRadar
from dv_tracking.crawl.models import RawCookie, RawRequest

# An identifier-like value: long enough, not purely numeric, with real variety.
MINIMUM_IDENTIFIER_LENGTH: int = 8
MINIMUM_IDENTIFIER_DISTINCT_CHARS: int = 5


@dataclass(frozen=True)
class AnalyzedRequest:
    """A captured request with its registered domain, third-party flag, and tracker flags."""

    url: str
    host: str
    method: str
    resource_type: str
    registered_domain: str
    is_third_party: bool
    is_tracker: bool
    tracker_source: str
    tracker_categories: tuple[str, ...]

    @classmethod
    def from_raw(
        cls,
        raw: RawRequest,
        ep_matcher: EasyPrivacyMatcher,
        radar: TrackerRadar,
    ) -> AnalyzedRequest:
        """
        Carry the request's party through and derive its tracker classification.

        Args:
            raw: The captured raw request.
            ep_matcher: The host-only EasyPrivacy matcher.
            radar: The DDG Tracker Radar index.
        Returns:
            analyzed: The request carrying its party and tracker fields.
        """
        ep_hit = ep_matcher.is_tracker(raw.host)
        ddg_hit = radar.is_tracker(raw.host)
        tracker_source = cls.tracker_source_label(ep_hit=ep_hit, ddg_hit=ddg_hit)
        radar_entry = radar.lookup(raw.host)
        tracker_categories = radar_entry.categories if radar_entry is not None else ()
        return cls(
            url=raw.url,
            host=raw.host,
            method=raw.method,
            resource_type=raw.resource_type,
            registered_domain=raw.registered_domain,
            is_third_party=raw.is_third_party,
            is_tracker=ep_hit or ddg_hit,
            tracker_source=tracker_source,
            tracker_categories=tracker_categories,
        )

    @staticmethod
    def tracker_source_label(*, ep_hit: bool, ddg_hit: bool) -> str:
        """
        Label which block lists matched a request host.

        Args:
            ep_hit: Whether the EasyPrivacy matcher matched.
            ddg_hit: Whether the DDG Tracker Radar matched.
        Returns:
            source: One of "both", "easyprivacy", "ddg", or "none".
        """
        if ep_hit and ddg_hit:
            return "both"
        if ep_hit:
            return "easyprivacy"
        if ddg_hit:
            return "ddg"
        return "none"


@dataclass(frozen=True)
class AnalyzedCookie:
    """A captured cookie with its party, value, and classification flags."""

    name: str
    domain: str
    path: str
    expires_unix: float
    is_session: bool
    is_secure: bool
    is_http_only: bool
    same_site: str
    value: str
    registered_domain: str
    is_third_party: bool
    classification_matched: bool
    cookie_category: str
    cookie_vendor: str
    is_strictly_necessary: bool
    is_eprivacy_5_3_candidate: bool
    has_identifier_value: bool

    @staticmethod
    def has_identifier(value: str) -> bool:
        """
        Whether a cookie value looks like an encoded visitor identifier.

        Args:
            value: The cookie value.
        Returns:
            identifier_like: True when the value is long, non-numeric, and varied.
        """
        return (
            len(value) >= MINIMUM_IDENTIFIER_LENGTH
            and not value.isdigit()
            and len(set(value)) >= MINIMUM_IDENTIFIER_DISTINCT_CHARS
        )

    @classmethod
    def from_raw(cls, raw: RawCookie, cookie_db: CookieDatabase) -> AnalyzedCookie:
        """
        Carry the cookie's party and value through, and classify its name.

        Args:
            raw: The captured raw cookie.
            cookie_db: The cookie database used to classify the cookie name.
        Returns:
            analyzed: The cookie carrying its party, value, and classification.
        """
        classification = cookie_db.classify(raw.name)
        return cls(
            name=raw.name,
            domain=raw.domain,
            path=raw.path,
            expires_unix=raw.expires_unix,
            is_session=raw.is_session,
            is_secure=raw.is_secure,
            is_http_only=raw.is_http_only,
            same_site=raw.same_site,
            value=raw.value,
            registered_domain=raw.registered_domain,
            is_third_party=raw.is_third_party,
            classification_matched=classification.matched,
            cookie_category=classification.category,
            cookie_vendor=classification.vendor,
            is_strictly_necessary=classification.is_strictly_necessary,
            is_eprivacy_5_3_candidate=classification.is_eprivacy_5_3_candidate,
            has_identifier_value=cls.has_identifier(raw.value),
        )
