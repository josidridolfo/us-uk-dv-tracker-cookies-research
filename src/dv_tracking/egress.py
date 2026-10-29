"""
Check that the crawl's relay egress lands in the expected jurisdiction on a residential exit.

Owns the relay address, the exit read-back through it, and the pass/fail
check against a target geolocation. Imports stdlib and `dv_tracking.constants`
only: the crawl container deliberately keeps the analysis stage's heavier
dependencies out, and this module has to run inside that container anyway,
since it must observe the same egress the browser uses.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import time
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from dv_tracking import PROJECT_ROOT
from dv_tracking.constants import (
    COUNTRY_CODE_TO_GEOLOCATION,
    DATACENTER_KEYWORDS,
    EXPECTED_COUNTRY,
    GEOLOCATIONS,
)

IP_GEOLOCATION_ENDPOINT: str = "https://ipinfo.io/json"
LOOKUP_TIMEOUT_SECONDS: float = 10.0
MIN_PORT: int = 1
MAX_PORT: int = 65535
RELAY_READ_ATTEMPTS: int = 3
RELAY_READ_RETRY_DELAY_SECONDS: float = 2.0


class ProxyConfigurationError(Exception):
    """Raised when the relay's address is unset or invalid."""


class ExitReadError(Exception):
    """Raised when the exit facts cannot be read through the relay or parsed."""


@dataclass(frozen=True)
class RelayAddress:
    """The relay's host and port; the gate carries no credential for it."""

    host: str
    port: int

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> RelayAddress:
        """
        Read the relay's host and port from the environment.

        DV_RELAY_HOST and DV_RELAY_PORT carry no default: an unset variable
        must fail closed rather than quietly resolve to some other listener
        on the machine, which would let the gate pass against the wrong
        endpoint.

        Args:
            environ: The environment mapping supplying DV_RELAY_HOST / DV_RELAY_PORT.
        Returns:
            relay: The parsed relay address.
        Raises:
            ProxyConfigurationError: When DV_RELAY_HOST or DV_RELAY_PORT is
                unset, or DV_RELAY_PORT is not a port number in 1-65535.
        """
        host = environ.get("DV_RELAY_HOST")
        if not host:
            raise ProxyConfigurationError("DV_RELAY_HOST is required to route through the relay")
        port = environ.get("DV_RELAY_PORT")
        if not port:
            raise ProxyConfigurationError("DV_RELAY_PORT is required to route through the relay")
        if not (port.isascii() and port.isdigit()) or not (MIN_PORT <= int(port) <= MAX_PORT):
            raise ProxyConfigurationError(f"DV_RELAY_PORT must be a port number, got {port!r}")
        return cls(host=host, port=int(port))

    @property
    def proxy_url(self) -> str:
        """The relay's HTTP proxy URL, carrying no credentials."""
        return f"http://{self.host}:{self.port}"


@dataclass(frozen=True)
class ObservedExit:
    """The exit IP, country, and org read back through the relay."""

    ip: str
    country: str
    org: str

    @property
    def geolocation(self) -> str | None:
        """The project geolocation mapped from the observed country, or None when unmapped."""
        return COUNTRY_CODE_TO_GEOLOCATION.get(self.country)

    @property
    def is_residential(self) -> bool:
        """Whether the org reads as a residential ISP rather than a known datacenter.

        An empty or missing org is treated as unknown, not residential: the
        gate must fail closed on an exit it cannot classify.
        """
        if not self.org:
            return False
        lowered = self.org.lower()
        return not any(keyword in lowered for keyword in DATACENTER_KEYWORDS)


@dataclass(frozen=True)
class ExitCheck:
    """The outcome of checking the relay's observed exit against an expected geolocation."""

    expected_geolocation: str
    exit_ip: str
    resolved_country_code: str
    resolved_geolocation: str | None
    resolved_org: str
    exit_is_residential: bool
    passed: bool
    checked_at_utc: str

    def as_provenance(self) -> dict:
        """The exit facts recorded as metadata provenance for a crawl."""
        return {
            "ip": self.exit_ip,
            "country": self.resolved_country_code,
            "org": self.resolved_org,
            "residential": self.exit_is_residential,
            "checked_at_utc": self.checked_at_utc,
        }


def utcnow_iso() -> str:
    """
    Return the current UTC time as an ISO 8601 string.

    Args:
        None
    Returns:
        timestamp: The current UTC instant in ISO 8601 format.
    """
    return datetime.now(tz=UTC).isoformat()


