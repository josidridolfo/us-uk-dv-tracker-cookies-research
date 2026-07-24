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
from dv_tracking.analysis.statistics import DistributionSummary, proportion
from dv_tracking.latex import LatexTable

SUMMARY_CAPTION: str = (
    "Pre-consent third-party tracking on DV support sites, by jurisdiction (aggregate-only)."
)
SUMMARY_LABEL: str = "tab:jurisdiction-summary"

SUMMARY_HEADERS: dict[str, str] = {
    "run_label": "Run",
    "site_count": "Sites",
    "art_9_implicated_count": "Art 9 sites",
    "art_9_transmission_count": "Art 9 transmit",
    "art_9_transmission_pct": "Art 9 transmit %",
    "eprivacy_candidate_site_count": "ePrivacy sites",
    "eprivacy_candidate_pct": "ePrivacy %",
    "banner_present_count": "Banner sites",
    "banner_present_pct": "Banner %",
    "tracker_request_median": "Tracker reqs (median)",
    "third_party_request_median": "3rd-party reqs (median)",
    "total_cookie_median": "Cookies (median)",
}

SUMMARY_COLUMN_FORMATS: dict[str, int] = {
    "art_9_transmission_pct": 1,
    "eprivacy_candidate_pct": 1,
    "banner_present_pct": 1,
    "tracker_request_median": 1,
    "third_party_request_median": 1,
    "total_cookie_median": 1,
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
    art_9_transmission_count: int
    art_9_transmission_pct: float
    eprivacy_candidate_site_count: int
    eprivacy_candidate_pct: float
    banner_present_count: int
    banner_present_pct: float
    tracker_request_median: float
    third_party_request_median: float
    total_cookie_median: float

    @classmethod
    def from_run(cls, run_label: str, cell: pl.DataFrame) -> GeolocationSummaryRow:
        """
        Summarises one run's site-level frame into a descriptive row.

        Art 9 transmission is a rate over the Art-9-implicated denominator, not
        all sites; ePrivacy and banner rates are over all successful sites.

        Args:
            run_label: The run's display label.
            cell: The successful site-level findings for this run.
        Returns:
            row: The descriptive summary row.
        """
        site_count = cell.height
        implicated = SitePrivacyFindings.art_9_denominator(cell)
        transmission = int(cell.filter(pl.col("art_9_transmission_present")).height)
        eprivacy = int(cell.filter(pl.col("eprivacy_5_3_candidate_count") > 0).height)
        banner = int(cell.filter(pl.col("banner_present")).height)
        return cls(
            run_label=run_label,
            site_count=site_count,
            art_9_implicated_count=implicated,
            art_9_transmission_count=transmission,
            art_9_transmission_pct=proportion(transmission, implicated) * 100,
            eprivacy_candidate_site_count=eprivacy,
            eprivacy_candidate_pct=proportion(eprivacy, site_count) * 100,
            banner_present_count=banner,
            banner_present_pct=proportion(banner, site_count) * 100,
            tracker_request_median=DistributionSummary.from_values(
                cell.get_column("tracker_request_count").to_list()
            ).median,
            third_party_request_median=DistributionSummary.from_values(
                cell.get_column("third_party_request_count").to_list()
            ).median,
            total_cookie_median=DistributionSummary.from_values(
                cell.get_column("total_cookie_count").to_list()
            ).median,
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

    def to_latex_table(self) -> LatexTable:
        """
        The summary as a configured LatexTable.

        Args:
            None
        Returns:
            table: The LatexTable over the summary frame with the study's captions and headers.
        """
        return LatexTable(
            self.to_dataframe(),
            caption=SUMMARY_CAPTION,
            label=SUMMARY_LABEL,
            column_formats=SUMMARY_COLUMN_FORMATS,
            header_overrides=SUMMARY_HEADERS,
        )

    def write_latex(self, path: Path) -> Path:
        """
        Writes the summary table as a LaTeX file.

        Args:
            path: The file path to write.
        Returns:
            path: The path written.
        """
        return self.to_latex_table().write(path)
