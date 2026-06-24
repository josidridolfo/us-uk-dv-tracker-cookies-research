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

import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from urllib.parse import urlparse

from playwright.sync_api import BrowserContext, Page, Request, sync_playwright

from dv_tracking.crawl.models import (
    BannerSignals,
    RawCookie,
    RawRequest,
    RawTrial,
    TCFProbeResult,
)
from dv_tracking.crawl.probes import read_probe
from dv_tracking.sites import DVSupportSite
from dv_tracking.utils import is_third_party_to_origin, registered_domain_for_host, utcnow_iso

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

DOM_HEURISTIC_KEYWORDS: tuple[str, ...] = (
    "cookie", "cookies", "consent", "privacy", "gdpr", "ccpa",
    "accept all", "reject all", "manage preferences", "tracking",
)
DOM_HEURISTIC_MINIMUM_Z_INDEX: int = 1
DOM_HEURISTIC_EXCERPT_MAX_LENGTH: int = 200


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


@contextmanager
def playwright_page_factory(
    geolocation: str, *, user_agent: str = DEFAULT_USER_AGENT, headless: bool = False
) -> Iterator[tuple[BrowserContext, Page]]:
    """
    Open a fresh Chromium context and page for one geolocation.

    Args:
        geolocation: The exit geolocation label selecting the browser locale.
        user_agent: The browser user-agent string.
        headless: Whether to launch Chromium headless.
    Returns:
        context_and_page: A context manager yielding the (context, page) pair.
    """
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
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

    def tcf_probe(self, trial: int) -> TCFProbeResult | None:
        # todo - build tcf field
        return None

    def banner_signals(self, consentomatic_bundle: object | None) -> BannerSignals:
        """
        Run the DOM banner heuristic and report the DOM-derived signals.

        Probe failure degrades to zeroed fields with error populated; never raises.

        Args:
            consentomatic_bundle: Reserved for the framework probe; unused at this stage.
        Returns:
            signals: The raw banner probe signals from the DOM heuristic.
        """
        dom_matched = False
        dom_evidence: str | None = None
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
        except Exception as error:
            errors.append(f"dom_heuristic:{type(error).__name__}:{error}")

        return BannerSignals(
            consentomatic_matches=(),
            dom_heuristic_matched=dom_matched,
            dom_heuristic_evidence=dom_evidence,
            error="; ".join(errors) or None,
        )


class Crawler:
    """Crawls one site visit at a time through an injected page factory."""

    def __init__(
        self,
        page_factory: Callable[[str], AbstractContextManager[tuple[BrowserContext, Page]]] | None = None,
        user_agent: str = DEFAULT_USER_AGENT,
        headless: bool = False,
        consentomatic_bundle: object | None = None,
    ) -> None:
        """
        Configure the crawler and bind the page factory.

        Args:
            page_factory: Injected factory yielding (context, page) for a geolocation;
                None binds playwright_page_factory with this crawler's user_agent and headless.
            user_agent: The browser user-agent string used by the default factory.
            headless: Whether the default factory launches Chromium headless.
            consentomatic_bundle: Rule identifiers the banner probe searches for as CMP
                script-src tells, or None.
        Returns:
            None
        """
        self.user_agent = user_agent
        self.headless = headless
        self.consentomatic_bundle = consentomatic_bundle
        if page_factory is None:
            def default_factory(
                geolocation: str,
            ) -> AbstractContextManager[tuple[BrowserContext, Page]]:
                return playwright_page_factory(
                    geolocation, user_agent=user_agent, headless=headless
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
        page_origin_domain = registered_domain_for_host(urlparse(site.url).hostname or "")

        def record_request(request: Request) -> None:
            host = urlparse(request.url).hostname or ""
            if host:
                registered_domain = registered_domain_for_host(host)
                captured_requests.append(
                    RawRequest(
                        url=request.url,
                        host=host,
                        method=request.method,
                        resource_type=request.resource_type,
                        registered_domain=registered_domain,
                        is_third_party=is_third_party_to_origin(registered_domain, page_origin_domain),
                    )
                )

        navigation_status: int | None = None
        navigation_error: str | None = None
        blocked = False
        cookies: tuple[RawCookie, ...] = ()
        tcf_probe: TCFProbeResult | None = None
        banner_signals: BannerSignals | None = None

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
                time.sleep(WAIT_SECONDS_BY_TRIAL[trial - 1])
                try:
                    cookies = tuple(
                        RawCookie.from_playwright(c, page_origin_domain) for c in context.cookies()
                    )
                except Exception:
                    cookies = ()
                probes = PageProbes(page)
                tcf_probe = probes.tcf_probe(trial)
                banner_signals = probes.banner_signals(self.consentomatic_bundle)
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
            banner_signals=banner_signals,
            crawl_start_utc=crawl_start_utc,
            crawl_end_utc=crawl_end_utc,
        )
