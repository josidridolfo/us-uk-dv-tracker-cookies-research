"""
Host-only vs faithful EasyPrivacy matcher: cross-source robustness comparison.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import polars as pl

from dv_tracking import PROJECT_ROOT
from dv_tracking.analysis.models import longest_dwell_trials
from dv_tracking.analysis.statistics import percent, proportion

DEFAULT_PROCESSED_DIR: Path = PROJECT_ROOT / "data" / "processed"
DEFAULT_BASELINE_DIR: Path = DEFAULT_PROCESSED_DIR / "_hostonly_baseline"
REQUEST_FINDINGS_PREFIX: str = "per_request_findings"


@dataclass(frozen=True)
class CrossSourceSplit:
    """The four EP-vs-DDG buckets over the third-party requests of one run."""

    third_party_count: int
    both: int
    ep_only: int
    ddg_only: int
    none: int

    @classmethod
    def from_request_frame(cls, frame: pl.DataFrame) -> CrossSourceSplit:
        """
        Buckets one run's third-party requests by their ``tracker_source`` label.

        Args:
            frame: A per-request findings frame with ``is_third_party`` and ``tracker_source``.
        Returns:
            split: The four-bucket cross-source split over third-party requests.
        """
        third_party = frame.filter(pl.col("is_third_party"))
        source = third_party.get_column("tracker_source")
        return cls(
            third_party_count=third_party.height,
            both=int((source == "both").sum()),
            ep_only=int((source == "easyprivacy").sum()),
            ddg_only=int((source == "ddg").sum()),
            none=int((source == "none").sum()),
        )

    @property
    def ep_total(self) -> int:
        """EasyPrivacy-flagged third-party requests (host-list agreement aside)."""
        return self.both + self.ep_only

    @property
    def ddg_total(self) -> int:
        """DDG-flagged third-party requests (the common, matcher-invariant base)."""
        return self.both + self.ddg_only

    @property
    def union_total(self) -> int:
        """Requests flagged by either list (the tracker union)."""
        return self.both + self.ep_only + self.ddg_only

    @property
    def ep_to_ddg_ratio(self) -> float:
        """EP-flagged relative to DDG-flagged; zero when DDG flags nothing."""
        return proportion(self.ep_total, self.ddg_total)

    @property
    def ep_share_percent(self) -> float:
        """EP-flagged as a percentage of the cell's third-party requests."""
        return percent(self.ep_total, self.third_party_count)


