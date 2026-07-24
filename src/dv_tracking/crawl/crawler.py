"""
Single-page crawl for one site at one geolocation.

A Crawler opens a page through an injected factory, navigates, dwells the
per-trial pre-consent window while recording the network trace, and returns a
RawTrial. The factory owns browser lifetime and teardown, which keeps the
Crawler independent of Playwright for tests.

The crawler captures only raw observations. Registered-domain resolution,
third-party status, and tracker classification are derived downstream in the
analysis stage.
"""

from __future__ import annotations

import os
import secrets
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from urllib.parse import urlparse

from playwright.sync_api import BrowserContext, Page, Request, sync_playwright

from dv_tracking.crawl.models import (
    BannerSignals,
    ConsentomaticBundle,
    LocalStorageSnapshot,
    RawCookie,
    RawRequest,
    RawTrial,
    TCFProbeResult,
)
from dv_tracking.crawl.probes import read_probe
from dv_tracking.sites import DVSupportSite
from dv_tracking.utils import utcnow_iso

NAVIGATION_TIMEOUT_MS: int = 30_000
BLOCKED_STATUSES: tuple[int, ...] = (403, 429, 503)
MAX_NAVIGATION_ATTEMPTS: int = 3
NAVIGATION_RETRY_BASE_DELAY_SECONDS: float = 1.0
DEFAULT_VIEWPORT: dict = {"width": 1920, "height": 1080}
DEFAULT_USER_AGENT: str = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/121.0.0.0 Safari/537.36"
)

# Post-load dwell before capture stops, indexed by trial (1, 2, 3).
WAIT_SECONDS_BY_TRIAL: tuple[float, ...] = (5.0, 10.0, 15.0)
# Post-load delay before the TCF ping, indexed by trial (1, 2, 3).
TCF_PROBE_DELAY_SECONDS_BY_TRIAL: tuple[float, ...] = (1.0, 3.0, 7.0)

DOM_HEURISTIC_KEYWORDS: tuple[str, ...] = (
    "cookie", "cookies", "consent", "privacy", "gdpr", "ccpa",
    "accept all", "reject all", "manage preferences", "tracking",
)
DOM_HEURISTIC_MINIMUM_Z_INDEX: int = 1
DOM_HEURISTIC_EXCERPT_MAX_LENGTH: int = 200

# IPRoyal residential-proxy exit: crawls route through the country residential exit
# unless the machine already exits residentially in the target country.
IPROYAL_DEFAULT_HOST: str = "geo.iproyal.com"
IPROYAL_DEFAULT_PORT: str = "12321"
IPROYAL_SESSION_LIFETIME: str = "10m"
IPROYAL_COUNTRY_BY_GEOLOCATION: dict[str, str] = {"uk": "gb", "us": "us"}


class BlockedNavigationError(Exception):
    """Raised when navigation returns a blocked HTTP status; blocked loads are not retried."""

    def __init__(self, status: int) -> None:
        """
        Record the blocked HTTP status on the exception.

        Args:
            status: The blocked HTTP status code (403, 429 or 503).
        Returns:
            None
        """
        super().__init__(f"blocked navigation status={status}")
        self.status = status


class ProxyConfigurationError(Exception):
    """Raised when the IPRoyal exit credentials or configuration are missing or incomplete."""


def iproyal_proxy_for(
    geolocation: str, environ: Mapping[str, str], session: str
) -> dict[str, str]:
    """
    Build the Playwright proxy settings for the IPRoyal residential exit.

    The password field carries the IPRoyal routing parameters (country, session,
    lifetime); error messages name only the missing variable and never a secret.

    Args:
        geolocation: The exit geolocation label ("uk" or "us").
        environ: The environment mapping supplying credentials and optional host/port.
        session: The sticky-session token appended to the routing parameters.
    Returns:
        proxy: The Playwright proxy dict with server, username, and routed password.
    """
    username = environ.get("IPROYAL_USERNAME")
    if not username:
        raise ProxyConfigurationError(
            f"IPROYAL_USERNAME is required to proxy the {geolocation} exit but is not set"
        )
    password = environ.get("IPROYAL_PASSWORD")
    if not password:
        raise ProxyConfigurationError(
            f"IPROYAL_PASSWORD is required to proxy the {geolocation} exit but is not set"
        )
    host = environ.get("IPROYAL_HOST", IPROYAL_DEFAULT_HOST)
    port = environ.get("IPROYAL_PORT", IPROYAL_DEFAULT_PORT)
    country = IPROYAL_COUNTRY_BY_GEOLOCATION[geolocation]
    return {
        "server": f"http://{host}:{port}",
        "username": username,
        "password": (
            f"{password}_country-{country}_session-{session}"
            f"_lifetime-{IPROYAL_SESSION_LIFETIME}"
        ),
    }