def load_env_file(path: Path = PROJECT_ROOT / ".env") -> None:
    """
    Load KEY=value lines from a .env file into os.environ without clobbering.

    Real exported variables win: each key is set only when absent, so exporting
    or `source`-ing overrides the file. A missing file is a no-op; no value is
    ever logged.

    Args:
        path: The .env file to read; defaults to the repo-root .env.
    Returns:
        None
    """
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


def read_exit_through_relay(relay: RelayAddress) -> ObservedExit:
    """
    Read the exit IP, country, and org through the relay a trial would use.

    Retries up to RELAY_READ_ATTEMPTS times: the relay sidecar can still be
    starting when the gate runs immediately after the stack comes up, and
    one failed attempt must not block an entire crawl on that startup race.

    Args:
        relay: The relay address to route through.
    Returns:
        exit_observed: The observed exit.
    Raises:
        ExitReadError: When every attempt fails, times out, or the response
            is not usable JSON with an ip and country field.
    """
    handler = urllib.request.ProxyHandler({"http": relay.proxy_url, "https": relay.proxy_url})
    opener = urllib.request.build_opener(handler)
    last_cause: Exception = RuntimeError("no attempt made")
    for attempt in range(1, RELAY_READ_ATTEMPTS + 1):
        try:
            with opener.open(IP_GEOLOCATION_ENDPOINT, timeout=LOOKUP_TIMEOUT_SECONDS) as response:
                payload = json.loads(response.read().decode("utf-8"))
            return ObservedExit(
                ip=payload["ip"], country=payload["country"], org=payload.get("org") or ""
            )
        except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException) as cause:
            last_cause = cause
            if attempt < RELAY_READ_ATTEMPTS:
                time.sleep(RELAY_READ_RETRY_DELAY_SECONDS)
    raise ExitReadError(
        f"exit lookup via {IP_GEOLOCATION_ENDPOINT} failed after "
        f"{RELAY_READ_ATTEMPTS} attempts: {last_cause}"
    ) from last_cause


def check_exit(geolocation: str, relay: RelayAddress) -> ExitCheck:
    """
    Check the relay's observed exit against the expected geolocation.

    It passes only when the country matches the expected one AND the exit
    looks residential.

    Args:
        geolocation: The geolocation the crawl must egress from ("uk" or "us").
        relay: The relay address to read the exit through.
    Returns:
        check: The check record; a country mismatch or datacenter exit is passed=False.
    Raises:
        ExitReadError: When the exit could not be read through the relay.
    """
    exit_observed = read_exit_through_relay(relay)
    country_matches = exit_observed.country == EXPECTED_COUNTRY[geolocation]
    passed = country_matches and exit_observed.is_residential
    return ExitCheck(
        expected_geolocation=geolocation,
        exit_ip=exit_observed.ip,
        resolved_country_code=exit_observed.country,
        resolved_geolocation=exit_observed.geolocation,
        resolved_org=exit_observed.org,
        exit_is_residential=exit_observed.is_residential,
        passed=passed,
        checked_at_utc=utcnow_iso(),
    )


def main(argv: list[str] | None = None) -> int:
    """
    Command-line entry point that checks the crawl's relay exit against the target geolocation.

    Args:
        argv: Argument vector; defaults to sys.argv when None.
    Returns:
        exit_code: 0 on pass, 1 on mismatch or datacenter exit, 2 on a relay
            configuration or read error.
    """
    # Load repo-root .env before any config read; exported vars still win.
    load_env_file()
    parser = argparse.ArgumentParser(description="Check the crawl's relay exit geolocation.")
    parser.add_argument("geolocation", choices=GEOLOCATIONS)
    arguments = parser.parse_args(argv)

    try:
        relay = RelayAddress.from_environ(os.environ)
        check = check_exit(arguments.geolocation, relay)
    except (ProxyConfigurationError, ExitReadError) as error:
        print(f"ERROR: {error}")
        print("Do NOT run the crawl.")
        return 2

    if check.passed:
        print(
            f"PASS: {arguments.geolocation} exit resolved to {check.exit_ip} "
            f"({check.resolved_country_code}, residential)"
        )
        return 0

    resolved = check.resolved_geolocation or "an unmapped country"
    exit_class = "residential" if check.exit_is_residential else "datacenter"
    print(
        f"FAIL: {arguments.geolocation} exit resolved to {check.exit_ip} "
        f"({check.resolved_country_code}, {resolved}, {exit_class})"
    )
    print("Do NOT run the crawl.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
