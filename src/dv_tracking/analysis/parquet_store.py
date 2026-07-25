"""
Parquet writers for the analysis-facing outputs.

One parquet per (output, crawl_date, geolocation):
    per_site_privacy_findings_<crawl_date>_<geolocation>.parquet   one row per (site, trial)
    per_cookie_findings_<crawl_date>_<geolocation>.parquet         one row per cookie (classification)
    per_request_findings_<crawl_date>_<geolocation>.parquet        one row per request
    cookie_exfiltration_edges_<crawl_date>_<geolocation>.parquet   one row per exfiltration edge

Each per-cookie / per-request / per-edge row is stamped with (site_id, geolocation,
trial, crawl_date, crawl_start_utc) at the write layer so the pooled row stays attributable
to its trial and fully self-auditable (the essential-cookie cutoff resolves from crawl_start_utc).
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
REQUEST_FINDINGS_PREFIX: str = "per_request_findings"
COOKIE_EXFILTRATION_PREFIX: str = "cookie_exfiltration_edges"


class FindingsParquetStore:
    """Writes the per-site findings and per-cookie exfiltration parquet outputs."""

    def __init__(self, out_dir: Path) -> None:
        """
        Binds the store to an output directory.

        Args:
            out_dir: The directory the parquet files are written under.
        Returns:
            None
        """
        self.out_dir = out_dir

    def save_site_findings(
        self, findings: list[SitePrivacyFindings], crawl_date: str, geolocation: str
    ) -> Path:
        """
        Writes the per-site findings for one run as a date-and-geolocation-suffixed parquet.

        Args:
            findings: The per-site-per-trial findings rows.
            crawl_date: The crawl date used in the filename.
            geolocation: The geolocation used in the filename.
        Returns:
            path: The parquet path written.
        """
        self.out_dir.mkdir(parents=True, exist_ok=True)
        path = self.out_dir / f"{SITE_FINDINGS_PREFIX}_{crawl_date}_{geolocation}.parquet"
        SitePrivacyFindings.to_dataframe(findings).write_parquet(path)
        return path

    def save_cookie_findings(
        self, analyzed_trials: list[AnalyzedTrial], crawl_date: str, geolocation: str
    ) -> Path:
        """
        Writes the per-cookie classification census for one run as a date-and-geolocation-suffixed parquet.

        Args:
            analyzed_trials: The analyzed trials whose cookies are enumerated.
            crawl_date: The crawl date stamped on every cookie and used in the filename.
            geolocation: The geolocation used in the filename.
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
                        "value": cookie.value,
                        "path": cookie.path,
                        "expires_unix": cookie.expires_unix,
                        "classification_matched": cookie.classification_matched,
                        "cookie_category": cookie.cookie_category,
                        "cookie_vendor": cookie.cookie_vendor,
                        "is_strictly_necessary": cookie.is_strictly_necessary,
                        "is_essential": cookie.is_essential,
                        "is_eprivacy_5_3_candidate": cookie.is_eprivacy_5_3_candidate,
                        "has_identifier_value": cookie.has_identifier_value,
                        "site_id": analyzed.site_id,
                        "geolocation": analyzed.geolocation,
                        "trial": analyzed.trial,
                        "crawl_date": crawl_date,
                        "crawl_start_utc": analyzed.crawl_start_utc,
                    }
                )
        path = self.out_dir / f"{COOKIE_FINDINGS_PREFIX}_{crawl_date}_{geolocation}.parquet"
        frame = pl.DataFrame(rows, infer_schema_length=None) if rows else pl.DataFrame()
        frame.write_parquet(path)
        return path

    def save_request_findings(
        self, analyzed_trials: list[AnalyzedTrial], crawl_date: str, geolocation: str
    ) -> Path:
        """
        Writes the per-request census for one run as a date-and-geolocation-suffixed parquet.

        Args:
            analyzed_trials: The analyzed trials whose requests are enumerated.
            crawl_date: The crawl date stamped on every request and used in the filename.
            geolocation: The geolocation used in the filename.
        Returns:
            path: The parquet path written.
        """
        self.out_dir.mkdir(parents=True, exist_ok=True)
        rows = []
        for analyzed in analyzed_trials:
            for request in analyzed.analyzed_requests:
                rows.append(
                    {
                        "url": request.url,
                        "host": request.host,
                        "registered_domain": request.registered_domain,
                        "is_third_party": request.is_third_party,
                        "is_tracker": request.is_tracker,
                        "tracker_source": request.tracker_source,
                        "tracker_categories": list(request.tracker_categories),
                        "method": request.method,
                        "resource_type": request.resource_type,
                        "site_id": analyzed.site_id,
                        "geolocation": analyzed.geolocation,
                        "trial": analyzed.trial,
                        "crawl_date": crawl_date,
                        "crawl_start_utc": analyzed.crawl_start_utc,
                    }
                )
        path = self.out_dir / f"{REQUEST_FINDINGS_PREFIX}_{crawl_date}_{geolocation}.parquet"
        frame = pl.DataFrame(rows, infer_schema_length=None) if rows else pl.DataFrame()
        frame.write_parquet(path)
        return path

    def save_exfiltration_edges(
        self, analyzed_trials: list[AnalyzedTrial], crawl_date: str, geolocation: str
    ) -> Path:
        """
        Writes the URL-channel exfiltration edges for one run as a date-and-geolocation-suffixed parquet.

        Args:
            analyzed_trials: The analyzed trials scanned for exfiltration edges.
            crawl_date: The crawl date stamped on every edge and used in the filename.
            geolocation: The geolocation used in the filename.
        Returns:
            path: The parquet path written.
        """
        self.out_dir.mkdir(parents=True, exist_ok=True)
        rows = []
        for analyzed in analyzed_trials:
            for edge in ExfilEdge.find_all(analyzed):
                # ExfilEdge is a flat all-primitive record so asdict is 1:1; cookie/request rows
                # project richer domain models onto a chosen schema, hence explicit dicts.
                row = asdict(edge)
                row["site_id"] = analyzed.site_id
                row["geolocation"] = analyzed.geolocation
                row["trial"] = analyzed.trial
                row["crawl_date"] = crawl_date
                row["crawl_start_utc"] = analyzed.crawl_start_utc
                rows.append(row)
        path = self.out_dir / f"{COOKIE_EXFILTRATION_PREFIX}_{crawl_date}_{geolocation}.parquet"
        frame = pl.DataFrame(rows, infer_schema_length=None) if rows else pl.DataFrame()
        frame.write_parquet(path)
        return path