def proxy_decision_for(
    target_geolocation: str, observed_geolocation: str | None, exit_is_residential: bool
) -> bool:
    """
    Decide whether the crawl must route through the proxy for one target geolocation.

    Direct egress is allowed only when the machine already sits in the target
    country on a residential exit; an unknown or non-residential exit routes
    through the proxy (fail-closed).

    Args:
        target_geolocation: The geolocation the crawl must egress from ("uk" or "us").
        observed_geolocation: The machine's current exit geolocation, or None when unknown.
        exit_is_residential: Whether the machine's current exit looks residential.
    Returns:
        use_proxy: True when the crawl must route through the proxy.
    """
    return not (observed_geolocation == target_geolocation and exit_is_residential)


def require_iproyal_credentials(geolocation: str, environ: Mapping[str, str]) -> None:
    """
    Fail closed before any request if the IPRoyal exit credentials are missing.

    Delegates to iproyal_proxy_for with a throwaway session and discards the
    result; a missing credential raises ProxyConfigurationError.

    Args:
        geolocation: The exit geolocation label ("uk" or "us").
        environ: The environment mapping supplying credentials.
    Returns:
        None
    """
    iproyal_proxy_for(geolocation, environ, "preflight")


@contextmanager
def playwright_page_factory(
    geolocation: str,
    *,
    user_agent: str = DEFAULT_USER_AGENT,
    headless: bool = False,
    use_proxy: bool = True,
) -> Iterator[tuple[BrowserContext, Page]]:
    """
    Open a fresh Chromium context and page for one geolocation.

    Args:
        geolocation: The exit geolocation label selecting the browser locale.
        user_agent: The browser user-agent string.
        headless: Whether to launch Chromium headless.
        use_proxy: Whether to route the browser through the IPRoyal exit; when
            False the machine's own egress is used.
    Returns:
        context_and_page: A context manager yielding the (context, page) pair.
    """
    with sync_playwright() as playwright:
        launch_kwargs: dict = {"headless": headless}
        if use_proxy:
            # Proxy is set at launch (browser-level) so no context, and no request,
            # can bypass it when the crawl must route through the exit.
            session = secrets.token_hex(8)
            launch_kwargs["proxy"] = iproyal_proxy_for(geolocation, os.environ, session)
        browser = playwright.chromium.launch(**launch_kwargs)
        try:
            context = browser.new_context(
                viewport=DEFAULT_VIEWPORT,
                user_agent=user_agent,
                locale="en-GB" if geolocation == "uk" else "en-US",
            )
            page = context.new_page()
            try:
                yield context, page
            finally:
                # Best-effort teardown: a close error must not discard a completed trial.
                try:
                    context.close()
                except Exception:
                    pass
        finally:
            # Best-effort teardown after the trial is assembled: a close error
            # cannot affect the captured data.
            try:
                browser.close()
            except Exception:
                pass


