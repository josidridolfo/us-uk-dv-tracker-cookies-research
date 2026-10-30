"""
Aggregate-only jurisdiction summary, rendered as a LaTeX table.

JurisdictionSummary compares two explicit runs, each a (crawl-date, geolocation):
one row per run with descriptive counts, percentages, and medians.
No inferential statistics, no confidence intervals.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

import polars as pl

from dv_tracking.analysis.models import SitePrivacyFindings
from dv_tracking.analysis.statistics import DistributionSummary, column_median, percent
from dv_tracking.constants import PUBLIC_SUFFIX_SNAPSHOT, TRACKER_LIST_SNAPSHOTS
from dv_tracking.latex import LatexTable

SUMMARY_CAPTION: str = (
    "Pre-consent third-party tracking on DV support sites, by jurisdiction (aggregate-only)."
)
SUMMARY_LABEL: str = "tab:jurisdiction-summary"

SUMMARY_HEADERS: dict[str, str] = {
    "run_label": "Run",
    "site_count": "Sites",
    "art_9_implicated_count": "Art 9 sites",
    "art_9_exfil_count": "Art 9 exfil",
    "art_9_exfil_pct": "Art 9 exfil %",
    "eprivacy_candidate_site_count": "ePrivacy sites",
    "eprivacy_candidate_pct": "ePrivacy %",
    "banner_api_verified_count": "Banner API sites (TCF-visible subset)",
    "banner_api_verified_pct": "Banner API % (TCF-visible subset)",
    "banner_heuristic_count": "Banner (heuristic) sites",
    "banner_heuristic_pct": "Banner (heuristic) %",
    "cmp_present_count": "CMP present sites (excl. DOM-heuristic)",
    "cmp_present_pct": "CMP present % (excl. DOM-heuristic)",
    "cmp_vendor_observed_count": "CMP vendor-observed sites",
    "cmp_vendor_observed_pct": "CMP vendor-observed %",
    "tracking_despite_cmp_count": "Track despite CMP",
    "tracking_despite_cmp_pct": "Track despite CMP %",
    "tracking_despite_vendor_observed_pct": "Track despite vendor-observed %",
    "banner_present_count": "Consent banner sites (incl. DOM-heuristic)",
    "banner_present_pct": "Consent banner % (incl. DOM-heuristic)",
    "tracking_despite_banner_present_count": "Track despite banner (incl. DOM-heuristic)",
    "tracking_despite_banner_present_pct": "Track despite banner % (incl. DOM-heuristic)",
    "tracker_request_median": "Tracker reqs (median)",
    "third_party_request_median": "3rd-party reqs (median)",
    "total_cookie_median": "Cookies (median)",
    "any_tracker_site_count": "Any-tracker sites",
    "any_tracker_pct": "Any-tracker %",
    "tracker_domain_median": "Tracker domains (median)",
    "tracker_domain_iqr_low": "Tracker domains (Q1)",
    "tracker_domain_iqr_high": "Tracker domains (Q3)",
    "banner_api_verified_site_count": "API-banner sites (TCF-visible subset)",
    "tracking_despite_api_banner_count": "Track despite API banner (TCF-visible subset)",
    "tracking_despite_api_banner_pct": "Track despite API banner % (TCF-visible subset)",
    "banner_heuristic_site_count": "Heuristic-banner sites",
    "tracking_despite_heuristic_banner_pct": "Track despite heuristic banner %",
    "non_essential_cookie_site_count": "Non-essential cookie sites",
    "non_essential_cookie_pct": "Non-essential cookie %",
    "necessary_long_lived_site_count": "Necessary long-lived sites",
    "necessary_long_lived_pct": "Necessary long-lived %",
    "exfiltration_site_count": "Exfiltration sites",
    "exfiltration_pct": "Exfiltration %",
    "exfiltration_edge_median": "Exfiltration edges (median)",
    "third_party_exfil_site_count": "Third-party exfil sites",
    "third_party_exfil_pct": "Third-party exfil %",
    "first_party_cookie_exfil_site_count": "First-party-cookie exfil sites",
    "first_party_cookie_exfil_pct": "First-party-cookie exfil %",
    "advertising_tracker_site_count": "Advertising tracker sites",
    "advertising_tracker_pct": "Advertising tracker %",
    "analytics_tracker_site_count": "Analytics tracker sites",
    "analytics_tracker_pct": "Analytics tracker %",
    "other_tracker_site_count": "Other tracker sites",
    "other_tracker_pct": "Other tracker %",
    "advertising_owner_google_site_count": "Advertising Google sites",
    "advertising_google_pct": "Advertising Google %",
    "advertising_owner_meta_site_count": "Advertising Meta sites",
    "advertising_meta_pct": "Advertising Meta %",
    "first_party_context_tracker_site_count": "First-party-context tracker sites",
    "first_party_context_tracker_pct": "First-party-context tracker %",
    "pure_third_party_tracker_site_count": "Pure third-party tracker sites",
    "pure_third_party_tracker_pct": "Pure third-party tracker %",
}

SUMMARY_COLUMN_FORMATS: dict[str, int] = {
    "art_9_exfil_pct": 1,
    "eprivacy_candidate_pct": 1,
    "banner_api_verified_pct": 1,
    "banner_heuristic_pct": 1,
    "cmp_present_pct": 1,
    "cmp_vendor_observed_pct": 1,
    "tracking_despite_cmp_pct": 1,
    "tracking_despite_vendor_observed_pct": 1,
    "banner_present_pct": 1,
    "tracking_despite_banner_present_pct": 1,
    "tracker_request_median": 1,
    "third_party_request_median": 1,
    "total_cookie_median": 1,
    "any_tracker_pct": 1,
    "tracker_domain_median": 1,
    "tracker_domain_iqr_low": 1,
    "tracker_domain_iqr_high": 1,
    "tracking_despite_api_banner_pct": 1,
    "tracking_despite_heuristic_banner_pct": 1,
    "non_essential_cookie_pct": 1,
    "necessary_long_lived_pct": 1,
    "exfiltration_pct": 1,
    "exfiltration_edge_median": 1,
    "third_party_exfil_pct": 1,
    "first_party_cookie_exfil_pct": 1,
    "advertising_tracker_pct": 1,
    "analytics_tracker_pct": 1,
    "other_tracker_pct": 1,
    "advertising_google_pct": 1,
    "advertising_meta_pct": 1,
    "first_party_context_tracker_pct": 1,
    "pure_third_party_tracker_pct": 1,
}


@dataclass(frozen=True)
class SummaryRun:
    """One run to summarise: its per-site findings plus its (crawl-date, geolocation)."""

    findings: list[SitePrivacyFindings]
    crawl_date: str
    geolocation: str

    @property
    def label(self) -> str:
        """
        The run's display label.

        Args:
            None
        Returns:
            label: The geolocation and crawl date, space-separated.
        """
        return f"{self.geolocation} {self.crawl_date}"


@dataclass(frozen=True)
class GeolocationSummaryRow:
    """Descriptive summary for one run's successful sites."""

    run_label: str
    site_count: int
    art_9_implicated_count: int
    art_9_exfil_count: int
    art_9_exfil_pct: float
    eprivacy_candidate_site_count: int
    eprivacy_candidate_pct: float
    banner_api_verified_count: int
    banner_api_verified_pct: float
    banner_heuristic_count: int
    banner_heuristic_pct: float
    # C6/C7: any CMP present = API-verified union vendor-observed (mutually exclusive tiers).
    cmp_present_count: int
    cmp_present_pct: float
    cmp_vendor_observed_count: int
    cmp_vendor_observed_pct: float
    tracking_despite_cmp_count: int
    tracking_despite_cmp_pct: float
    tracking_despite_vendor_observed_pct: float
    # Companion assessment: any consent banner incl. the DOM heuristic (superset of CMP present).
    banner_present_count: int
    banner_present_pct: float
    tracking_despite_banner_present_count: int
    tracking_despite_banner_present_pct: float
    tracker_request_median: float
    third_party_request_median: float
    total_cookie_median: float
    any_tracker_site_count: int
    any_tracker_pct: float
    tracker_domain_median: float
    tracker_domain_iqr_low: float
    tracker_domain_iqr_high: float
    banner_api_verified_site_count: int
    tracking_despite_api_banner_count: int
    tracking_despite_api_banner_pct: float
    banner_heuristic_site_count: int
    tracking_despite_heuristic_banner_pct: float
    non_essential_cookie_site_count: int
    non_essential_cookie_pct: float
    necessary_long_lived_site_count: int
    necessary_long_lived_pct: float
    exfiltration_site_count: int
    exfiltration_pct: float
    exfiltration_edge_median: float
    # R2 / R3 exfil, both over successful sites (site_count): R1 >= R2 >= R3.
    third_party_exfil_site_count: int
    third_party_exfil_pct: float
    first_party_cookie_exfil_site_count: int
    first_party_cookie_exfil_pct: float
    advertising_tracker_site_count: int
    advertising_tracker_pct: float
    analytics_tracker_site_count: int
    analytics_tracker_pct: float
    other_tracker_site_count: int
    other_tracker_pct: float
    advertising_owner_google_site_count: int
    advertising_google_pct: float
    advertising_owner_meta_site_count: int
    advertising_meta_pct: float
    # Owner-linked split of the third-party trackers, over successful sites (site_count).
    first_party_context_tracker_site_count: int
    first_party_context_tracker_pct: float
    pure_third_party_tracker_site_count: int
    pure_third_party_tracker_pct: float

    @classmethod
    def from_run(cls, run_label: str, cell: pl.DataFrame) -> GeolocationSummaryRow:
        """
        Summarises one run's site-level frame into a descriptive row.

        C6 counts any CMP present = API-verified union vendor-observed (exclusive tiers).

        Args:
            run_label: The run's display label.
            cell: The successful site-level findings for this run.
        Returns:
            row: The descriptive summary row.
        """
        site_count = cell.height
        implicated = SitePrivacyFindings.art_9_denominator(cell)
        art_9_exfil = int(cell.filter(pl.col("art_9_exfil")).height)
        eprivacy = int(cell.filter(pl.col("eprivacy_5_3_candidate_count") > 0).height)
        banner_api_verified = int(cell.filter(pl.col("banner_api_verified")).height)
        banner_heuristic = int(cell.filter(pl.col("banner_heuristic_only")).height)
        vendor_observed = int(
            cell.filter(pl.col("cmp_confidence_tier") == "vendor_observed").height
        )
        # api_verified (any-True) and vendor_observed (longest-dwell tier) are separate
        # collapse bases, so count the union directly rather than summing the two.
        cmp_present_mask = pl.col("banner_api_verified") | (
            pl.col("cmp_confidence_tier") == "vendor_observed"
        )
        cmp_present = int(cell.filter(cmp_present_mask).height)
        tracking_despite_cmp = int(
            cell.filter(cmp_present_mask & (pl.col("tracker_request_count") > 0)).height
        )
        tracking_despite_vendor_observed = int(
            cell.filter(
                (pl.col("cmp_confidence_tier") == "vendor_observed")
                & (pl.col("tracker_request_count") > 0)
            ).height
        )
        # With-DOM-heuristic companion: any consent banner (superset of CMP present).
        banner_present = int(cell.filter(pl.col("banner_present")).height)
        tracking_despite_banner_present = int(
            cell.filter(pl.col("banner_present") & (pl.col("tracker_request_count") > 0)).height
        )
        any_tracker = int(cell.filter(pl.col("tracker_request_count") > 0).height)
        tracker_domains = DistributionSummary.from_values(
            cell.get_column("tracker_domain_count").to_list()
        )
        tracking_despite_api = int(
            cell.filter(
                pl.col("banner_api_verified") & (pl.col("tracker_request_count") > 0)
            ).height
        )
        tracking_despite_heuristic = int(
            cell.filter(
                pl.col("banner_heuristic_only") & (pl.col("tracker_request_count") > 0)
            ).height
        )
        non_essential = int(cell.filter(pl.col("non_essential_cookie_count") > 0).height)
        necessary_long_lived = int(
            cell.filter(pl.col("necessary_but_long_lived_cookie_count") > 0).height
        )
        exfiltration = int(cell.filter(pl.col("has_exfiltration")).height)
        third_party_exfil = int(cell.filter(pl.col("third_party_exfil")).height)
        first_party_cookie_exfil = int(cell.filter(pl.col("first_party_cookie_exfil")).height)
        advertising = int(cell.filter(pl.col("advertising_tracker_request_count") > 0).height)
        analytics = int(cell.filter(pl.col("analytics_tracker_request_count") > 0).height)
        other_tracker = int(cell.filter(pl.col("other_tracker_request_count") > 0).height)
        advertising_google = int(cell.filter(pl.col("advertising_google_request_count") > 0).height)
        advertising_meta = int(cell.filter(pl.col("advertising_meta_request_count") > 0).height)
        first_party_context = int(cell.filter(pl.col("has_first_party_context_tracker")).height)
        pure_third_party = int(cell.filter(pl.col("has_pure_third_party_tracker")).height)
        return cls(
            run_label=run_label,
            site_count=site_count,
            art_9_implicated_count=implicated,
            art_9_exfil_count=art_9_exfil,
            art_9_exfil_pct=percent(art_9_exfil, implicated),
            eprivacy_candidate_site_count=eprivacy,
            eprivacy_candidate_pct=percent(eprivacy, site_count),
            banner_api_verified_count=banner_api_verified,
            banner_api_verified_pct=percent(banner_api_verified, site_count),
            banner_heuristic_count=banner_heuristic,
            banner_heuristic_pct=percent(banner_heuristic, site_count),
            cmp_present_count=cmp_present,
            cmp_present_pct=percent(cmp_present, site_count),
            cmp_vendor_observed_count=vendor_observed,
            cmp_vendor_observed_pct=percent(vendor_observed, site_count),
            tracking_despite_cmp_count=tracking_despite_cmp,
            tracking_despite_cmp_pct=percent(tracking_despite_cmp, cmp_present),
            tracking_despite_vendor_observed_pct=percent(
                tracking_despite_vendor_observed, vendor_observed
            ),
            banner_present_count=banner_present,
            banner_present_pct=percent(banner_present, site_count),
            tracking_despite_banner_present_count=tracking_despite_banner_present,
            tracking_despite_banner_present_pct=percent(
                tracking_despite_banner_present, banner_present
            ),
            tracker_request_median=column_median(cell, "tracker_request_count"),
            third_party_request_median=column_median(cell, "third_party_request_count"),
            total_cookie_median=column_median(cell, "total_cookie_count"),
            any_tracker_site_count=any_tracker,
            any_tracker_pct=percent(any_tracker, site_count),
            tracker_domain_median=tracker_domains.median,
            tracker_domain_iqr_low=tracker_domains.iqr_low,
            tracker_domain_iqr_high=tracker_domains.iqr_high,
            banner_api_verified_site_count=banner_api_verified,
            tracking_despite_api_banner_count=tracking_despite_api,
            tracking_despite_api_banner_pct=percent(tracking_despite_api, banner_api_verified),
            banner_heuristic_site_count=banner_heuristic,
            tracking_despite_heuristic_banner_pct=percent(
                tracking_despite_heuristic, banner_heuristic
            ),
            non_essential_cookie_site_count=non_essential,
            non_essential_cookie_pct=percent(non_essential, site_count),
            necessary_long_lived_site_count=necessary_long_lived,
            necessary_long_lived_pct=percent(necessary_long_lived, site_count),
            exfiltration_site_count=exfiltration,
            exfiltration_pct=percent(exfiltration, site_count),
            exfiltration_edge_median=column_median(cell, "exfiltration_edge_count"),
            third_party_exfil_site_count=third_party_exfil,
            third_party_exfil_pct=percent(third_party_exfil, site_count),
            first_party_cookie_exfil_site_count=first_party_cookie_exfil,
            first_party_cookie_exfil_pct=percent(first_party_cookie_exfil, site_count),
            advertising_tracker_site_count=advertising,
            advertising_tracker_pct=percent(advertising, site_count),
            analytics_tracker_site_count=analytics,
            analytics_tracker_pct=percent(analytics, site_count),
            other_tracker_site_count=other_tracker,
            other_tracker_pct=percent(other_tracker, site_count),
            advertising_owner_google_site_count=advertising_google,
            # Owner rates are over the advertising-band sites, not all sites.
            advertising_google_pct=percent(advertising_google, advertising),
            advertising_owner_meta_site_count=advertising_meta,
            advertising_meta_pct=percent(advertising_meta, advertising),
            first_party_context_tracker_site_count=first_party_context,
            first_party_context_tracker_pct=percent(first_party_context, site_count),
            pure_third_party_tracker_site_count=pure_third_party,
            pure_third_party_tracker_pct=percent(pure_third_party, site_count),
        )


