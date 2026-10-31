"""
Party resolution for the OpenWPM analysis stage: whether a request went to a third party.

This is the analysis-stage definition on the OpenWPM path. The Playwright-era
`dv_tracking.classify` pipeline still resolves domains through
`dv_tracking.utils`, against whichever snapshot the installed `tldextract`
bundles, so until that path is retired the tree carries two party rules and only
this one is pinned.

Two notions, both of which OpenWPM's extension originally computed and no longer
does. `http_requests.is_third_party_to_top_window` and `is_third_party_channel`
are NULL on every captured row, because the upstream computation used an XPCOM
service a WebExtension cannot reach. They are reproduced here rather than
collapsed into one, because they disagree inside nested frames and consent
platforms commonly render in one:

    party_to_top_level_site    the request's party differs from `top_level_url`'s
    party_to_loading_document  the request's party differs from `loading_origin`'s

A request served into an advertising iframe from that iframe's own vendor is
third-party to the site and first-party to its loading document. The top-level
notion is the one a headline claim about visitor exposure uses.

Registered domains come from the vendored public-suffix snapshot pinned in
`constants.PUBLIC_SUFFIX_SNAPSHOT`, never from a live fetch and never from
tldextract's bundled fallback. The snapshot's digest and its self-declared
version and commit header are both checked before the first resolution, so a
swapped, upgraded or truncated list fails closed instead of quietly moving a
number.
"""

from __future__ import annotations

import hashlib
import ipaddress
import statistics
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import tldextract

from dv_tracking.constants import PUBLIC_SUFFIX_SNAPSHOT

# What a party identity resolved to. Only the first three identify a party that
# can be compared with another; the last two mark a comparison that cannot be made.
PartyKind = Literal[
    "registered_domain",
    "ip_literal",
    "suffixless_host",
    "non_network",
    "unresolvable",
]
_COMPARABLE_KINDS: frozenset[PartyKind] = frozenset(
    {"registered_domain", "ip_literal", "suffixless_host"}
)

PartyRelation = Literal["first_party", "third_party", "undetermined"]

# Which of the two notions a collected result was built under.
Notion = Literal["top_level_site", "loading_document"]

# Allowlist of schemes carrying a network authority; anything else is "non_network".
_NETWORK_SCHEMES: frozenset[str] = frozenset({"http", "https", "ws", "wss", "ftp"})

# OpenWPM writes the JavaScript strings through to SQLite, so a missing
# loading_origin arrives as "undefined" and an opaque one as "null".
ABSENT_VALUES: frozenset[str] = frozenset({"", "undefined", "null"})

_VERSION_PREFIX: str = "// VERSION:"
_COMMIT_PREFIX: str = "// COMMIT:"


class PublicSuffixPinError(RuntimeError):
    """Raised when the vendored public-suffix list is absent or is not the pinned snapshot."""


@dataclass(frozen=True)
class PartyIdentity:
    """The party a URL or host belongs to, as resolved under the pinned public-suffix list."""

    kind: PartyKind
    value: str

    @property
    def is_comparable(self) -> bool:
        """Whether this identity names a party that can be compared with another."""
        return self.kind in _COMPARABLE_KINDS


@dataclass(frozen=True)
class RequestParty:
    """One request's own party identity and its relation to the site and to its loading document."""

    identity: PartyIdentity
    party_to_top_level_site: PartyRelation
    party_to_loading_document: PartyRelation


@dataclass(frozen=True, kw_only=True)
class ObservedRequest:
    """One `http_requests` row reduced to the site it belongs to and the columns party resolution reads.

    Keyword-only on purpose: `url`, `top_level_url` and `loading_origin` are
    three adjacent same-typed URLs, and transposing the last two inverts both
    notions silently.
    """

    site_id: str
    url: str
    top_level_url: str | None
    loading_origin: str | None


