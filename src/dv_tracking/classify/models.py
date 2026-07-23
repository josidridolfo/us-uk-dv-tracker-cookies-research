"""
Analyzed records: the RAW -> DERIVED boundary for the crawl trace.

The analyzed records carry the raw fields plus fields derived from the pinned
public-suffix list and the cookie database -- registered domain, third-party
status, and cookie classification. These live only here; the raw records never
carry them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from dv_tracking.classify.banner import BannerClassifier, BannerFinding
from dv_tracking.classify.cookies import CookieDatabase
from dv_tracking.classify.easyprivacy import EasyPrivacyMatcher
from dv_tracking.classify.tracker_radar import TrackerRadar
from dv_tracking.crawl.models import LocalStorageSnapshot, RawCookie, RawRequest, RawTrial
from dv_tracking.utils import is_third_party_to_origin, registered_domain_for_host

if TYPE_CHECKING:
    # platform imports this module; the hint stays type-only to avoid an import cycle.
    from dv_tracking.classify.platform import HostingPlatform, HostingPlatformClassifier

# An identifier-like value: long enough, not purely numeric, with real variety.
MINIMUM_IDENTIFIER_LENGTH: int = 8
MINIMUM_IDENTIFIER_DISTINCT_CHARS: int = 5


@dataclass(frozen=True)
class AnalyzedRequest:
    """A captured request with its derived party and EP-union DDG tracker flags."""

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
        page_origin_domain: str,
        ep_matcher: EasyPrivacyMatcher,
        radar: TrackerRadar,
    ) -> AnalyzedRequest:
        """
        Derive the party and tracker classification for a raw request.

        Args:
            raw: The captured raw request.
            page_origin_domain: The registered domain of the visited page.
            ep_matcher: The host-only EasyPrivacy matcher.
            radar: The DDG Tracker Radar index.
        Returns:
            analyzed: The request carrying its derived party and tracker fields.
        """
        registered_domain = registered_domain_for_host(raw.host)
        is_third_party = is_third_party_to_origin(registered_domain, page_origin_domain)
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
            registered_domain=registered_domain,
            is_third_party=is_third_party,
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
    """A captured cookie with its derived party and classification flags."""

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
    def from_raw(
        cls, raw: RawCookie, cookie_db: CookieDatabase, page_origin_domain: str
    ) -> AnalyzedCookie:
        """
        Derive the party and classification flags for a raw cookie.

        Args:
            raw: The captured raw cookie.
            cookie_db: The cookie database used to classify the cookie name.
            page_origin_domain: The registered domain of the visited page.
        Returns:
            analyzed: The cookie carrying its derived party and classification.
        """
        registered_domain = registered_domain_for_host(raw.domain)
        is_third_party = is_third_party_to_origin(registered_domain, page_origin_domain)
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
            registered_domain=registered_domain,
            is_third_party=is_third_party,
            classification_matched=classification.matched,
            cookie_category=classification.category,
            cookie_vendor=classification.vendor,
            is_strictly_necessary=classification.is_strictly_necessary,
            is_eprivacy_5_3_candidate=classification.is_eprivacy_5_3_candidate,
            has_identifier_value=cls.has_identifier(raw.value),
        )


@dataclass(frozen=True)
class AnalyzedTrial:
    """The analyzed products of one raw trial: analyzed requests, cookies, banner, platform."""

    site_id: str
    url: str
    geolocation: str
    trial: int
    blocked: bool
    analyzed_requests: tuple[AnalyzedRequest, ...]
    analyzed_cookies: tuple[AnalyzedCookie, ...]
    banner_finding: BannerFinding
    hosting_platform: HostingPlatform
    local_storage: LocalStorageSnapshot | None

    @classmethod
    def classify(
        cls,
        raw_trial: RawTrial,
        *,
        cookie_db: CookieDatabase,
        ep_matcher: EasyPrivacyMatcher,
        radar: TrackerRadar,
        banner_classifier: BannerClassifier,
        platform_classifier: HostingPlatformClassifier,
        consentomatic_bundle: object | None = None,
    ) -> AnalyzedTrial:
        """
        Derive the analyzed products of one raw trial.

        Args:
            raw_trial: The raw trial to analyze.
            cookie_db: The cookie database used to classify cookie names.
            ep_matcher: The host-only EasyPrivacy matcher.
            radar: The DDG Tracker Radar index.
            banner_classifier: The multi-source banner classifier.
            platform_classifier: The hosting-platform classifier.
            consentomatic_bundle: A bundle whose rules restrict recognised CMP tells, or None.
        Returns:
            analyzed: The analyzed trial carrying the derived requests, cookies, banner, platform.
        """
        hostname = urlparse(raw_trial.url).hostname or ""
        page_origin_domain = registered_domain_for_host(hostname)
        analyzed_requests = tuple(
            AnalyzedRequest.from_raw(request, page_origin_domain, ep_matcher, radar)
            for request in raw_trial.requests
        )
        analyzed_cookies = tuple(
            AnalyzedCookie.from_raw(cookie, cookie_db, page_origin_domain)
            for cookie in raw_trial.cookies
        )
        third_party_hosts = [
            request.host for request in analyzed_requests if request.is_third_party
        ]
        banner_finding = banner_classifier.detect(
            raw_trial.banner_signals,
            consentomatic_bundle,
            raw_trial.tcf_probe,
            third_party_hosts,
        )
        hosting_platform = platform_classifier.classify(analyzed_requests, analyzed_cookies)
        return cls(
            site_id=raw_trial.site_id,
            url=raw_trial.url,
            geolocation=raw_trial.geolocation,
            trial=raw_trial.trial,
            blocked=raw_trial.blocked,
            analyzed_requests=analyzed_requests,
            analyzed_cookies=analyzed_cookies,
            banner_finding=banner_finding,
            hosting_platform=hosting_platform,
            local_storage=raw_trial.local_storage,
        )