@dataclass(frozen=True)
class JurisdictionSummary:
    """Aggregate-only descriptive summary comparing two runs, one row per run."""

    total_site_count: int
    rows: tuple[GeolocationSummaryRow, ...]

    @classmethod
    def from_runs(cls, run_a: SummaryRun, run_b: SummaryRun) -> JurisdictionSummary:
        """
        Builds the summary from two explicit runs, in (run_a, run_b) order.

        Each run is collapsed to one row per site at the longest dwell, blocked
        sites dropped; a run with no successful sites contributes no row.

        Args:
            run_a: The first run to summarise.
            run_b: The second run to summarise.
        Returns:
            summary: The aggregate summary over both runs' successful sites.
        """
        rows: list[GeolocationSummaryRow] = []
        total = 0
        for run in (run_a, run_b):
            frame = SitePrivacyFindings.site_level_frame(run.findings)
            if frame.is_empty():
                continue
            successful = frame.filter(~pl.col("blocked"))
            if successful.is_empty():
                continue
            rows.append(GeolocationSummaryRow.from_run(run.label, successful))
            total += successful.height
        return cls(total_site_count=total, rows=tuple(rows))

    def to_dataframe(self) -> pl.DataFrame:
        """
        The summary as a DataFrame, one row per run.

        Args:
            None
        Returns:
            frame: One row per run with columns matching the row fields; empty when no rows.
        """
        if not self.rows:
            return pl.DataFrame()
        return pl.DataFrame([asdict(row) for row in self.rows], infer_schema_length=None)

    def provenance_comment(self) -> str:
        """
        The reproducibility provenance as LaTeX comment lines preceding the table.

        Records both run labels with their site and Art-9-implicated denominators,
        the pinned tracker-list snapshot versions, the pinned public-suffix snapshot
        every party judgement rests on, and the per-trial collapse rule.

        Args:
            None
        Returns:
            comment: The %-prefixed provenance lines, newline-joined.
        """
        lines = ["% jurisdiction summary provenance"]
        for row in self.rows:
            lines.append(
                f"% run {row.run_label}: {row.site_count} sites, "
                f"{row.art_9_implicated_count} Art-9-implicated (exfil denominator)"
            )
        for snapshot in TRACKER_LIST_SNAPSHOTS:
            commit = snapshot.pinned_commit or "n/a"
            lines.append(
                f"% tracker list {snapshot.name}: snapshot {snapshot.snapshot_date}, commit {commit}"
            )
        lines.append(
            f"% public suffix list: version {PUBLIC_SUFFIX_SNAPSHOT.list_version}, "
            f"commit {PUBLIC_SUFFIX_SNAPSHOT.pinned_commit}, "
            f"private domains {PUBLIC_SUFFIX_SNAPSHOT.include_private_domains}"
        )
        lines.append(
            "% per-trial rows collapse to one row per site at the highest non-blocked trial "
            "(longest dwell); booleans any-True, blocked all-True, blocked dropped before rates"
        )
        return "\n".join(lines)

    def to_latex_table(self, preamble: str | None = None) -> LatexTable:
        """
        The summary as a configured LatexTable.

        Args:
            preamble: Text emitted before the table environment, or None.
        Returns:
            table: The LatexTable over the summary frame with the study's captions and headers.
        """
        return LatexTable(
            self.to_dataframe(),
            caption=SUMMARY_CAPTION,
            label=SUMMARY_LABEL,
            column_formats=SUMMARY_COLUMN_FORMATS,
            header_overrides=SUMMARY_HEADERS,
            preamble=preamble,
        )

    def write_latex(self, path: Path) -> Path:
        """
        Writes the summary table, preceded by its provenance comment, as a LaTeX file.

        Args:
            path: The file path to write.
        Returns:
            path: The path written.
        """
        return self.to_latex_table(preamble=self.provenance_comment()).write(path)

    def write_parquet(self, path: Path) -> Path:
        """
        Writes the summary frame, one row per run, as a parquet file.

        Args:
            path: The file path to write.
        Returns:
            path: The path written.
        """
        self.to_dataframe().write_parquet(path)
        return path
