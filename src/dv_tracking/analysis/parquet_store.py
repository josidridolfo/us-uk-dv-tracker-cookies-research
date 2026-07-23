"""
Parquet writers for the analysis-facing outputs (v1).

One parquet per (output, crawl_date):
    per_site_privacy_findings_<crawl_date>.parquet   one row per (site, trial)
    per_cookie_findings_<crawl_date>.parquet         one row per cookie (classification)
    cookie_exfiltration_edges_<crawl_date>.parquet   one row per exfiltration edge

Each per-cookie / per-edge row is stamped with (site_id, geolocation, trial,
crawl_date) at the write layer so the pooled row stays attributable to its trial.
The per-cookie classification census excludes the raw cookie value.
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

import polars as pl

from dv_tracking.analysis.cookie_exfiltration import ExfilEdge
from dv_tracking.analysis.models import SitePrivacyFindings
from dv_tracking.classify.models import AnalyzedTrial

SITE_FINDINGS_PREFIX: str = "per_site_privacy_findings"
COOKIE_FINDINGS_PREFIX: str = "per_cookie_findings"
COOKIE_EXFILTRATION_PREFIX: str = "cookie_exfiltration_edges"


class FindingsParquetStore:
    """Writes the per-site findings and per-cookie exfiltration parquet outputs (v1)."""

    def __init__(self, out_dir: Path) -> None:
        """
        Bind the store to an output directory.

        Args:
            out_dir: The directory the parquet files are written under.
        Returns:
            None
        """
        self.out_dir = out_dir

    def save_site_findings(self, findings: list[SitePrivacyFindings], crawl_date: str) -> Path:
        """
        Write the per-site findings for one crawl date as a date-suffixed parquet.

        Args:
            findings: The per-site-per-trial findings rows.
            crawl_date: The crawl date used in the filename.
        Returns:
            path: The parquet path written.
        """
        self.out_dir.mkdir(parents=True, exist_ok=True)
        path = self.out_dir / f"{SITE_FINDINGS_PREFIX}_{crawl_date}.parquet"
        SitePrivacyFindings.to_dataframe(findings).write_parquet(path)
        return path

    def save_cookie_findings(self, analyzed_trials: list[AnalyzedTrial], crawl_date: str) -> Path:
        """
        Write the per-cookie classification census for one crawl date as a date-suffixed parquet.

        Args:
            analyzed_trials: The analyzed trials whose cookies are enumerated.
            crawl_date: The crawl date stamped on every cookie and used in the filename.
        Returns:
            path: The parquet path written.
        """
        self.out_dir.mkdir(parents=True, exist_ok=True)
        rows = []
        for analyzed in analyzed_trials:
            for cookie in analyzed.analyzed_cookies:
                rows.append(
                    {
                        "name": cookie.name,
                        "domain": cookie.domain,
                        "registered_domain": cookie.registered_domain,
                        "is_third_party": cookie.is_third_party,
                        "is_session": cookie.is_session,
                        "is_secure": cookie.is_secure,
                        "is_http_only": cookie.is_http_only,
                        "same_site": cookie.same_site,
                        "classification_matched": cookie.classification_matched,
                        "cookie_category": cookie.cookie_category,
                        "cookie_vendor": cookie.cookie_vendor,
                        "is_strictly_necessary": cookie.is_strictly_necessary,
                        "is_eprivacy_5_3_candidate": cookie.is_eprivacy_5_3_candidate,
                        "has_identifier_value": cookie.has_identifier_value,
                        "site_id": analyzed.site_id,
                        "geolocation": analyzed.geolocation,
                        "trial": analyzed.trial,
                        "crawl_date": crawl_date,
                    }
                )
        path = self.out_dir / f"{COOKIE_FINDINGS_PREFIX}_{crawl_date}.parquet"
        frame = pl.DataFrame(rows, infer_schema_length=None) if rows else pl.DataFrame()
        frame.write_parquet(path)
        return path

    def save_exfiltration_edges(self, analyzed_trials: list[AnalyzedTrial], crawl_date: str) -> Path:
        """
        Write the URL-channel exfiltration edges for one crawl date as a date-suffixed parquet.

        Args:
            analyzed_trials: The analyzed trials scanned for exfiltration edges.
            crawl_date: The crawl date stamped on every edge and used in the filename.
        Returns:
            path: The parquet path written.
        """
        self.out_dir.mkdir(parents=True, exist_ok=True)
        rows = []
        for analyzed in analyzed_trials:
            for edge in ExfilEdge.find_all(analyzed):
                row = asdict(edge)
                row["site_id"] = analyzed.site_id
                row["geolocation"] = analyzed.geolocation
                row["trial"] = analyzed.trial
                row["crawl_date"] = crawl_date
                rows.append(row)
        path = self.out_dir / f"{COOKIE_EXFILTRATION_PREFIX}_{crawl_date}.parquet"
        frame = pl.DataFrame(rows, infer_schema_length=None) if rows else pl.DataFrame()
        frame.write_parquet(path)
        return path
