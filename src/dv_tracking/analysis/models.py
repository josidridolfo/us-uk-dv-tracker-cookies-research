"""
Per-site privacy findings.

A SitePrivacyFindings row aggregates one analyzed trial into compliance-style
counts and flags for one site at one trial. It is the study's per-cell unit of
analysis; identity and category metadata are carried from the corpus site.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import polars as pl

from dv_tracking.analysis.cookie_exfiltration import ExfilEdge
from dv_tracking.classify.models import AnalyzedRequest, AnalyzedTrial
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

# Advertising / cross-site band: DDG categories that mark ad-motivated or pixel tracking.
ADVERTISING_TRACKER_CATEGORIES: frozenset[str] = frozenset(
    {"Advertising", "Ad Motivated Tracking", "Ad Fraud", "Action Pixels"}
)
# Analytics / measurement band: DDG categories for audience and behaviour measurement.
# The two bands overlap (a request may carry both) and are never summed.
ANALYTICS_TRACKER_CATEGORIES: frozenset[str] = frozenset(
    {"Analytics", "Audience Measurement", "Third-Party Analytics Marketing", "Session Replay"}
)

GOOGLE_OWNER_NAME: str = "Google"
META_OWNER_NAMES: frozenset[str] = frozenset({"Facebook", "Meta"})


def in_band(request: AnalyzedRequest, band: frozenset[str]) -> bool:
    """True when a request carries any tracker category in the given band."""
    return bool(set(request.tracker_categories) & band)


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
    non_essential_cookie_count: int
    necessary_but_long_lived_cookie_count: int
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
    # Spec-verified CMP signal (TCF/USP/GPP APIs or a Consent-O-Matic selector match).
    banner_api_verified: bool
    # DOM/hostname-only detection with no API confirmation; the unvalidated bucket.
    banner_heuristic_only: bool
    banner_primary_method: str | None
    cmp_vendor: str | None
    # Confidence ladder: api_verified > vendor_observed > heuristic_only > none.
    cmp_confidence_tier: str
    hosting_platform: str
    # First-party (top-origin) localStorage only; cross-origin iframe stores are out of scope.
    first_party_local_storage_key_count: int

    art_9_implicated: bool
    art_9_identity_specific: bool
    art_9_tier: str
    art_9_tracking_cookie_present: bool
    art_9_tracker_request_present: bool
    art_9_transmission_present: bool

    exfiltration_edge_count: int = 0
    has_exfiltration: bool = False

    # Three overlapping bands over the third-party trackers; longest-dwell counts, never summed.
    advertising_tracker_request_count: int = 0
    analytics_tracker_request_count: int = 0
    # "Other": a third-party tracker in neither band, including EP-only uncategorised hits.
    other_tracker_request_count: int = 0
    # Owner concentration over the advertising band (two-vendor Google / Meta split).
    advertising_google_request_count: int = 0
    advertising_meta_request_count: int = 0

    @staticmethod
    def art_9_tier_for_category(category: str) -> str:
        """
        Labels the Art 9 tier a site category falls into.

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
    def to_dataframe(cls, findings: list[SitePrivacyFindings]) -> pl.DataFrame:
        """
        Builds a flat per-(site, trial) DataFrame from a list of findings.

        Args:
            findings: The per-site-per-trial findings rows.
        Returns:
            frame: One row per findings record; empty when there are no findings.
        """
        if not findings:
            return pl.DataFrame()
        return pl.DataFrame([asdict(row) for row in findings], infer_schema_length=None)

    @classmethod
    def site_level_frame(cls, findings: list[SitePrivacyFindings]) -> pl.DataFrame:
        """
        Collapses the per-trial rows to one row per site at the longest-dwell visit.

        Numeric and identity columns take the highest-numbered non-blocked trial's
        value; boolean flags collapse by any-True across trials; blocked by all-True.

        Args:
            findings: The per-site-per-trial findings rows.
        Returns:
            frame: One row per (site_id, geolocation); empty when there are no findings.
        """
        frame = cls.to_dataframe(findings)
        if frame.is_empty():
            return frame
        # Non-blocked first, then longest dwell (highest trial number).
        order = [pl.col("blocked"), pl.col("trial")]
        descending = [False, True]
        aggregations = []
        for column in frame.columns:
            if column in ("site_id", "geolocation"):
                continue
            if column == "blocked":
                aggregations.append(pl.col(column).all().alias(column))
            elif frame.schema[column] == pl.Boolean:
                aggregations.append(pl.col(column).any().alias(column))
            else:
                # Longest-dwell collapse: numeric/identity cols take the highest-numbered
                # non-blocked trial; booleans any-True, blocked all-True.
                aggregations.append(
                    pl.col(column).sort_by(order, descending=descending).first().alias(column)
                )
        return frame.group_by("site_id", "geolocation").agg(aggregations)

    @classmethod
    def art_9_denominator(cls, frame: pl.DataFrame) -> int:
        """
        The denominator for Art 9 transmission rates: the count of implicated sites.

        Args:
            frame: A site-level findings frame.
        Returns:
            denominator: The number of Art-9-implicated sites, or the row count if the column is absent.
        """
        if "art_9_implicated" not in frame.columns:
            return frame.height
        return int(frame.filter(pl.col("art_9_implicated")).height)

    @classmethod
    def from_trial(
        cls, analyzed_trial: AnalyzedTrial, site: DVSupportSite, *, crawl_date: str = ""
    ) -> SitePrivacyFindings:
        """
        Aggregates one analyzed trial into a per-site findings row.

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

        third_party_trackers = [
            request for request in requests if request.is_third_party and request.is_tracker
        ]
        advertising_trackers = [
            request
            for request in third_party_trackers
            if in_band(request, ADVERTISING_TRACKER_CATEGORIES)
        ]
        advertising_tracker_request_count = len(advertising_trackers)
        analytics_tracker_request_count = sum(
            1 for request in third_party_trackers if in_band(request, ANALYTICS_TRACKER_CATEGORIES)
        )
        other_tracker_request_count = sum(
            1
            for request in third_party_trackers
            if not in_band(request, ADVERTISING_TRACKER_CATEGORIES)
            and not in_band(request, ANALYTICS_TRACKER_CATEGORIES)
        )
        advertising_google_request_count = sum(
            1 for request in advertising_trackers if request.tracker_owner == GOOGLE_OWNER_NAME
        )
        advertising_meta_request_count = sum(
            1 for request in advertising_trackers if request.tracker_owner in META_OWNER_NAMES
        )

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

        # Category-derived visit context (observed behaviour), not a legal Art 9 determination.
        art_9_implicated = site.category in ART9_IMPLICATED_CATEGORIES
        # Scoped to implicated sites: the art_9_ prefix means "on an Art 9 visit".
        # ePrivacy 5(3) candidates only (analytics/marketing); narrower than tracking_cookie_count.
        tracking_cookie_present = art_9_implicated and any(
            cookie.is_eprivacy_5_3_candidate for cookie in cookies
        )
        tracker_request_present = art_9_implicated and bool(tracker_requests)

        banner_api_verified = (
            banner.tcf_present
            or banner.consentomatic_matched
            or banner.usp_api_present
            or banner.gpp_api_present
        )
        vendor_observed = banner.hostname_match is not None
        # A pinned CMP host loading (vendor_observed) is a firmer tell than the bare
        # DOM/z-index heuristic, so it no longer collapses into the heuristic bucket.
        if banner_api_verified:
            cmp_confidence_tier = "api_verified"
        elif vendor_observed:
            cmp_confidence_tier = "vendor_observed"
        elif banner.banner_present:
            cmp_confidence_tier = "heuristic_only"
        else:
            cmp_confidence_tier = "none"
        banner_heuristic_only = cmp_confidence_tier == "heuristic_only"

        edges = ExfilEdge.find_all(analyzed_trial)

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
            # classification_matched and not strictly-necessary: includes analytics/marketing/
            # preferences and matched-but-unmapped; broader than the ePrivacy 5(3) subset.
            tracking_cookie_count=sum(
                1
                for cookie in cookies
                if cookie.classification_matched and not cookie.is_strictly_necessary
            ),
            non_essential_cookie_count=sum(1 for cookie in cookies if not cookie.is_essential),
            necessary_but_long_lived_cookie_count=sum(
                1
                for cookie in cookies
                if cookie.is_strictly_necessary and not cookie.is_essential
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
            banner_api_verified=banner_api_verified,
            banner_heuristic_only=banner_heuristic_only,
            banner_primary_method=banner.primary_method,
            cmp_vendor=banner.cmp_vendor,
            cmp_confidence_tier=cmp_confidence_tier,
            hosting_platform=analyzed_trial.hosting_platform.platform,
            first_party_local_storage_key_count=(
                len(analyzed_trial.local_storage.entries)
                if analyzed_trial.local_storage is not None
                else 0
            ),
            art_9_implicated=art_9_implicated,
            art_9_identity_specific=site.category in IDENTITY_SPECIFIC_CATEGORIES,
            art_9_tier=cls.art_9_tier_for_category(site.category),
            art_9_tracking_cookie_present=tracking_cookie_present,
            art_9_tracker_request_present=tracker_request_present,
            # Observed behaviour: a tracker request (transmission) or a tracking cookie being set
            # (local storage, not itself transmitted); not a compliance verdict.
            art_9_transmission_present=tracking_cookie_present or tracker_request_present,
            exfiltration_edge_count=len(edges),
            has_exfiltration=bool(edges),
            advertising_tracker_request_count=advertising_tracker_request_count,
            analytics_tracker_request_count=analytics_tracker_request_count,
            other_tracker_request_count=other_tracker_request_count,
            advertising_google_request_count=advertising_google_request_count,
            advertising_meta_request_count=advertising_meta_request_count,
        )


def longest_dwell_trials(frame: pl.DataFrame) -> pl.DataFrame:
    """
    The one (site, geo, trial) triple to keep per site: the longest successful dwell.

    Request-level twin of SitePrivacyFindings.site_level_frame's collapse rule: over
    non-blocked rows only, take the highest trial number per (site_id, geolocation).
    All-blocked sites have no non-blocked row and so drop out, matching summary's set.

    Args:
        frame: A per-request findings frame with site_id, geolocation, trial, blocked.
    Returns:
        selector: One row per surviving (site_id, geolocation) with its kept trial;
            empty (with those columns) when the frame is empty.
    """
    if frame.is_empty():
        return pl.DataFrame(schema={"site_id": pl.String, "geolocation": pl.String, "trial": pl.Int64})
    return (
        frame.filter(~pl.col("blocked"))
        .group_by(["site_id", "geolocation"])
        .agg(pl.col("trial").max())
    )
