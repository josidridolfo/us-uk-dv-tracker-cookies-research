"""
Validate that the active VPN exit lands in the expected jurisdiction.

The /validate-vpn gate confirms a UK exit resolves to a UK IP and a US exit to
a US IP before any crawl runs. This module's code is committed, but the written
validation record carries the author's real exit IP and is written under data/
(git-excluded); the record itself is never committed.
"""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from dv_tracking import PROJECT_ROOT
from dv_tracking.constants import GEOLOCATIONS
from dv_tracking.utils import keep_known_fields, utcnow_iso

# UK's ISO 3166-1 alpha-2 code is GB; the project label for that cell is "uk".
COUNTRY_CODE_TO_GEOLOCATION: dict[str, str] = {"GB": "uk", "US": "us"}
IP_GEOLOCATION_ENDPOINT: str = "https://ipinfo.io/json"
LOOKUP_TIMEOUT_SECONDS: float = 10.0
LOOKUP_USER_AGENT: str = "dv-tracking-vpn-validator/0.1"
DEFAULT_CORPUS_DIR: Path = PROJECT_ROOT / "data" / "corpus"


class VpnLookupError(Exception):
    """Raised when the exit IP or country cannot be determined."""


def look_up_exit_ip_and_country(timeout_seconds: float = LOOKUP_TIMEOUT_SECONDS) -> tuple[str, str]:
    """
    Query the geolocation endpoint for the current exit IP and country code.

    Args:
        timeout_seconds: Maximum seconds to wait on the HTTP request.
    Returns:
        ip_and_country: The exit IP and its ISO 3166-1 alpha-2 country code.
    """
    request = urllib.request.Request(
        IP_GEOLOCATION_ENDPOINT, headers={"User-Agent": LOOKUP_USER_AGENT}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return payload["ip"], payload["country"]
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError) as cause:
        raise VpnLookupError(f"exit lookup via {IP_GEOLOCATION_ENDPOINT} failed: {cause}") from cause


@dataclass(frozen=True)
class VpnValidation:
    """The outcome of checking the active VPN exit against an expected geolocation."""

    expected_geolocation: str
    exit_ip: str
    resolved_country_code: str
    resolved_geolocation: str | None
    passed: bool
    checked_at_utc: str

    @staticmethod
    def geolocation_for_country(country_code: str) -> str | None:
        """
        Map an ISO country code to the project geolocation label.

        Args:
            country_code: The ISO 3166-1 alpha-2 country code.
        Returns:
            geolocation: The mapped label, or None when the country is unmapped.
        """
        return COUNTRY_CODE_TO_GEOLOCATION.get(country_code)

    @classmethod
    def check(
        cls,
        expected_geolocation: str,
        *,
        look_up: Callable[[], tuple[str, str]] = look_up_exit_ip_and_country,
        now: Callable[[], str] = utcnow_iso,
    ) -> VpnValidation:
        """
        Check the active VPN exit against an expected geolocation.

        Args:
            expected_geolocation: The geolocation the exit is expected to resolve to.
            look_up: Callable returning the exit IP and country code.
            now: Callable returning the current UTC timestamp.
        Returns:
            validation: The validation record; a country mismatch is a passed=False record.
        """
        exit_ip, country_code = look_up()
        resolved_geolocation = cls.geolocation_for_country(country_code)
        passed = resolved_geolocation is not None and resolved_geolocation == expected_geolocation
        return cls(
            expected_geolocation=expected_geolocation,
            exit_ip=exit_ip,
            resolved_country_code=country_code,
            resolved_geolocation=resolved_geolocation,
            passed=passed,
            checked_at_utc=now(),
        )

    @classmethod
    def from_dict(cls, mapping: dict) -> VpnValidation:
        """
        Build a validation record from a decoded JSON object.

        Args:
            mapping: The decoded validation record; unknown keys are dropped.
        Returns:
            validation: The validation record.
        """
        return cls(**keep_known_fields(cls, mapping))

    def to_markdown(self) -> str:
        """
        Render this validation record as a human-readable markdown block.

        Args:
            None
        Returns:
            markdown: The formatted markdown text.
        """
        verdict = "PASS" if self.passed else "FAIL"
        resolved = self.resolved_geolocation or "unmapped"
        return (
            f"# VPN exit validation: {self.expected_geolocation}\n\n"
            f"- verdict: **{verdict}**\n"
            f"- exit IP: {self.exit_ip}\n"
            f"- resolved country code: {self.resolved_country_code}\n"
            f"- resolved geolocation: {resolved}\n"
            f"- expected geolocation: {self.expected_geolocation}\n"
            f"- checked at (UTC): {self.checked_at_utc}\n"
        )

    def write(self, corpus_dir: Path = DEFAULT_CORPUS_DIR) -> Path:
        """
        Write the rendered validation markdown to the corpus directory.

        Args:
            corpus_dir: The directory the record is written to; created when missing.
        Returns:
            record_path: The path the markdown record was written to.
        """
        corpus_dir.mkdir(parents=True, exist_ok=True)
        record_path = corpus_dir / f"vpn_validation_{self.expected_geolocation}.md"
        record_path.write_text(self.to_markdown(), encoding="utf-8")
        return record_path


def main(
    argv: list[str] | None = None,
    *,
    look_up: Callable[[], tuple[str, str]] = look_up_exit_ip_and_country,
    corpus_dir: Path = DEFAULT_CORPUS_DIR,
) -> int:
    """
    Command-line entry point for the /validate-vpn gate.

    Args:
        argv: Argument vector; defaults to sys.argv when None.
        look_up: Callable returning the exit IP and country code.
        corpus_dir: The directory a written record is placed in.
    Returns:
        exit_code: 0 on pass, 1 on mismatch or unknown country, 2 on lookup error.
    """
    parser = argparse.ArgumentParser(description="Validate the active VPN exit geolocation.")
    parser.add_argument("geolocation", choices=GEOLOCATIONS)
    arguments = parser.parse_args(argv)

    try:
        validation = VpnValidation.check(arguments.geolocation, look_up=look_up)
    except VpnLookupError as error:
        print(f"ERROR: {error}")
        print("Do NOT run the crawl.")
        return 2

    record_path = validation.write(corpus_dir)
    if validation.passed:
        print(
            f"PASS: {arguments.geolocation} exit resolved to {validation.exit_ip} "
            f"({validation.resolved_country_code}); record written to {record_path}"
        )
        return 0

    resolved = validation.resolved_geolocation or "an unmapped country"
    print(
        f"FAIL: {arguments.geolocation} exit resolved to {validation.exit_ip} "
        f"({validation.resolved_country_code}, {resolved}); record written to {record_path}"
    )
    print("Do NOT run the crawl.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
