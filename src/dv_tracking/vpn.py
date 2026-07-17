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
import os
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path

from playwright.sync_api import BrowserContext, Page

from dv_tracking import PROJECT_ROOT
from dv_tracking.constants import GEOLOCATIONS
from dv_tracking.crawl.crawler import (
    ProxyConfigurationError,
    playwright_page_factory,
    proxy_decision_for,
    require_iproyal_credentials,
)
from dv_tracking.utils import keep_known_fields, load_env_file, utcnow_iso

# UK's ISO 3166-1 alpha-2 code is GB; the project label for that cell is "uk".
COUNTRY_CODE_TO_GEOLOCATION: dict[str, str] = {"GB": "uk", "US": "us"}
# The ISO 3166-1 alpha-2 country each project geolocation must resolve to.
EXPECTED_COUNTRY: dict[str, str] = {"uk": "GB", "us": "US"}
IP_GEOLOCATION_ENDPOINT: str = "https://ipinfo.io/json"
LOOKUP_TIMEOUT_SECONDS: float = 10.0
LOOKUP_USER_AGENT: str = "dv-tracking-vpn-validator/0.1"
DEFAULT_CORPUS_DIR: Path = PROJECT_ROOT / "data" / "corpus"

# Residential vs datacenter: best-effort keyword match on the ipinfo `org` field.
# Non-exhaustive; a cloud-upstream exit reads as datacenter (fail-closed to proxy).
DATACENTER_KEYWORDS: tuple[str, ...] = (
    "amazon", "aws",
    "google",
    "microsoft", "azure",
    "digitalocean",
    "linode", "akamai",
    "vultr", "choopa",
    "ovh",
    "hetzner",
    "leaseweb",
    "m247",
    "datacamp",
    "cogent",
    "gtt communications",
    "psychz",
    "quadranet",
    "colocrossing",
)


class VpnLookupError(Exception):
    """Raised when the exit IP, country, or org cannot be determined."""


class ExitCheckError(Exception):
    """Raised when the observed exit fails the country/residential check; the crawl is blocked."""


def is_datacenter(org: str) -> bool:
    """
    Report whether an ipinfo org string names a known datacenter or cloud host.

    Args:
        org: The ipinfo `org` field for the exit IP.
    Returns:
        datacenter: True when any datacenter keyword appears in the lowercased org.
    """
    lowered = org.lower()
    datacenter = any(keyword in lowered for keyword in DATACENTER_KEYWORDS)
    return datacenter


def look_up_exit_ip_and_country(
    timeout_seconds: float = LOOKUP_TIMEOUT_SECONDS,
) -> tuple[str, str, str]:
    """
    Query the geolocation endpoint for the current exit IP, country code, and org.

    Args:
        timeout_seconds: Maximum seconds to wait on the HTTP request.
    Returns:
        exit_facts: The exit IP, its ISO 3166-1 alpha-2 country code, and its org string.
    """
    request = urllib.request.Request(
        IP_GEOLOCATION_ENDPOINT, headers={"User-Agent": LOOKUP_USER_AGENT}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return payload["ip"], payload["country"], payload.get("org", "")
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError) as cause:
        raise VpnLookupError(f"exit lookup via {IP_GEOLOCATION_ENDPOINT} failed: {cause}") from cause


def look_up_exit_through_page(
    geolocation: str,
    *,
    use_proxy: bool,
    page_factory: Callable[[str], AbstractContextManager[tuple[BrowserContext, Page]]] | None = None,
) -> tuple[str, str, str]:
    """
    Read the exit IP, country, and org through the crawler's own Playwright egress.

    Observing through the same page factory and proxy builder the crawl uses means
    the validation samples the exact egress the crawl will present, not a urllib default.

    Args:
        geolocation: The exit geolocation label selecting locale and proxy country.
        use_proxy: Whether the browser routes through the IPRoyal exit.
        page_factory: Injected factory yielding (context, page); None binds the
            crawler's headless playwright_page_factory with this use_proxy.
    Returns:
        exit_facts: The exit IP, its ISO 3166-1 alpha-2 country code, and its org string.
    """
    if page_factory is None:
        context_manager = playwright_page_factory(
            geolocation, headless=True, use_proxy=use_proxy
        )
    else:
        context_manager = page_factory(geolocation)
    try:
        with context_manager as (context, page):
            page.goto(IP_GEOLOCATION_ENDPOINT)
            payload = json.loads(page.evaluate("document.body.innerText"))
            return payload["ip"], payload["country"], payload.get("org", "")
    except Exception as cause:
        raise VpnLookupError(
            f"exit lookup through the page at {IP_GEOLOCATION_ENDPOINT} failed: {cause}"
        ) from cause


