"""
Validate that a crawl's exit lands in the expected jurisdiction on a residential IP.
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass

from dv_tracking.constants import EXPECTED_COUNTRY, GEOLOCATIONS
from dv_tracking.crawl.crawler import (
    ProxyConfigurationError,
    playwright_page_factory,
    proxy_decision_for,
    require_iproyal_credentials,
)
from dv_tracking.utils import geolocation_for_country, is_datacenter, load_env_file, utcnow_iso

IP_GEOLOCATION_ENDPOINT: str = "https://ipinfo.io/json"
LOOKUP_TIMEOUT_SECONDS: float = 10.0


class VpnLookupError(Exception):
    """Raised when the exit IP, country, or org cannot be determined."""


class ExitCheckError(Exception):
    """Raised when the observed exit fails the country/residential check; the crawl is blocked."""


@dataclass(frozen=True)
class VpnValidation:
    """The outcome of checking the active VPN exit against an expected geolocation."""

    expected_geolocation: str
    exit_ip: str
    resolved_country_code: str
    resolved_geolocation: str | None
    resolved_org: str
    exit_is_residential: bool
    via_proxy: bool
    passed: bool
    checked_at_utc: str

    def as_provenance(self, provider: str | None = None) -> dict:
        """The exit facts recorded as metadata provenance for a crawl."""
        provenance = {
            "ip": self.exit_ip,
            "country": self.resolved_country_code,
            "org": self.resolved_org,
            "residential": self.exit_is_residential,
            "via_proxy": self.via_proxy,
            "checked_at_utc": self.checked_at_utc,
        }
        if provider:
            provenance["provider"] = provider
        return provenance


class VpnValidator:
    """Checks and decides a crawl's exit geolocation for one target jurisdiction."""

    def __init__(self, geolocation: str, environ: Mapping[str, str]) -> None:
        """
        Bind the validator to a target geolocation and environment.

        Args:
            geolocation: The geolocation the crawl must egress from ("uk" or "us").
            environ: The environment mapping supplying credentials when proxying.
        Returns:
            None
        """
        self.geolocation = geolocation
        self.environ = environ

    def look_up_exit_ip_and_country(self) -> tuple[str, str, str]:
        """
        Query the geolocation endpoint for the current exit IP, country code, and org.

        Args:
            None
        Returns:
            exit_facts: The exit IP, its ISO 3166-1 alpha-2 country code, and its org string.
        Raises:
            VpnLookupError: When the endpoint request fails or returns unusable JSON.
        """
        try:
            with urllib.request.urlopen(
                IP_GEOLOCATION_ENDPOINT, timeout=LOOKUP_TIMEOUT_SECONDS
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
            return payload["ip"], payload["country"], payload.get("org", "")
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError) as cause:
            raise VpnLookupError(
                f"exit lookup via {IP_GEOLOCATION_ENDPOINT} failed: {cause}"
            ) from cause

    def look_up_exit_through_page(self, *, use_proxy: bool) -> tuple[str, str, str]:
        """
        Read the exit IP, country, and org through the crawler's own Playwright egress.

        Args:
            use_proxy: Whether the browser routes through the IPRoyal exit.
        Returns:
            exit_facts: The exit IP, its ISO 3166-1 alpha-2 country code, and its org string.
        Raises:
            VpnLookupError: When the page navigation or JSON parse fails.
        """
        try:
            with playwright_page_factory(
                self.geolocation, headless=True, use_proxy=use_proxy
            ) as (context, page):
                page.goto(IP_GEOLOCATION_ENDPOINT)
                payload = json.loads(page.evaluate("document.body.innerText"))
                return payload["ip"], payload["country"], payload.get("org", "")
        except Exception as cause:
            raise VpnLookupError(
                f"exit lookup through the page at {IP_GEOLOCATION_ENDPOINT} failed: {cause}"
            ) from cause

    def decide_egress(self) -> bool:
        """
        Decide whether the crawl routes through the proxy, requiring creds only if so.

        Direct egress iff the machine already sits in the target country on a
        residential exit; any other outcome (including a lookup failure) routes
        through the proxy.

        Args:
            None
        Returns:
            use_proxy: True when the crawl must route through the proxy.
        Raises:
            ProxyConfigurationError: When proxying is required but credentials are missing.
        """
        try:
            ip, country, org = self.look_up_exit_ip_and_country()
            observed = geolocation_for_country(country)
            residential = not is_datacenter(org)
        except VpnLookupError:
            # A failed self-lookup is treated as unknown egress: proxy (fail-closed).
            observed, residential = None, False
        use_proxy = proxy_decision_for(self.geolocation, observed, residential)
        if use_proxy:
            require_iproyal_credentials(self.geolocation, self.environ)
        return use_proxy

    def validate(self, *, use_proxy: bool) -> VpnValidation:
        """
        Check the crawl's egress against the expected geolocation through the real page.

        The exit is read through the same egress the crawl uses. It passes only
        when the country matches the expected one AND the exit looks residential.

        Args:
            use_proxy: Whether the observed egress routes through the IPRoyal exit.
        Returns:
            validation: The validation record; a country mismatch or datacenter exit is passed=False.
        Raises:
            VpnLookupError: When the exit could not be read through the page.
        """
        exit_ip, country_code, org = self.look_up_exit_through_page(use_proxy=use_proxy)
        resolved_geolocation = geolocation_for_country(country_code)
        residential = not is_datacenter(org)
        country_matches = country_code == EXPECTED_COUNTRY[self.geolocation]
        passed = country_matches and residential
        return VpnValidation(
            expected_geolocation=self.geolocation,
            exit_ip=exit_ip,
            resolved_country_code=country_code,
            resolved_geolocation=resolved_geolocation,
            resolved_org=org,
            exit_is_residential=residential,
            via_proxy=use_proxy,
            passed=passed,
            checked_at_utc=utcnow_iso(),
        )


def main(argv: list[str] | None = None) -> int:
    """
    Command-line entry point that validates the crawl's exit geolocation.

    Decides the crawl egress with the same policy as the crawl (direct only when
    already in-country and residential), then validates through that egress.

    Args:
        argv: Argument vector; defaults to sys.argv when None.
    Returns:
        exit_code: 0 on pass, 1 on mismatch or datacenter exit, 2 on lookup or config error.
    """
    # Load repo-root .env before any credential read; exported vars still win.
    load_env_file()
    parser = argparse.ArgumentParser(description="Validate the active VPN exit geolocation.")
    parser.add_argument("geolocation", choices=GEOLOCATIONS)
    arguments = parser.parse_args(argv)

    validator = VpnValidator(arguments.geolocation, os.environ)

    try:
        use_proxy = validator.decide_egress()
    except ProxyConfigurationError as error:
        print(f"ERROR: {error}")
        print("Do NOT run the crawl.")
        return 2

    try:
        validation = validator.validate(use_proxy=use_proxy)
    except VpnLookupError as error:
        print(f"ERROR: {error}")
        print("Do NOT run the crawl.")
        return 2

    egress = "proxy exit" if validation.via_proxy else "direct exit"
    if validation.passed:
        print(
            f"PASS: {arguments.geolocation} {egress} resolved to {validation.exit_ip} "
            f"({validation.resolved_country_code}, residential)"
        )
        return 0

    resolved = validation.resolved_geolocation or "an unmapped country"
    exit_class = "residential" if validation.exit_is_residential else "datacenter"
    print(
        f"FAIL: {arguments.geolocation} {egress} resolved to {validation.exit_ip} "
        f"({validation.resolved_country_code}, {resolved}, {exit_class})"
    )
    print("Do NOT run the crawl.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