@dataclass(frozen=True)
class MatcherComparison:
    """Host-only vs faithful EasyPrivacy verdicts for one jurisdiction cell."""

    geolocation: str
    host_only: CrossSourceSplit
    faithful: CrossSourceSplit
    flips: pl.DataFrame = field(compare=False, repr=False)

    @classmethod
    def from_parquet(
        cls,
        geolocation: str,
        crawl_date: str,
        processed_dir: Path = DEFAULT_PROCESSED_DIR,
        baseline_dir: Path = DEFAULT_BASELINE_DIR,
    ) -> MatcherComparison:
        """
        Loads the host-only baseline and faithful per-request parquet for one cell.

        Args:
            geolocation: The run geolocation label (uk or us).
            crawl_date: The crawl date stamped in the parquet filenames.
            processed_dir: The directory holding the current (faithful) parquet.
            baseline_dir: The directory holding the preserved host-only parquet.
        Returns:
            comparison: The paired comparison for the cell.
        """
        name = f"{REQUEST_FINDINGS_PREFIX}_{crawl_date}_{geolocation}.parquet"
        baseline = pl.read_parquet(baseline_dir / name)
        faithful = pl.read_parquet(processed_dir / name)
        # The parquet holds all three dwell trials; collapse to one dwell per site first to
        # avoid n-tuple counting. One selector serves both frames (blocked is matcher-invariant).
        selector = longest_dwell_trials(faithful)
        keys = ["site_id", "geolocation", "trial"]
        baseline = baseline.join(selector, on=keys, how="semi")
        faithful = faithful.join(selector, on=keys, how="semi")
        return cls(
            geolocation=geolocation,
            host_only=CrossSourceSplit.from_request_frame(baseline),
            faithful=CrossSourceSplit.from_request_frame(faithful),
            flips=cls.paired_flips(baseline, faithful),
        )

    @staticmethod
    def paired_flips(baseline: pl.DataFrame, faithful: pl.DataFrame) -> pl.DataFrame:
        """
        Cross-tabulates baseline vs faithful ``tracker_source`` for aligned third-party rows.

        Args:
            baseline: The host-only per-request frame for one run.
            faithful: The faithful per-request frame for the same run.
        Returns:
            matrix: One row per (baseline label, faithful label) transition with a count.
        """
        if baseline.height != faithful.height:
            raise ValueError(
                f"row-count mismatch: baseline {baseline.height} vs faithful {faithful.height}"
            )
        aligned = pl.DataFrame(
            {
                "url": baseline.get_column("url"),
                "url_faithful": faithful.get_column("url"),
                "is_third_party": baseline.get_column("is_third_party"),
                "source_baseline": baseline.get_column("tracker_source"),
                "source_faithful": faithful.get_column("tracker_source"),
            }
        )
        misaligned = int((aligned.get_column("url") != aligned.get_column("url_faithful")).sum())
        if misaligned:
            raise ValueError(f"row alignment broken: {misaligned} url mismatches between the runs")
        return (
            aligned.filter(pl.col("is_third_party"))
            .group_by(["source_baseline", "source_faithful"])
            .len()
            .sort("len", descending=True)
        )

    def render(self) -> str:
        """
        Formats the cell's split-under-each-matcher, ratio shift, union delta, and flips.

        Returns:
            report: A multi-line human-readable comparison block for the cell.
        """
        lines = [f"=== {self.geolocation.upper()} ({self.faithful.third_party_count} third-party requests) ==="]
        header = f"{'matcher':12} {'both':>7} {'ep_only':>8} {'ddg_only':>9} {'none':>7} {'ep_total':>9} {'ep/ddg':>7}"
        lines.append(header)
        for label, split in (("host-only", self.host_only), ("faithful", self.faithful)):
            lines.append(
                f"{label:12} {split.both:>7} {split.ep_only:>8} {split.ddg_only:>9} "
                f"{split.none:>7} {split.ep_total:>9} {split.ep_to_ddg_ratio:>7.2f}"
            )
        lines.append(
            f"EP share of 3P: {self.host_only.ep_share_percent:.1f}% -> {self.faithful.ep_share_percent:.1f}% | "
            f"union tracker requests: {self.host_only.union_total} -> {self.faithful.union_total} "
            f"(net {self.faithful.union_total - self.host_only.union_total:+d})"
        )
        lines.append("paired flips (baseline -> faithful), changed rows only:")
        changed = self.flips.filter(pl.col("source_baseline") != pl.col("source_faithful"))
        for row in changed.iter_rows(named=True):
            lines.append(f"  {row['source_baseline']:>11} -> {row['source_faithful']:<11} {row['len']}")
        return "\n".join(lines)

    def to_dataframe(self) -> pl.DataFrame:
        """
        The cross-source split under each matcher, one row per (geolocation, matcher).

        Args:
            None
        Returns:
            frame: One row for the host-only matcher and one for the faithful matcher.
        """
        rows = [
            {
                "geolocation": self.geolocation,
                "matcher": matcher,
                "third_party_count": split.third_party_count,
                "both": split.both,
                "ep_only": split.ep_only,
                "ddg_only": split.ddg_only,
                "none": split.none,
                "ep_total": split.ep_total,
                "ddg_total": split.ddg_total,
                "union_total": split.union_total,
                "ep_to_ddg_ratio": split.ep_to_ddg_ratio,
                "ep_share_percent": split.ep_share_percent,
            }
            for matcher, split in (("host_only", self.host_only), ("faithful", self.faithful))
        ]
        return pl.DataFrame(rows, infer_schema_length=None)

    def write_parquet(self, path: Path) -> Path:
        """
        Writes the per-matcher cross-source split as a parquet file.

        Args:
            path: The file path to write.
        Returns:
            path: The path written.
        """
        self.to_dataframe().write_parquet(path)
        return path