class PageProbes:
    """Runs the DOM/JS probes against one live page and returns raw probe records."""

    def __init__(self, page: Page) -> None:
        """
        Bind the probes to a page.

        Args:
            page: The Playwright page to probe.
        Returns:
            None
        """
        self.page = page

    def tcf_probe(self, trial: int) -> TCFProbeResult:
        """
        Wait the per-trial delay, then run the TCF ping probe.

        Args:
            trial: The trial number selecting the post-load probe delay.
        Returns:
            probe: The TCF ping outcome; api_present is False when no CMP API responds.
        """
        time.sleep(TCF_PROBE_DELAY_SECONDS_BY_TRIAL[trial - 1])
        try:
            ping = self.page.evaluate(read_probe("tcf_ping.js"))
        except Exception:
            return TCFProbeResult(
                api_present=False,
                gdpr_applies=None,
                cmp_id=None,
                cmp_loaded=None,
                cmp_status=None,
                display_status=None,
                tcf_policy_version=None,
            )
        if not isinstance(ping, dict):
            return TCFProbeResult(
                api_present=False,
                gdpr_applies=None,
                cmp_id=None,
                cmp_loaded=None,
                cmp_status=None,
                display_status=None,
                tcf_policy_version=None,
            )
        return TCFProbeResult(
            api_present=True,
            gdpr_applies=ping.get("gdprApplies"),
            cmp_id=ping.get("cmpId"),
            cmp_loaded=ping.get("cmpLoaded"),
            cmp_status=ping.get("cmpStatus"),
            display_status=ping.get("displayStatus"),
            tcf_policy_version=ping.get("tcfPolicyVersion"),
        )

    def local_storage(self) -> LocalStorageSnapshot:
        """
        Samples the visited page's first-party (top-origin) localStorage stats.

        Scoped to the top document's origin: cross-origin iframe localStorage is
        not read, so third-party localStorage is out of scope for this measure.

        Args:
            None
        Returns:
            snapshot: The localStorage entries with their key count and byte total, or an empty snapshot with error populated.
        """
        try:
            result = self.page.evaluate(read_probe("local_storage.js"))
        except Exception as error:
            return LocalStorageSnapshot(
                entries=(), key_count=0, total_bytes=0, error=f"{type(error).__name__}: {error}"
            )
        if not isinstance(result, dict):
            return LocalStorageSnapshot(
                entries=(),
                key_count=0,
                total_bytes=0,
                error=f"unexpected probe result: {type(result).__name__}",
            )
        if result.get("error"):
            return LocalStorageSnapshot(
                entries=(), key_count=0, total_bytes=0, error=str(result["error"])
            )
        pairs = tuple(
            (str(pair[0]), str(pair[1]))
            for pair in (result.get("entries") or ())
            if isinstance(pair, (list, tuple)) and len(pair) == 2
        )
        return LocalStorageSnapshot(
            entries=pairs,
            key_count=len(pairs),
            total_bytes=sum(len(key) + len(value) for key, value in pairs),
            error=None,
        )

    def banner_signals(self, consentomatic_bundle: ConsentomaticBundle | None) -> BannerSignals:
        """
        Runs the DOM banner heuristic and the framework/CMP tell probe.

        Probe failure degrades to zeroed fields with error populated; never raises.

        Args:
            consentomatic_bundle: Carries the CMP rule identifiers searched for as
                script-src tells, or None to search for none.
        Returns:
            signals: The raw banner probe signals.
        """
        consentomatic_matches: tuple[str, ...] = ()
        dom_matched = False
        dom_evidence: str | None = None
        close_button_present = False
        quick_exit_present = False
        usp_present = False
        gpp_present = False
        errors: list[str] = []

        try:
            result = self.page.evaluate(
                read_probe("dom_heuristic.js"),
                {
                    "zIndexMin": DOM_HEURISTIC_MINIMUM_Z_INDEX,
                    "keywords": list(DOM_HEURISTIC_KEYWORDS),
                    "excerptMax": DOM_HEURISTIC_EXCERPT_MAX_LENGTH,
                },
            )
            if isinstance(result, dict):
                dom_matched = bool(result.get("matched"))
                dom_evidence = (
                    str(result.get("evidence") or "")[:DOM_HEURISTIC_EXCERPT_MAX_LENGTH] or None
                )
                close_button_present = bool(result.get("close_button_present"))
                quick_exit_present = bool(result.get("quick_exit_present"))
        except Exception as error:
            errors.append(f"dom_heuristic:{type(error).__name__}:{error}")

        rule_identifiers = list(consentomatic_bundle.rules) if consentomatic_bundle else []
        try:
            result = self.page.evaluate(read_probe("consent_framework.js"), rule_identifiers)
            if isinstance(result, dict):
                usp_present = bool(result.get("usp_present"))
                gpp_present = bool(result.get("gpp_present"))
                consentomatic_matches = tuple(str(x) for x in result.get("cmp_matches") or ())
        except Exception as error:
            errors.append(f"consent_framework:{type(error).__name__}:{error}")

        return BannerSignals(
            consentomatic_matches=consentomatic_matches,
            dom_heuristic_matched=dom_matched,
            dom_heuristic_evidence=dom_evidence,
            usp_api_present=usp_present,
            gpp_api_present=gpp_present,
            close_button_present=close_button_present,
            quick_exit_present=quick_exit_present,
            error="; ".join(errors) or None,
        )


