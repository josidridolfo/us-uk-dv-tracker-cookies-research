"""
Per-trial raw persistence.

A RawStore owns the on-disk layout for raw trials; the layout spans several
inputs and the store is the single writer, so persistence is a store object
rather than a method on the record. RawTrial.from_disk reads a trial back.

On-disk layout:
    <raw_root>/<crawl_date>_<geolocation>/<site_id>/trial_<N>/{metadata.json, requests.jsonl, cookies.jsonl}
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path

from dv_tracking.crawl.models import RawTrial


class RawStore:
    """Writes raw trials into a per-trial directory tree under a root, and reads them back."""

    def __init__(self, raw_root: Path) -> None:
        """
        Bind the store to a raw-output root directory.

        Args:
            raw_root: The root directory under which trial directories are created.
        Returns:
            None
        """
        self.raw_root = raw_root

    def save(self, trial: RawTrial, exit_info: dict, crawl_date: str) -> Path:
        """
        Write requests.jsonl, cookies.jsonl and metadata.json for one raw trial.

        Args:
            trial: The raw trial to persist.
            exit_info: VPN exit provenance recorded in the metadata; on a real
                crawl the gate-verified exit facts (ip, country, org, residential,
                via_proxy, checked_at_utc) plus an optional provider label.
            crawl_date: The crawl date, used in the cell directory name.
        Returns:
            trial_dir: The directory the trial was written to.
        """
        trial_dir = (
            self.raw_root
            / f"{crawl_date}_{trial.geolocation}"
            / trial.site_id
            / f"trial_{trial.trial}"
        )
        trial_dir.mkdir(parents=True, exist_ok=True)

        with (trial_dir / "requests.jsonl").open("w", encoding="utf-8") as handle:
            for request in trial.requests:
                handle.write(json.dumps(asdict(request), separators=(",", ":")))
                handle.write("\n")

        with (trial_dir / "cookies.jsonl").open("w", encoding="utf-8") as handle:
            for cookie in trial.cookies:
                handle.write(json.dumps(asdict(cookie), separators=(",", ":")))
                handle.write("\n")

        metadata = {
            "site_id": trial.site_id,
            "url": trial.url,
            "geolocation": trial.geolocation,
            "trial": trial.trial,
            "crawl_date": crawl_date,
            "crawl_start_utc": trial.crawl_start_utc,
            "crawl_end_utc": trial.crawl_end_utc,
            "time_on_page_since_page_load": trial.time_on_page_since_page_load,
            "navigation_status": trial.navigation_status,
            "navigation_error": trial.navigation_error,
            "blocked": trial.blocked,
            "request_count": len(trial.requests),
            "cookie_count": len(trial.cookies),
            "tcf_probe": asdict(trial.tcf_probe) if trial.tcf_probe is not None else None,
            "local_storage": asdict(trial.local_storage) if trial.local_storage is not None else None,
            "banner_signals": asdict(trial.banner_signals) if trial.banner_signals is not None else None,
            "exit_info": exit_info,
        }
        (trial_dir / "metadata.json").write_text(
            json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8"
        )
        return trial_dir

    def load(self, trial_dir: Path) -> RawTrial | None:
        """
        Read one raw trial back from its directory.

        Args:
            trial_dir: The trial directory holding metadata.json and the JSONL traces.
        Returns:
            trial: The rebuilt raw trial, or None when metadata is absent or unreadable.
        """
        return RawTrial.from_disk(trial_dir)

    def iter_trials(self) -> Iterator[RawTrial]:
        """
        Yield every raw trial saved under the root, in cell/site/trial order.

        Assumes a single crawl per geolocation under raw_root; multiple crawls for
        one geolocation pool across runs and corrupt the 15s-dwell collapse.

        Args:
            None
        Returns:
            trials: An iterator over the readable raw trials; unreadable dirs are skipped.
        """
        if not self.raw_root.is_dir():
            return
        for cell_dir in sorted(path for path in self.raw_root.iterdir() if path.is_dir()):
            for site_dir in sorted(path for path in cell_dir.iterdir() if path.is_dir()):
                for trial_dir in sorted(path for path in site_dir.iterdir() if path.is_dir()):
                    trial = RawTrial.from_disk(trial_dir)
                    if trial is not None:
                        yield trial
