"""
Site-hosting-platform classifier.

Infers the hosting platform from three signals, in priority order:

1. Third-party request host (vendor CDN appearance) -- see VENDOR_HOST_SUFFIXES.
2. First-party WordPress URL path tells (`/wp-content/`, `/wp-json/`, ...).
3. First-party platform-specific cookie names -- see COOKIE_NAME_TELLS.

First match wins. No match returns the custom_or_unknown platform.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import urlparse

from dv_tracking.classify.models import AnalyzedCookie, AnalyzedRequest

# Platform -> third-party host suffixes that reveal the platform's CDN/vendor.
VENDOR_HOST_SUFFIXES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("squarespace", ("squarespace-cdn.com", "sqspcdn.com", "squarespace.com", "static1.squarespace.com")),
    ("wix", ("wixstatic.com", "parastorage.com", "wix.com", "wixsite.com")),
    ("wordpress_com", ("wp.com", "gravatar.com", "s.w.org", "i0.wp.com", "i1.wp.com", "i2.wp.com")),
    ("shopify", ("cdn.shopify.com", "myshopify.com", "shopifycdn.com")),
    ("hubspot", ("hubspot.com", "hs-scripts.com", "hsforms.com", "hs-analytics.net", "hsforms.net", "hubapi.com")),
    ("webflow", ("website-files.com", "webflow.com")),
    ("wp_engine", ("wpengine.com",)),
    ("godaddy", ("secureserver.net",)),
)

WORDPRESS_PATH_TELLS: tuple[str, ...] = (
    "/wp-content/", "/wp-includes/", "/wp-json/", "/xmlrpc.php", "/wp-admin/",
)

# Platform -> first-party cookie names that identify the platform.
COOKIE_NAME_TELLS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("squarespace", ("crumb", "ss_cvt", "ss_cvr", "ss_cvisit", "_nx-nocache", "SS_MID", "test")),
    ("hubspot", ("hubspotutk", "__hstc", "__hssrc", "__hssc", "hubspot.cookies.enabled")),
    ("shopify", ("_shopify_y", "_shopify_s", "cart_currency", "cart_sig", "secure_customer_sig")),
    ("wordpress_self_hosted", ("wp-settings-1", "wp-settings-time-1", "wordpress_test_cookie")),
)


@dataclass(frozen=True)
class HostingPlatform:
    """The detected hosting platform plus the signal class that fired."""

    platform: str
    signal: str
    evidence: str | None


class HostingPlatformClassifier:
    """Infers the site hosting platform from request-host, URL, and cookie signatures."""

    @staticmethod
    def vendor_host_signal(third_party_hosts: Iterable[str]) -> HostingPlatform | None:
        """
        Matches a third-party request host against the vendor CDN suffixes.

        Args:
            third_party_hosts: The third-party request hosts observed.
        Returns:
            platform: The matched platform, or None when no vendor host fired.
        """
        for platform, suffixes in VENDOR_HOST_SUFFIXES:
            for host in third_party_hosts:
                cleaned = host.lower()
                if any(cleaned == suffix or cleaned.endswith("." + suffix) for suffix in suffixes):
                    return HostingPlatform(platform=platform, signal="vendor_host", evidence=host)
        return None

    @staticmethod
    def wordpress_path_signal(first_party_urls: Iterable[str]) -> HostingPlatform | None:
        """
        Matches first-party URL paths against the self-hosted WordPress tells.

        Args:
            first_party_urls: The first-party request URLs observed.
        Returns:
            platform: The wordpress_self_hosted platform, or None when no path tell fired.
        """
        for url in first_party_urls:
            path = urlparse(url).path
            if any(tell in path for tell in WORDPRESS_PATH_TELLS):
                return HostingPlatform(
                    platform="wordpress_self_hosted", signal="wordpress_path", evidence=path
                )
        return None

    @staticmethod
    def cookie_name_signal(first_party_cookie_names: Iterable[str]) -> HostingPlatform | None:
        """
        Matches first-party cookie names against the platform cookie tells.

        Args:
            first_party_cookie_names: The first-party cookie names observed.
        Returns:
            platform: The matched platform, or None when no cookie tell fired.
        """
        cookie_set = set(first_party_cookie_names)
        for platform, tells in COOKIE_NAME_TELLS:
            hits = cookie_set & set(tells)
            if hits:
                return HostingPlatform(platform=platform, signal="cookie_name", evidence=sorted(hits)[0])
        return None

    def classify(
        self, requests: Iterable[AnalyzedRequest], cookies: Iterable[AnalyzedCookie]
    ) -> HostingPlatform:
        """
        Infers the hosting platform from the request trace and first-party cookies.

        Args:
            requests: The analyzed requests for one site visit.
            cookies: The analyzed cookies for one site visit.
        Returns:
            platform: The detected platform; custom_or_unknown when no signal fires.
        """
        requests = tuple(requests)
        third_party_hosts = [request.host for request in requests if request.is_third_party]
        first_party_urls = [request.url for request in requests if not request.is_third_party]
        first_party_cookie_names = [cookie.name for cookie in cookies if not cookie.is_third_party]

        vendor = self.vendor_host_signal(third_party_hosts)
        if vendor is not None:
            return vendor
        path_hit = self.wordpress_path_signal(first_party_urls)
        if path_hit is not None:
            return path_hit
        cookie_hit = self.cookie_name_signal(first_party_cookie_names)
        if cookie_hit is not None:
            return cookie_hit
        return HostingPlatform(platform="custom_or_unknown", signal="default", evidence=None)