class Crawler:
    """Crawls one site visit at a time through an injected page factory."""

    def __init__(
        self,
        page_factory: Callable[[str], AbstractContextManager[tuple[BrowserContext, Page]]] | None = None,
        user_agent: str = DEFAULT_USER_AGENT,
        headless: bool = False,
        use_proxy: bool = True,
        consentomatic_bundle: ConsentomaticBundle | None = None,
    ) -> None:
        """
        Configure the crawler and bind the page factory.

        Args:
            page_factory: Injected factory yielding (context, page) for a geolocation;
                None binds playwright_page_factory with this crawler's user_agent, headless,
                and use_proxy.
            user_agent: The browser user-agent string used by the default factory.
            headless: Whether the default factory launches Chromium headless.
            use_proxy: Whether the default factory routes through the IPRoyal exit;
                True is fail-closed and only lifted by a runtime egress decision.
            consentomatic_bundle: Rule identifiers the banner probe searches for as CMP
                script-src tells, or None.
        Returns:
            None
        """
        self.user_agent = user_agent
        self.headless = headless
        self.use_proxy = use_proxy
        self.consentomatic_bundle = consentomatic_bundle
        if page_factory is None:
            def default_factory(
                geolocation: str,
            ) -> AbstractContextManager[tuple[BrowserContext, Page]]:
                return playwright_page_factory(
                    geolocation, user_agent=user_agent, headless=headless, use_proxy=use_proxy
                )

            self.page_factory = default_factory
        else:
            self.page_factory = page_factory

    def navigate(self, page: Page, url: str) -> int | None:
        """
        Navigate to a URL with bounded exponential-backoff retry.

        A blocked HTTP status (403, 429, 503) raises BlockedNavigationError and
        is not retried; other errors are retried up to MAX_NAVIGATION_ATTEMPTS.

        Args:
            page: The Playwright page to drive.
            url: The absolute URL to navigate to.
        Returns:
            status: The HTTP status of the navigation, or None when no response arrived.
        """
        last_error: Exception = RuntimeError("navigation not attempted")
        for attempt in range(1, MAX_NAVIGATION_ATTEMPTS + 1):
            try:
                response = page.goto(url, wait_until="load", timeout=NAVIGATION_TIMEOUT_MS)
                status = response.status if response else None
                if status in BLOCKED_STATUSES:
                    raise BlockedNavigationError(status)
                return status
            except BlockedNavigationError:
                raise
            except Exception as error:
                last_error = error
                if attempt >= MAX_NAVIGATION_ATTEMPTS:
                    break
                time.sleep(NAVIGATION_RETRY_BASE_DELAY_SECONDS * (2.0 ** (attempt - 1)))
        raise last_error

    def crawl(self, site: DVSupportSite, geolocation: str, trial: int) -> RawTrial:
        """
        Crawl one site at one geolocation and return its raw trial.

        Args:
            site: The DV support site to crawl.
            geolocation: The exit geolocation label, e.g. "uk" or "us".
            trial: The trial number selecting the per-trial dwell.
        Returns:
            trial_record: The raw trial holding the captured network requests.
        """
        captured_requests: list[RawRequest] = []

        def record_request(request: Request) -> None:
            host = urlparse(request.url).hostname or ""
            if host:
                captured_requests.append(
                    RawRequest(
                        url=request.url,
                        host=host,
                        method=request.method,
                        resource_type=request.resource_type,
                    )
                )

        navigation_status: int | None = None
        navigation_error: str | None = None
        blocked = False
        cookies: tuple[RawCookie, ...] = ()
        tcf_probe: TCFProbeResult | None = None
        local_storage: LocalStorageSnapshot | None = None
        banner_signals: BannerSignals | None = None
        time_on_page: float | None = None

        crawl_start_utc = utcnow_iso()
        with self.page_factory(geolocation) as (context, page):
            page.on("request", record_request)
            try:
                navigation_status = self.navigate(page, site.url)
            except BlockedNavigationError as error:
                navigation_status = error.status
                blocked = True
            except Exception as error:
                navigation_error = f"{type(error).__name__}: {error}"
                blocked = True

            if not blocked:
                page_loaded_at = time.monotonic()
                time.sleep(WAIT_SECONDS_BY_TRIAL[trial - 1])
                try:
                    cookies = tuple(RawCookie.from_playwright(c) for c in context.cookies())
                except Exception:
                    cookies = ()
                probes = PageProbes(page)
                tcf_probe = probes.tcf_probe(trial)
                local_storage = probes.local_storage()
                banner_signals = probes.banner_signals(self.consentomatic_bundle)
                time_on_page = time.monotonic() - page_loaded_at
        crawl_end_utc = utcnow_iso()

        return RawTrial(
            site_id=site.site_id,
            url=site.url,
            geolocation=geolocation,
            trial=trial,
            navigation_status=navigation_status,
            navigation_error=navigation_error,
            blocked=blocked,
            requests=tuple(captured_requests),
            cookies=cookies,
            tcf_probe=tcf_probe,
            local_storage=local_storage,
            banner_signals=banner_signals,
            crawl_start_utc=crawl_start_utc,
            crawl_end_utc=crawl_end_utc,
            time_on_page_since_page_load=time_on_page,
        )
