"""
JavaScript probe payloads injected into pages during a crawl.

Each probe's source is a sibling .js file read on demand for page.evaluate.
"""

from __future__ import annotations

from pathlib import Path

PROBES_DIR: Path = Path(__file__).parent


def read_probe(name: str) -> str:
    """
    Reads a probe's JavaScript source from the probes directory.

    Args:
        name: The probe filename, e.g. "tcf_ping.js".
    Returns:
        source: The probe's JavaScript source.
    """
    return (PROBES_DIR / name).read_text(encoding="utf-8")
