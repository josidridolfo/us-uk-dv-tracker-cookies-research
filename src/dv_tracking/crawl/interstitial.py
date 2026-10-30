"""
Interstitial and block-page signature matching over a main document's HTML.

Pure text matching only: no network, filesystem, or crawl-stage dependency,
so it runs identically in the crawl container and the analysis-stage
virtualenv. A status check alone misses the case that matters most here --
a 200 response carrying a challenge or block interstitial instead of the
site's own content -- which is what this module exists to catch.

Signature provenance: `_match_bitninja` is confirmed against a captured
block page from this project's own corpus (ashiana.org.uk, 2026-10-30); the
rest are drawn from current anti-bot vendor documentation and scraping
write-ups (Cloudflare, Imperva/Incapsula, Sucuri, Akamai, PerimeterX/HUMAN,
DataDome), none of which this corpus has triggered yet. Each matcher's
docstring states what it requires and why.
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable
from dataclasses import dataclass

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


@dataclass(frozen=True)
class InterstitialMatch:
    """One signature's positive match against a main document's HTML."""

    vendor: str
    evidence: str


def _prepare(html_text: str) -> tuple[str, str]:
    """
    Normalise raw HTML into a lowercase body and a lowercase title for matching.

    `html.unescape` runs before lowercasing: some vendors (Akamai) entity-
    encode the punctuation in their own deny pages, and a raw-string check
    would silently miss them.

    Args:
        html_text: The decoded main-document HTML.
    Returns:
        body: The unescaped, lowercased full document.
        title: The unescaped, lowercased `<title>` text, or "" if absent.
    """
    # Non-breaking space folded to a plain space after unescaping: Akamai's
    # own deny pages entity-encode it (`&#160;`/`&nbsp;`) between "Reference"
    # and "#", and a literal-space substring check would miss them otherwise.
    unescaped = html.unescape(html_text).replace("\xa0", " ")
    title_match = _TITLE_RE.search(unescaped)
    title = title_match.group(1).strip() if title_match else ""
    return unescaped.lower(), title.lower()


def _match_bitninja(body: str, title: str) -> InterstitialMatch | None:
    """BitNinja's Browser Integrity / CAPTCHA challenge page.

    Confirmed against this corpus's own capture (ashiana.org.uk,
    2026-10-30): title "Visitor anti-robot validation"; body text naming
    "BitNinja" and "BitNinja.IO" repeatedly across its localised copy,
    "challenge list" wording, and a `.bn-cap-captcha-wrap` / `g-recaptcha`
    challenge form. The vendor name alone is the signature -- a DV support
    non-profit's own page has no reason to mention it.
    """
    if "bitninja" in body:
        return InterstitialMatch("bitninja", "page body names BitNinja")
    return None


def _match_cloudflare(body: str, title: str) -> InterstitialMatch | None:
    """Cloudflare's JS/managed challenge interstitial.

    Requires at least two of five independent signals rather than one:
    pages that merely mention Cloudflare (a status page, a blog post about
    outages) are common enough that a single "cloudflare" substring would
    be too weak on its own.
    """
    signals = (
        "just a moment" in title,
        "/cdn-cgi/challenge-platform/" in body,
        "cf_chl_opt" in body,
        "__cf_chl_rt_tk" in body,
        "cf-chl" in body,
    )
    if sum(signals) >= 2:
        return InterstitialMatch("cloudflare_challenge", "two or more Cloudflare challenge signals present")
    return None


def _match_incapsula(body: str, title: str) -> InterstitialMatch | None:
    """Imperva/Incapsula's block page: the incident-ID wording is unique to it."""
    for marker in ("incapsula incident id", "_incapsula_resource", "powered by incapsula"):
        if marker in body:
            return InterstitialMatch("incapsula", f"body contains {marker!r}")
    return None


def _match_sucuri(body: str, title: str) -> InterstitialMatch | None:
    """Sucuri's WebSite Firewall / CloudProxy denial page.

    Both phrases are required together: "access denied" alone is the
    generic-interstitial case below, not a vendor identification.
    """
    if "sucuri website firewall" in body and "access denied" in body:
        return InterstitialMatch("sucuri", "Sucuri Website Firewall + Access Denied wording")
    return None