@dataclass(frozen=True)
class ThirdPartyDomainsBySite:
    """Each site's distinct third-party identities under one named notion, with its undetermined tally.

    The notion travels with the numbers, so a count cannot be detached from the
    definition that produced it and reported under the other one.

    Attributes:
        notion: Which reference document the third-party judgement was made against.
        domains_by_site: Third-party identity values keyed by site id; a site
            whose requests were all first-party is present with an empty set.
        undetermined_by_site: Requests per site whose party could not be
            determined, keyed by site id. The data-quality denominator for the
            counts above: a non-network URL, or a reference document that does
            not resolve, lands here rather than in either party.
    """

    notion: Notion
    domains_by_site: Mapping[str, frozenset[str]]
    undetermined_by_site: Mapping[str, int]


@dataclass(frozen=True)
class PerSiteDistinctCounts:
    """Distinct third-party count per site, and the corpus-level sum, mean and median of those counts.

    Answers "how many third parties does a site in this corpus carry". The same
    domain seen on five sites is counted five times, once per site; `total` is
    therefore larger than the number of distinct companies in the corpus, which
    is what `CorpusDomainUnion.distinct_count` reports instead.

    Attributes:
        notion: The notion the underlying judgement was made under.
        by_site: Distinct third-party count, keyed by site id; sites with none are present at 0.
        site_count: Number of sites contributing a count.
        total: Sum of the per-site counts.
        mean: Arithmetic mean of the per-site counts, or None for an empty corpus.
        median: Median of the per-site counts, or None for an empty corpus.
        undetermined_total: Requests across the corpus whose party could not be determined.
    """

    notion: Notion
    by_site: Mapping[str, int]
    site_count: int
    total: int
    mean: float | None
    median: float | None
    undetermined_total: int


@dataclass(frozen=True)
class CorpusDomainUnion:
    """The set of third-party identities appearing anywhere in a corpus, counted once each.

    Answers "how many distinct third parties appear anywhere in this corpus",
    which is at most `PerSiteDistinctCounts.total` over the same input.

    Attributes:
        notion: The notion the underlying judgement was made under.
        domains: Every third-party identity value observed, deduplicated across sites.
        site_count: Number of sites the union was taken over.
        undetermined_total: Requests across the corpus whose party could not be determined.
    """

    notion: Notion
    domains: frozenset[str]
    site_count: int
    undetermined_total: int

    @property
    def distinct_count(self) -> int:
        """Number of distinct third-party identities in the corpus."""
        return len(self.domains)


def party_identity_of_url(url: str | None) -> PartyIdentity:
    """
    Resolve the party a URL belongs to.

    Userinfo, port, case and a trailing root dot are normalised away. A
    non-network scheme resolves to "non_network" carrying the scheme, an absent
    or scheme-less value to "unresolvable"; neither takes part in a comparison.
    `blob:` and `filesystem:` do inherit their creator's origin, so treating
    them as no party is the conservative reading rather than a claim about the
    schemes: a request under one is left out of a count instead of attributed.

    Args:
        url: A request URL, top-level URL, or origin; None and OpenWPM's
            "undefined"/"null" sentinels are treated as absent.
    Returns:
        identity: The resolved party identity.
    """
    if url is None or url.strip().lower() in ABSENT_VALUES:
        return PartyIdentity(kind="unresolvable", value="")
    try:
        parts = urlsplit(url.strip())
        scheme = parts.scheme.lower()
        hostname = parts.hostname
    except ValueError:
        return PartyIdentity(kind="unresolvable", value="")
    if scheme and scheme not in _NETWORK_SCHEMES:
        return PartyIdentity(kind="non_network", value=scheme)
    if not scheme or not hostname:
        return PartyIdentity(kind="unresolvable", value="")
    return party_identity_of_host(hostname)


