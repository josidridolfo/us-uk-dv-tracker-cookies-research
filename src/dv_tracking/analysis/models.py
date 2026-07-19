"""
Per-site privacy findings.

A SitePrivacyFindings row aggregates one analyzed trial into compliance-style
counts and flags for one site at one trial. It is the study's per-cell unit of
analysis; identity and category metadata are carried from the corpus site.
"""

from __future__ import annotations

from dataclasses import dataclass

from dv_tracking.classify.models import AnalyzedTrial
from dv_tracking.sites import DVSupportSite

# Direct-service categories: a visit strongly implies the visitor is seeking help,
# so the health inference (Art 9, per C-252/21) is strong.
DIRECT_SERVICE_CATEGORIES: frozenset[str] = frozenset(
    {"local_shelter", "national_hotline", "legal_aid", "perpetrator_rehab"}
)

# Identity-specific categories: an additive Art 9 axis (orientation / ethnicity).
IDENTITY_SPECIFIC_CATEGORIES: frozenset[str] = frozenset(
    {"lgbtq_specific", "minority_community"}
)

# A visit implicates Art 9 for the direct-service or identity tiers; mixed-audience
# advocacy / national-charity / youth bodies are a separate, weaker tier.
ART9_IMPLICATED_CATEGORIES: frozenset[str] = (
    DIRECT_SERVICE_CATEGORIES | IDENTITY_SPECIFIC_CATEGORIES
)


@dataclass(frozen=True)
class SitePrivacyFindings:
    """Aggregate privacy findings for one site at one trial."""

    site_id: str
    org_name: str
    country: str
    category: str
    url: str
    geolocation: str
    trial: int
    crawl_date: str
    blocked: bool

    total_cookie_count: int
    third_party_cookie_count: int
    classified_cookie_count: int
    unknown_cookie_count: int
    tracking_cookie_count: int
    eprivacy_5_3_candidate_count: int
    eprivacy_5_3_vendors: tuple[str, ...]

    third_party_request_count: int
    third_party_domain_count: int
    tracker_request_count: int
    tracker_domain_count: int
    ep_only_request_count: int
    ddg_only_request_count: int
    both_lists_request_count: int

    banner_present: bool
    banner_primary_method: str | None
    cmp_vendor: str | None
    hosting_platform: str
    local_storage_key_count: int

    art_9_implicated: bool
    art_9_identity_specific: bool
    art_9_tier: str
    art_9_tracking_cookie_present: bool
    art_9_tracker_request_present: bool
    art_9_transmission_present: bool

    @staticmethod
    def art_9_tier_for_category(category: str) -> str:
        """
        Label the Art 9 tier a site category falls into.

        Args:
            category: The corpus site category.
        Returns:
            tier: "direct_service", "identity", or "not_implicated".
        """
        if category in DIRECT_SERVICE_CATEGORIES:
            return "direct_service"
        if category in IDENTITY_SPECIFIC_CATEGORIES:
            return "identity"
        return "not_implicated"

    @classmethod
    def from_trial(
        cls, analyzed_trial: AnalyzedTrial, site: DVSupportSite, *, crawl_date: str = ""
    ) -> SitePrivacyFindings:
        """
        Aggregate one analyzed trial into a per-site findings row.

        Args:
            analyzed_trial: The analyzed trial to aggregate.
            site: The corpus site supplying identity and category metadata.
            crawl_date: The crawl date recorded on the row.
        Returns:
            findings: The per-site privacy findings for this trial.
        """
        requests = analyzed_trial.analyzed_requests
        cookies = analyzed_trial.analyzed_cookies
        banner = analyzed_trial.banner_finding

        third_party_requests = [request for request in requests if request.is_third_party]
        tracker_requests = [request for request in requests if request.is_tracker]
        third_party_domains = {request.registered_domain for request in third_party_requests}
        tracker_domains = {request.registered_domain for request in tracker_requests}

        classified_cookie_count = sum(1 for cookie in cookies if cookie.classification_matched)
        eprivacy_vendors = tuple(
            sorted(
                {
                    cookie.cookie_vendor or cookie.registered_domain
                    for cookie in cookies
                    if cookie.is_eprivacy_5_3_candidate
                }
            )
        )

        art_9_implicated = site.category in ART9_IMPLICATED_CATEGORIES
        # Scoped to implicated sites: the art_9_ prefix means "on an Art 9 visit".
        tracking_cookie_present = art_9_implicated and any(
            cookie.is_eprivacy_5_3_candidate for cookie in cookies
        )
        tracker_request_present = art_9_implicated and bool(tracker_requests)

        return cls(
            site_id=site.site_id,
            org_name=site.org_name,
            country=site.country,
            category=site.category,
            url=site.url,
            geolocation=analyzed_trial.geolocation,
            trial=analyzed_trial.trial,
            crawl_date=crawl_date,
            blocked=analyzed_trial.blocked,
            total_cookie_count=len(cookies),
            third_party_cookie_count=sum(1 for cookie in cookies if cookie.is_third_party),
            classified_cookie_count=classified_cookie_count,
            unknown_cookie_count=len(cookies) - classified_cookie_count,
            tracking_cookie_count=sum(
                1
                for cookie in cookies
                if cookie.classification_matched and not cookie.is_strictly_necessary
            ),
            eprivacy_5_3_candidate_count=sum(
                1 for cookie in cookies if cookie.is_eprivacy_5_3_candidate
            ),
            eprivacy_5_3_vendors=eprivacy_vendors,
            third_party_request_count=len(third_party_requests),
            third_party_domain_count=len(third_party_domains),
            tracker_request_count=len(tracker_requests),
            tracker_domain_count=len(tracker_domains),
            ep_only_request_count=sum(
                1 for request in tracker_requests if request.tracker_source == "easyprivacy"
            ),
            ddg_only_request_count=sum(
                1 for request in tracker_requests if request.tracker_source == "ddg"
            ),
            both_lists_request_count=sum(
                1 for request in tracker_requests if request.tracker_source == "both"
            ),
            banner_present=banner.banner_present,
            banner_primary_method=banner.primary_method,
            cmp_vendor=banner.cmp_vendor,
            hosting_platform=analyzed_trial.hosting_platform.platform,
            local_storage_key_count=(
                len(analyzed_trial.local_storage.entries)
                if analyzed_trial.local_storage is not None
                else 0
            ),
            art_9_implicated=art_9_implicated,
            art_9_identity_specific=site.category in IDENTITY_SPECIFIC_CATEGORIES,
            art_9_tier=cls.art_9_tier_for_category(site.category),
            art_9_tracking_cookie_present=tracking_cookie_present,
            art_9_tracker_request_present=tracker_request_present,
            art_9_transmission_present=tracking_cookie_present or tracker_request_present,
        )
