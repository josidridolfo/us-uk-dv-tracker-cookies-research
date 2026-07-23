"""
Aggregate-only end-of-crawl summary report.

CrawlSummary describes one crawl from its per-site findings: per-geolocation
site counts, tracker/request medians, the Art 9 transmission rate over the
implicated denominator, banner/CMP coverage, and the hosting-platform
breakdown. Descriptive only; no inferential statistics; no organisation named.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import polars as pl

from dv_tracking.analysis.models import SitePrivacyFindings
from dv_tracking.analysis.statistics import DistributionSummary, proportion


@dataclass(frozen=True)
class GeolocationSummary:
    """Descriptive summary for one geolocation's successful sites."""

    geolocation: str
    site_count: int
    art_9_implicated_count: int
    art_9_transmission_count: int
    eprivacy_candidate_site_count: int
    banner_present_count: int
    tracker_request_median: float
    third_party_request_median: float
    cmp_vendor_counts: tuple[tuple[str, int], ...]
    hosting_platform_counts: tuple[tuple[str, int], ...]

    def to_markdown(self) -> str:
        """
        Render this geolocation's summary as a markdown block.

        Args:
            None
        Returns:
            markdown: The formatted markdown block.
        """
        cmp = ", ".join(f"{vendor}={count}" for vendor, count in self.cmp_vendor_counts) or "(none)"
        platforms = ", ".join(
            f"{platform}={count}" for platform, count in self.hosting_platform_counts
        ) or "(none)"
        transmission_rate = proportion(self.art_9_transmission_count, self.art_9_implicated_count)
        eprivacy_rate = proportion(self.eprivacy_candidate_site_count, self.site_count)
        banner_rate = proportion(self.banner_present_count, self.site_count)
        return (
            f"## {self.geolocation}\n"
            f"- successful sites: {self.site_count}\n"
            f"- Art 9 transmission: {self.art_9_transmission_count}/{self.art_9_implicated_count} "
            f"implicated sites ({transmission_rate * 100:.1f}%)\n"
            f"- ePrivacy 5(3) candidate sites: {self.eprivacy_candidate_site_count}/{self.site_count} "
            f"({eprivacy_rate * 100:.1f}%)\n"
            f"- banner present: {self.banner_present_count}/{self.site_count} "
            f"({banner_rate * 100:.1f}%)\n"
            f"- tracker requests per site: median {self.tracker_request_median:.1f}\n"
            f"- third-party requests per site: median {self.third_party_request_median:.1f}\n"
            f"- CMP vendors: {cmp}\n"
            f"- hosting platforms: {platforms}\n"
        )


@dataclass(frozen=True)
class CrawlSummary:
    """Aggregate-only descriptive summary of one crawl."""

    crawl_date: str
    total_site_count: int
    cells: tuple[GeolocationSummary, ...]

    @staticmethod
    def count_column(frame: pl.DataFrame, column: str, *, drop_none: bool = False) -> tuple:
        """
        Count the values of a column, sorted by descending count then value.

        Args:
            frame: The site-level frame.
            column: The column to count.
            drop_none: Whether to drop None values before counting.
        Returns:
            counts: A tuple of (value, count) pairs.
        """
        values = frame.get_column(column).to_list()
        if drop_none:
            values = [value for value in values if value is not None]
        counter = Counter(str(value) for value in values)
        return tuple(sorted(counter.items(), key=lambda item: (-item[1], item[0])))

    @classmethod
    def from_findings(cls, findings: list[SitePrivacyFindings]) -> CrawlSummary:
        """
        Build the crawl summary from a list of per-site findings.

        Args:
            findings: The per-site-per-trial findings rows.
        Returns:
            summary: The aggregate crawl summary over the successful sites.
        """
        crawl_date = findings[0].crawl_date if findings else ""
        frame = SitePrivacyFindings.site_level_frame(findings)
        if frame.is_empty():
            return cls(crawl_date=crawl_date, total_site_count=0, cells=())
        successful = frame.filter(~pl.col("blocked"))
        geolocations = sorted(
            {str(value) for value in successful.get_column("geolocation").to_list() if value}
        )
        cells = []
        for geolocation in geolocations:
            cell = successful.filter(pl.col("geolocation") == geolocation)
            cells.append(
                GeolocationSummary(
                    geolocation=geolocation,
                    site_count=cell.height,
                    art_9_implicated_count=SitePrivacyFindings.art_9_denominator(cell),
                    art_9_transmission_count=int(
                        cell.filter(pl.col("art_9_transmission_present")).height
                    ),
                    eprivacy_candidate_site_count=int(
                        cell.filter(pl.col("eprivacy_5_3_candidate_count") > 0).height
                    ),
                    banner_present_count=int(cell.filter(pl.col("banner_present")).height),
                    tracker_request_median=DistributionSummary.from_values(
                        cell.get_column("tracker_request_count").to_list()
                    ).median,
                    third_party_request_median=DistributionSummary.from_values(
                        cell.get_column("third_party_request_count").to_list()
                    ).median,
                    cmp_vendor_counts=cls.count_column(cell, "cmp_vendor", drop_none=True),
                    hosting_platform_counts=cls.count_column(cell, "hosting_platform"),
                )
            )
        return cls(crawl_date=crawl_date, total_site_count=successful.height, cells=tuple(cells))

    def to_markdown(self) -> str:
        """
        Render the full crawl summary as markdown.

        Args:
            None
        Returns:
            markdown: The formatted markdown report.
        """
        header = (
            f"# Crawl summary: {self.crawl_date or '(unknown date)'}\n\n"
            f"successful sites: {self.total_site_count}\n\n"
        )
        return header + "\n".join(cell.to_markdown() for cell in self.cells)

    def write(self, path: Path) -> Path:
        """
        Write the markdown summary to a path.

        Args:
            path: The file path to write.
        Returns:
            path: The path written.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_markdown(), encoding="utf-8")
        return path