def _match_akamai(body: str, title: str) -> InterstitialMatch | None:
    """Akamai Bot Manager / WAF deny page."""
    if "pardon our interruption" in body:
        return InterstitialMatch("akamai", "'Pardon Our Interruption' wording")
    if "access denied" in body and "reference #" in body:
        return InterstitialMatch("akamai", "Access Denied + Reference # wording")
    return None


def _match_perimeterx(body: str, title: str) -> InterstitialMatch | None:
    """PerimeterX/HUMAN and DataDome's rendered challenge containers.

    Matches the rendered challenge element, not a bare domain substring:
    `captcha-delivery.com` and `px-cdn.net` can legitimately appear in a CSP
    meta tag or a preconnect hint on a page that was never blocked.
    """
    for marker in ('id="px-captcha"', 'class="px-captcha-error-container"', "data-px-captcha"):
        if marker in body:
            return InterstitialMatch("perimeterx", f"challenge container {marker!r} present")
    if re.search(r"<iframe[^>]+captcha-delivery\.com", body):
        return InterstitialMatch("datadome", "DataDome captcha-delivery.com iframe present")
    return None


_ANTIBOT_TITLE_PHRASES: tuple[str, ...] = (
    "are you a robot",
    "security check",
    "verify you are human",
    "unusual traffic",
    "anti-robot validation",
)


def _match_recaptcha_interstitial(body: str, title: str) -> InterstitialMatch | None:
    """A full-page reCAPTCHA challenge, as distinct from a form's own widget.

    This corpus's sites legitimately embed reCAPTCHA on contact and self-
    referral forms, so the widget's presence alone must never be the
    signal. Requires anti-bot phrasing in the page `<title>` as well: a
    contact form's title never carries it.
    """
    has_widget = "g-recaptcha" in body or "grecaptcha.execute" in body
    if not has_widget:
        return None
    for phrase in _ANTIBOT_TITLE_PHRASES:
        if phrase in title:
            return InterstitialMatch("recaptcha_interstitial", f"title contains {phrase!r} with reCAPTCHA widget")
    return None


_GENERIC_TITLE_PHRASES: tuple[str, ...] = (
    "attention required",
    "access denied",
    "checking your browser",
    "please wait while we verify",
    "you have been blocked",
    "pardon our interruption",
    "unusual traffic",
)


def _match_generic_interstitial(body: str, title: str) -> InterstitialMatch | None:
    """A `<title>` naming a well-known challenge/block phrase, vendor unidentified.

    Anchored to the title rather than the body: a legitimate page's body
    text can touch on any of these phrases in passing, but a DV support
    site has no reason to title its own page this way.
    """
    for phrase in _GENERIC_TITLE_PHRASES:
        if phrase in title:
            return InterstitialMatch("generic_interstitial", f"title contains {phrase!r}")
    return None


# Order matters: a vendor-specific match must be tried before the generic
# fallback, since several vendor pages also carry a title the generic
# matcher would otherwise claim first (BitNinja's own title names "robot").
_Matcher = Callable[[str, str], InterstitialMatch | None]
_MATCHERS: tuple[_Matcher, ...] = (
    _match_bitninja,
    _match_cloudflare,
    _match_incapsula,
    _match_sucuri,
    _match_akamai,
    _match_perimeterx,
    _match_recaptcha_interstitial,
    _match_generic_interstitial,
)


def classify_interstitial(html_text: str) -> InterstitialMatch | None:
    """
    Classify a main document's HTML as a block/challenge interstitial, or not.

    Args:
        html_text: The decoded main-document HTML.
    Returns:
        match: The first matching signature, tried in specificity order, or
            None when no signature matched.
    """
    body, title = _prepare(html_text)
    for matcher in _MATCHERS:
        match = matcher(body, title)
        if match is not None:
            return match
    return None
