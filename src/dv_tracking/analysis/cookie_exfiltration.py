"""
First-party cookie values (and value/name components) reaching a third-party URL.

Detects when a first-party cookie value (identifier-like) appears in an outgoing
third-party request URL -- verbatim or Base64/MD5/SHA-1/SHA-256 encoded
(full_value) -- and, in reverse, when a long identifier token lifted from the
request URL is a component of a first-party cookie value (value_component) or
name (name_component). This is the URL-channel, un-attributed, lower-bound
subset: it observes the value reaching a third-party endpoint via the URL only.
"""

from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlsplit

from dv_tracking.classify.models import AnalyzedCookie, AnalyzedTrial

MINIMUM_IDENTIFIER_LENGTH: int = 8
MINIMUM_IDENTIFIER_DISTINCT_CHARS: int = 5
HASH_ENCODINGS: frozenset[str] = frozenset({"md5", "sha1", "sha256"})
# Id separators, minus `.` -- keeps the dotted GA client id `<rand>.<ts>` one token.
TOKEN_SEPARATOR_PATTERN: re.Pattern[str] = re.compile(r"[-_/:~|$+,;=&\s]+")


@dataclass(frozen=True)
class ExfilEdge:
    """One first-party cookie value (or component) observed reaching a third-party endpoint."""

    cookie_name: str
    cookie_registered_domain: str
    cookie_vendor: str
    cookie_category: str
    dest_host: str
    dest_registered_domain: str
    dest_url: str
    encoding: str
    value_len: int
    match_kind: str
    matched_token: str

    @staticmethod
    def encode_variants(value: str) -> dict[str, str]:
        """
        The encoded forms a tracker might transmit a value as, keyed by encoding label.

        Args:
            value: The cookie value to encode.
        Returns:
            variants: A mapping of encoding label to the encoded value.
        """
        raw = value.encode()
        standard = base64.b64encode(raw).decode()
        url_safe = base64.urlsafe_b64encode(raw).decode()
        return {
            "plaintext": value,
            "base64_std": standard,
            "base64_std_nopad": standard.rstrip("="),
            "base64_url": url_safe,
            "base64_url_nopad": url_safe.rstrip("="),
            "md5": hashlib.md5(raw).hexdigest(),
            "sha1": hashlib.sha1(raw).hexdigest(),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }

    @staticmethod
    def url_value_haystacks(url: str) -> list[str]:
        """
        The query-parameter values, path, and fragment of a URL -- the value haystacks.

        Args:
            url: The request URL.
        Returns:
            haystacks: The decoded query values, path, and fragment.
        """
        parts = urlsplit(url)
        haystacks = [
            value
            for values in parse_qs(parts.query, keep_blank_values=True).values()
            for value in values
        ]
        if parts.path:
            haystacks.append(unquote(parts.path))
        if parts.fragment:
            haystacks.append(unquote(parts.fragment))
        return haystacks

    @staticmethod
    def is_identifier_value(value: str) -> bool:
        """
        Whether a value is identifier-like: long enough, not numeric, not low-variety.

        Args:
            value: The value to test.
        Returns:
            identifier_like: True when the value passes the false-positive gate.
        """
        return (
            len(value) >= MINIMUM_IDENTIFIER_LENGTH
            and not value.isdigit()
            and len(set(value)) >= MINIMUM_IDENTIFIER_DISTINCT_CHARS
        )

    @staticmethod
    def is_identifier_token(token: str, domain_label: str) -> bool:
        """
        Whether a URL token is an identifier token, excluding the site's own domain label.

        Args:
            token: The token lifted from the request URL.
            domain_label: The first label of the first-party registered domain.
        Returns:
            identifier_token: True when identifier-like and not a first-party domain echo.
        """
        return ExfilEdge.is_identifier_value(token) and (
            not domain_label or domain_label not in token.lower()
        )

    @classmethod
    def find_all(cls, analyzed_trial: AnalyzedTrial) -> list[ExfilEdge]:
        """
        Finds first-party cookie values and components reaching a third-party request URL.

        Args:
            analyzed_trial: The analyzed trial whose cookies and requests are scanned.
        Returns:
            edges: One edge per (cookie, destination, match_kind, encoding), deduplicated.
        """
        first_party_cookies = [
            cookie for cookie in analyzed_trial.analyzed_cookies if not cookie.is_third_party
        ]
        if not first_party_cookies:
            return []
        domain_label = next(
            (
                cookie.registered_domain.split(".")[0].lower()
                for cookie in first_party_cookies
                if cookie.registered_domain
            ),
            "",
        )
        needles: dict[str, tuple[AnalyzedCookie, str]] = {}
        for cookie in first_party_cookies:
            if cls.is_identifier_value(cookie.value):
                for encoding, needle in cls.encode_variants(cookie.value).items():
                    needles.setdefault(needle, (cookie, encoding))

        edges: dict[tuple, ExfilEdge] = {}
        full_covered: set[tuple] = set()
        pending: list[tuple[AnalyzedCookie, str, str, str, str, str]] = []
        for request in analyzed_trial.analyzed_requests:
            if not request.is_third_party:
                continue
            haystacks = cls.url_value_haystacks(request.url)
            lowered = [haystack.lower() for haystack in haystacks]
            # Direction 1: a whole cookie value reaches the URL, any encoding.
            for needle, (cookie, encoding) in needles.items():
                if cookie.registered_domain == request.registered_domain:
                    continue
                searched = lowered if encoding in HASH_ENCODINGS else haystacks
                if any(needle in haystack for haystack in searched):
                    destination = (cookie.name, request.host, request.registered_domain)
                    full_covered.add(destination)
                    edges.setdefault(
                        (*destination, "full_value", encoding),
                        cls.build_edge(cookie, request.host, request.registered_domain,
                                       request.url, encoding, "full_value", needle),
                    )
            # Direction 2: an id token from the URL is a component of a first-party cookie.
            tokens = {
                token
                for haystack in haystacks
                for token in TOKEN_SEPARATOR_PATTERN.split(haystack)
                if cls.is_identifier_token(token, domain_label)
            }
            for token in tokens:
                for cookie in first_party_cookies:
                    if cookie.registered_domain == request.registered_domain:
                        continue
                    if token in cookie.value and len(token) < len(cookie.value):
                        pending.append(
                            (cookie, request.host, request.registered_domain, request.url,
                             "value_component", token)
                        )
                    elif token in cookie.name and token not in cookie.value:
                        pending.append(
                            (cookie, request.host, request.registered_domain, request.url,
                             "name_component", token)
                        )

        # Resolved after the pass so full_covered is complete regardless of request order.
        for cookie, host, registered_domain, url, kind, token in pending:
            destination = (cookie.name, host, registered_domain)
            if kind == "value_component" and destination in full_covered:
                continue
            edges.setdefault(
                (*destination, kind, "plaintext"),
                cls.build_edge(cookie, host, registered_domain, url, "plaintext", kind, token),
            )
        return list(edges.values())

    @classmethod
    def build_edge(
        cls,
        cookie: AnalyzedCookie,
        dest_host: str,
        dest_registered_domain: str,
        dest_url: str,
        encoding: str,
        match_kind: str,
        matched_token: str,
    ) -> ExfilEdge:
        """
        Builds one exfiltration edge from a cookie and its destination request.

        Args:
            cookie: The first-party cookie whose value or component was found.
            dest_host: The destination request host.
            dest_registered_domain: The destination registered domain.
            dest_url: The destination request URL.
            encoding: The encoding label the value matched under.
            match_kind: One of "full_value", "value_component", "name_component".
            matched_token: The needle or token that matched.
        Returns:
            edge: The exfiltration edge.
        """
        return cls(
            cookie_name=cookie.name,
            cookie_registered_domain=cookie.registered_domain,
            cookie_vendor=cookie.cookie_vendor,
            cookie_category=cookie.cookie_category,
            dest_host=dest_host,
            dest_registered_domain=dest_registered_domain,
            dest_url=dest_url,
            encoding=encoding,
            value_len=len(cookie.value),
            match_kind=match_kind,
            matched_token=matched_token,
        )