def party_identity_of_host(host: str | None) -> PartyIdentity:
    """
    Resolve the party a bare host belongs to.

    Accepts a `javascript_cookies.host` value directly: the leading dot of a
    domain cookie is stripped, so cookie party-ness and request party-ness
    resolve through the same rule. Internationalised hosts are normalised to
    punycode so a Unicode and an A-label spelling of one host resolve to one
    identity. That normalisation runs through the stdlib IDNA2003 codec, which
    disagrees with the browser's UTS#46 mapping on a narrow class of hosts
    (sharp s, final sigma); Firefox serialises to A-labels before the extension
    observes a request, so a captured host arrives already normalised.

    Args:
        host: A hostname, cookie host, or IP literal, with or without IPv6
            brackets; None and OpenWPM's "undefined"/"null" sentinels are
            treated as absent.
    Returns:
        identity: The resolved party identity.
    """
    if host is None:
        return PartyIdentity(kind="unresolvable", value="")
    normalised = host.strip().lower().strip(".").strip("[]")
    if normalised in ABSENT_VALUES:
        return PartyIdentity(kind="unresolvable", value="")
    normalised = _to_punycode(normalised)
    try:
        return PartyIdentity(kind="ip_literal", value=str(ipaddress.ip_address(normalised)))
    except ValueError:
        pass
    registered_domain = _public_suffix_extractor()(normalised).top_domain_under_public_suffix
    if registered_domain:
        return PartyIdentity(kind="registered_domain", value=registered_domain)
    return PartyIdentity(kind="suffixless_host", value=normalised)


def relation_between(subject: PartyIdentity, reference: PartyIdentity) -> PartyRelation:
    """
    Relate one party identity to another.

    Args:
        subject: The identity being classified, usually a request's.
        reference: The identity it is classified against, usually a document's.
    Returns:
        relation: "undetermined" when either side is not comparable, otherwise
            "first_party" on an exact match and "third_party" on a difference.
    """
    if not subject.is_comparable or not reference.is_comparable:
        return "undetermined"
    return "first_party" if subject == reference else "third_party"


def resolve_request_party(
    *, url: str, top_level_url: str | None, loading_origin: str | None
) -> RequestParty:
    """
    Resolve both party notions for one captured request.

    `loading_origin` is absent on a top-level navigation, which has no loading
    document; that leaves `party_to_loading_document` "undetermined" rather than
    silently substituting the top-level URL.

    Args:
        url: The request URL from `http_requests.url`.
        top_level_url: The visited page URL from `http_requests.top_level_url`.
        loading_origin: The loading document's origin from `http_requests.loading_origin`.
    Returns:
        party: The request's own identity and its relation to the site and to its loading document.
    """
    identity = party_identity_of_url(url)
    return RequestParty(
        identity=identity,
        party_to_top_level_site=relation_between(identity, party_identity_of_url(top_level_url)),
        party_to_loading_document=relation_between(identity, party_identity_of_url(loading_origin)),
    )


def third_party_domains_to_top_level_site(
    requests: Iterable[ObservedRequest],
) -> ThirdPartyDomainsBySite:
    """
    Collect each site's distinct third-party-to-the-site identities.

    The notion a headline exposure claim uses: a party the visitor met that is
    not the site they chose.

    Args:
        requests: The captured requests, each carrying the site it belongs to.
    Returns:
        collected: The per-site third-party sets labelled "top_level_site".
    """
    return _domains_by_site(
        requests, notion="top_level_site", relation_of=lambda party: party.party_to_top_level_site
    )


def third_party_domains_to_loading_document(
    requests: Iterable[ObservedRequest],
) -> ThirdPartyDomainsBySite:
    """
    Collect each site's distinct third-party-to-the-loading-document identities.

    Differs from the site notion inside nested frames, and can include the
    site's own registered domain where the site's document is fetched from
    inside a third party's frame.

    Args:
        requests: The captured requests, each carrying the site it belongs to.
    Returns:
        collected: The per-site third-party sets labelled "loading_document".
    """
    return _domains_by_site(
        requests,
        notion="loading_document",
        relation_of=lambda party: party.party_to_loading_document,
    )


def per_site_distinct_counts(collected: ThirdPartyDomainsBySite) -> PerSiteDistinctCounts:
    """
    Count third parties per site and summarise those counts across the corpus.

    Args:
        collected: The per-site third-party sets under one notion.
    Returns:
        counts: The per-site counts with their sum, mean and median, carrying the notion forward.
    """
    by_site = {site_id: len(domains) for site_id, domains in collected.domains_by_site.items()}
    values = list(by_site.values())
    return PerSiteDistinctCounts(
        notion=collected.notion,
        by_site=by_site,
        site_count=len(values),
        total=sum(values),
        mean=statistics.fmean(values) if values else None,
        median=float(statistics.median(values)) if values else None,
        undetermined_total=sum(collected.undetermined_by_site.values()),
    )


