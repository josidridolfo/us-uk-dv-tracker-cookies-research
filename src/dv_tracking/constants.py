"""
Cross-stage constants shared by the crawl and analysis stages.
"""

from __future__ import annotations

from pathlib import Path

from dv_tracking import PROJECT_ROOT

GEOLOCATIONS: tuple[str, ...] = ("uk", "us")
TRACKER_LISTS_DIR: Path = PROJECT_ROOT / "tracker_lists"
DEFAULT_CONSENTOMATIC_RULES_PATH: Path = TRACKER_LISTS_DIR / "consentomatic" / "Rules.json"