def decide_crawl_egress(
    target_geolocation: str,
    environ: Mapping[str, str],
    *,
    look_up: Callable[[], tuple[str, str, str]] = look_up_exit_ip_and_country,
) -> bool:
    """
    Decide whether the crawl routes through the proxy, requiring creds only if so.

    Direct egress iff the machine already sits in the target country on a
    residential exit; any other outcome (including a lookup failure) routes
    through the proxy. When proxying, the IPRoyal credentials are required.

    Args:
        target_geolocation: The geolocation the crawl must egress from ("uk" or "us").
        environ: The environment mapping supplying IPRoyal credentials when proxying.
        look_up: Callable returning the machine's current exit IP, country, and org.
    Returns:
        use_proxy: True when the crawl must route through the proxy.
    """
    try:
        ip, country, org = look_up()
        observed = VpnValidation.geolocation_for_country(country)
        residential = not is_datacenter(org)
    except VpnLookupError:
        # A failed self-lookup is treated as unknown egress: proxy (fail-closed).
        observed, residential = None, False
    use_proxy = proxy_decision_for(target_geolocation, observed, residential)
    if use_proxy:
        require_iproyal_credentials(target_geolocation, environ)
    return use_proxy


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
        use_proxy: bool,
        look_up_through_page: Callable[..., tuple[str, str, str]] = look_up_exit_through_page,
        now: Callable[[], str] = utcnow_iso,
    ) -> VpnValidation:
        """
        Check the crawl's egress against an expected geolocation through the real page.

        The exit is read through the same egress the crawl uses. It passes only
        when the country matches the expected one AND the exit looks residential.

        Args:
            expected_geolocation: The geolocation the exit is expected to resolve to.
            use_proxy: Whether the observed egress routes through the IPRoyal exit.
            look_up_through_page: Callable reading the exit IP, country, and org through
                the page for a geolocation and use_proxy.
            now: Callable returning the current UTC timestamp.
        Returns:
            validation: The validation record; a country mismatch or datacenter exit is passed=False.
        """
        exit_ip, country_code, org = look_up_through_page(expected_geolocation, use_proxy=use_proxy)
        resolved_geolocation = cls.geolocation_for_country(country_code)
        residential = not is_datacenter(org)
        country_matches = country_code == EXPECTED_COUNTRY[expected_geolocation]
        passed = country_matches and residential
        return cls(
            expected_geolocation=expected_geolocation,
            exit_ip=exit_ip,
            resolved_country_code=country_code,
            resolved_geolocation=resolved_geolocation,
            resolved_org=org,
            exit_is_residential=residential,
            via_proxy=use_proxy,
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
        exit_class = "residential" if self.exit_is_residential else "datacenter"
        return (
            f"# VPN exit validation: {self.expected_geolocation}\n\n"
            f"- verdict: **{verdict}**\n"
            f"- exit IP: {self.exit_ip}\n"
            f"- resolved country code: {self.resolved_country_code}\n"
            f"- resolved geolocation: {resolved}\n"
            f"- expected geolocation: {self.expected_geolocation}\n"
            f"- exit org: {self.resolved_org}\n"
            f"- exit classification: {exit_class} (best-effort ipinfo org keyword heuristic)\n"
            f"- via proxy: {self.via_proxy}\n"
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
    look_up: Callable[[], tuple[str, str, str]] = look_up_exit_ip_and_country,
    look_up_through_page: Callable[..., tuple[str, str, str]] = look_up_exit_through_page,
    corpus_dir: Path = DEFAULT_CORPUS_DIR,
) -> int:
    """
    Command-line entry point for the /validate-vpn gate.

    Decides the crawl egress with the same policy as the crawl (direct only when
    already in-country and residential), then validates through that egress.

    Args:
        argv: Argument vector; defaults to sys.argv when None.
        look_up: Callable returning the machine's exit IP, country, and org.
        look_up_through_page: Callable reading the exit through the crawl's own page egress.
        corpus_dir: The directory a written record is placed in.
    Returns:
        exit_code: 0 on pass, 1 on mismatch or datacenter exit, 2 on lookup or config error.
    """
    # Load repo-root .env before any credential read; exported vars still win.
    load_env_file()
    parser = argparse.ArgumentParser(description="Validate the active VPN exit geolocation.")
    parser.add_argument("geolocation", choices=GEOLOCATIONS)
    arguments = parser.parse_args(argv)

    try:
        use_proxy = decide_crawl_egress(arguments.geolocation, os.environ, look_up=look_up)
    except ProxyConfigurationError as error:
        print(f"ERROR: {error}")
        print("Do NOT run the crawl.")
        return 2

    try:
        validation = VpnValidation.check(
            arguments.geolocation,
            use_proxy=use_proxy,
            look_up_through_page=look_up_through_page,
        )
    except VpnLookupError as error:
        print(f"ERROR: {error}")
        print("Do NOT run the crawl.")
        return 2

    record_path = validation.write(corpus_dir)
    egress = "proxy exit" if validation.via_proxy else "direct exit"
    if validation.passed:
        print(
            f"PASS: {arguments.geolocation} {egress} resolved to {validation.exit_ip} "
            f"({validation.resolved_country_code}, residential); record written to {record_path}"
        )
        return 0

    resolved = validation.resolved_geolocation or "an unmapped country"
    exit_class = "residential" if validation.exit_is_residential else "datacenter"
    print(
        f"FAIL: {arguments.geolocation} {egress} resolved to {validation.exit_ip} "
        f"({validation.resolved_country_code}, {resolved}, {exit_class}); "
        f"record written to {record_path}"
    )
    print("Do NOT run the crawl.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