def corpus_domain_union(collected: ThirdPartyDomainsBySite) -> CorpusDomainUnion:
    """
    Union the third parties of every site into one deduplicated set.

    Args:
        collected: The per-site third-party sets under one notion.
    Returns:
        union: Every third-party identity observed anywhere, counted once each,
            carrying the notion forward.
    """
    union: set[str] = set()
    for domains in collected.domains_by_site.values():
        union |= domains
    return CorpusDomainUnion(
        notion=collected.notion,
        domains=frozenset(union),
        site_count=len(collected.domains_by_site),
        undetermined_total=sum(collected.undetermined_by_site.values()),
    )


def _domains_by_site(
    requests: Iterable[ObservedRequest],
    *,
    notion: Notion,
    relation_of: Callable[[RequestParty], PartyRelation],
) -> ThirdPartyDomainsBySite:
    collected: dict[str, set[str]] = {}
    undetermined: dict[str, int] = {}
    for request in requests:
        domains = collected.setdefault(request.site_id, set())
        undetermined.setdefault(request.site_id, 0)
        party = resolve_request_party(
            url=request.url,
            top_level_url=request.top_level_url,
            loading_origin=request.loading_origin,
        )
        relation = relation_of(party)
        if relation == "third_party":
            domains.add(party.identity.value)
        elif relation == "undetermined":
            undetermined[request.site_id] += 1
    return ThirdPartyDomainsBySite(
        notion=notion,
        domains_by_site={site_id: frozenset(d) for site_id, d in collected.items()},
        undetermined_by_site=undetermined,
    )


def _to_punycode(host: str) -> str:
    if host.isascii():
        return host
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        # IDNA2003 rejects labels Firefox resolves; the Unicode form stays
        # comparable with itself, which is all party-ness needs.
        return host


@cache
def _public_suffix_extractor() -> tldextract.TLDExtract:
    """
    Build the extractor bound to the pinned public-suffix snapshot.

    Returns:
        extractor: A tldextract instance reading only the vendored snapshot.
    Raises:
        PublicSuffixPinError: If the vendored file is missing, or its digest or
            its version and commit header does not match
            `constants.PUBLIC_SUFFIX_SNAPSHOT`.
    """
    snapshot = PUBLIC_SUFFIX_SNAPSHOT
    version, commit = _snapshot_header(snapshot.path)
    if (version, commit) != (snapshot.list_version, snapshot.pinned_commit):
        raise PublicSuffixPinError(
            f"public suffix list at {snapshot.path} is VERSION {version!r} COMMIT {commit!r}, "
            f"but the pin expects {snapshot.list_version!r} / {snapshot.pinned_commit!r}"
        )
    digest = hashlib.sha256(snapshot.path.read_bytes()).hexdigest()
    if digest != snapshot.sha256:
        raise PublicSuffixPinError(
            f"public suffix list at {snapshot.path} hashes to {digest}, "
            f"but the pin expects {snapshot.sha256}"
        )
    return tldextract.TLDExtract(
        suffix_list_urls=(snapshot.path.as_uri(),),
        fallback_to_snapshot=False,
        cache_dir=None,
        include_psl_private_domains=snapshot.include_private_domains,
    )


def _snapshot_header(path: Path) -> tuple[str, str]:
    if not path.is_file():
        raise PublicSuffixPinError(f"vendored public suffix list not found at {path}")
    version = ""
    commit = ""
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.startswith(_VERSION_PREFIX):
                version = line[len(_VERSION_PREFIX) :].strip()
            elif line.startswith(_COMMIT_PREFIX):
                commit = line[len(_COMMIT_PREFIX) :].strip()
            elif line.strip() and not line.startswith("//"):
                break
            if version and commit:
                break
    return version, commit
