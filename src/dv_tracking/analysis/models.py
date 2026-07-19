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
        )
