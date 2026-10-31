"""
Per-visit block verdicts and the site-level exclusion rule they feed.

A visit is classified from evidence gathered on three independent routes,
tried in order of strength, every one of which stays auditable on the
built verdict:

1. The main document's own HTTP response status -- the strongest signal,
   and the only one that needs no further corroboration.
2. WAF/CDN vendor signatures: the main response's own headers (`cf-mitigated`,
   `akamai-grn`, `x-iinfo`) and known challenge-vendor request hosts
   (`admin.bitninja.io`, `challenges.cloudflare.com`, `*.incapsula.com`,
   `geo.captcha-delivery.com`). Both come straight out of
   `crawl-data.sqlite`, so they run even when no content store exists.
3. A title/body interstitial signature over the retained main-document
   HTML (`dv_tracking.crawl.interstitial`) -- the only route that needs the
   content store, hence the only one `signature_assessable` can be False for.

First-party and total request counts are carried as auxiliary evidence,
never as a classification input: cookie counts collapse far less than
request counts on a real block (confirmed on this project's own pilot
pair, 17/14 cookies against 159/2 first-party requests), so the count is
useful context for a human reviewing a verdict but is not itself decisive
-- a genuinely light page is a legitimate outcome, not a block.

Fetching the retained HTML needs `plyvel`, not available outside the crawl
container, so this module takes HTML as a plain string argument rather than
reading the content store itself; the fetch side lives in
`dv_tracking.crawl.content_store`, and only the container-side CLI
(`dv_tracking.crawl.detect_blocks`) imports both. Everything else here is
stdlib plus `dv_tracking.crawl.interstitial`, so it runs and tests
identically in the crawl container and the analysis virtualenv.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Sequence
from contextlib import closing
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Self
from urllib.parse import urlsplit

from dv_tracking.crawl.interstitial import InterstitialMatch, classify_interstitial

BLOCK_DETECTION_SCHEMA_VERSION: str = "1"
MAIN_FRAME_RESOURCE_TYPE: str = "main_frame"
BLOCKED_STATUS_THRESHOLD: int = 400

# (header name lowercased, substring required in its value, vendor). Presence
# of the header is the whole signal when the required substring is "".
_HEADER_SIGNATURES: tuple[tuple[str, str, str], ...] = (
    ("cf-mitigated", "challenge", "cloudflare_challenge"),
    ("akamai-grn", "", "akamai"),
    ("x-iinfo", "", "incapsula"),
)

# (marker host, vendor). A request to the marker host or any of its
# subdomains is the signal.
_VENDOR_HOST_SIGNATURES: tuple[tuple[str, str], ...] = (
    ("admin.bitninja.io", "bitninja"),
    ("challenges.cloudflare.com", "cloudflare_challenge"),
    ("geo.captcha-delivery.com", "datadome"),
    ("incapsula.com", "incapsula"),
)


class BlockOutcome(StrEnum):
    """A visit's overall block verdict."""

    SERVED = "served"
    BLOCKED_STATUS = "blocked_status"
    BLOCKED_SIGNATURE = "blocked_signature"


def _host(url: str) -> str:
    """Extract the lowercased hostname from a URL, or "" when it has none."""
    return (urlsplit(url).hostname or "").lower()


def _same_site(request_host: str, page_host: str) -> bool:
    """
    Report whether a request host is first-party to the visited page's host.

    No public-suffix list is consulted -- `tldextract` is unavailable in
    the crawl container (see the module docstring of
    `dv_tracking.crawl.openwpm_run`) -- so this is a host-suffix heuristic,
    not the registrable-domain comparison the analysis stage's own
    classifiers use. It only feeds an auxiliary evidence count, never a
    block decision, so the approximation is deliberate: a `www.` prefix is
    stripped, then either host must be a dot-bounded suffix of the other.

    Args:
        request_host: The request's hostname, lowercased.
        page_host: The visited page's hostname, lowercased.
    Returns:
        first_party: Whether the two hosts are the same site under this heuristic.
    """
    a = request_host.removeprefix("www.")
    b = page_host.removeprefix("www.")
    return a == b or a.endswith(f".{b}") or b.endswith(f".{a}")


def _match_response_headers(headers_blobs: Iterable[str]) -> InterstitialMatch | None:
    """
    Match a known WAF/CDN response-header marker across the observed header blobs.

    Both the JSON parse and the pair-unpacking are guarded by one `try`:
    `headers` is live-captured HTTP data from arbitrary third-party sites,
    not project-controlled output, so a blob that parses as JSON but is not
    shaped as a list of `[name, value]` pairs (wrong arity, a bare dict, a
    scalar) must degrade to "no match" rather than raise out of a read-only
    evidence pass.
    """
    for blob in headers_blobs:
        try:
            pairs = json.loads(blob)
            lowered = {str(name).lower(): str(value).lower() for name, value in pairs}
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        for name, needle, vendor in _HEADER_SIGNATURES:
            if name in lowered and needle in lowered[name]:
                return InterstitialMatch(vendor, f"response header {name!r} matched")
    return None


def _match_vendor_hosts(request_hosts: Iterable[str]) -> InterstitialMatch | None:
    """Match a known challenge-vendor request host among the visit's observed hosts."""
    for host in request_hosts:
        for marker_host, vendor in _VENDOR_HOST_SIGNATURES:
            if host == marker_host or host.endswith(f".{marker_host}"):
                return InterstitialMatch(vendor, f"request to {host!r} observed")
    return None


@dataclass(frozen=True)
class VisitEvidence:
    """Everything one visit's sqlite rows contribute to its block verdict, before HTML.

    Every field is populated from `crawl-data.sqlite` alone, so this is
    exactly what the pass can see for a cell with no content store.
    """

    visit_id: int
    statuses: tuple[int, ...]
    content_hash: str | None
    response_header_blobs: tuple[str, ...]
    request_hosts: frozenset[str]
    total_request_count: int
    first_party_request_count: int

    @property
    def header_or_host_signature(self) -> InterstitialMatch | None:
        """The WAF/CDN signature found in response headers or request hosts, if any."""
        return _match_response_headers(self.response_header_blobs) or _match_vendor_hosts(
            self.request_hosts
        )


@dataclass
class _MainFrameAccumulator:
    """Mutable per-visit accumulator for the main-document query, before VisitEvidence is built.

    `latest_content_hash` tracks the response with the greatest
    `time_stamp` seen so far, not merely the last or the lexicographically
    smallest hash: the redirect-duplication case (a second `main_frame`
    request row sharing the first row's `request_id`) collapses to one
    hash regardless, but a visit with genuinely distinct main-document
    responses -- an in-page reload or meta-refresh landing on a different
    body -- should resolve to whichever one is temporally last, the state
    the page was actually left in. `time_stamp` strings compare correctly
    under plain string ordering because OpenWPM writes them in one
    consistent ISO-8601 UTC format.
    """

    statuses: set[int] = field(default_factory=set)
    header_blobs: set[str] = field(default_factory=set)
    latest_time_stamp: str | None = None
    latest_content_hash: str | None = None

    def observe(self, *, status: int | None, content_hash: str | None, headers: str | None, time_stamp: str | None) -> None:
        """Fold one main-document response row into this accumulator."""
        if status is not None:
            self.statuses.add(status)
        if headers is not None:
            self.header_blobs.add(headers)
        if content_hash is not None and (self.latest_time_stamp is None or (time_stamp or "") >= self.latest_time_stamp):
            self.latest_time_stamp = time_stamp
            self.latest_content_hash = content_hash


def fetch_visit_evidence(db_path: Path) -> dict[int, VisitEvidence]:
    """
    Read every visit's main-document response(s) and request-level evidence.

    The main-document query joins on `(visit_id, request_id)` -- OpenWPM's
    own network request identifier -- rather than `http_requests.id`. A
    redirect hop recorded as a second `main_frame` request row shares the
    first row's `request_id` and the one underlying response; joining on
    `id` would duplicate that single response once per request row sharing
    it, inflating one status into two identical-looking rows. `GROUP BY`
    collapses exactly that duplication back down.

    Guards the same risk window `dv_tracking.crawl.resume._terminal_outcomes`
    was written to defend against on this same file: a database that
    exists but predates OpenWPM writing its schema (process killed between
    `TaskManager` opening it and the first write) has no `http_requests`
    table yet, and querying it would raise rather than reading as "nothing
    here yet."

    Args:
        db_path: The cell's crawl-data.sqlite path.
    Returns:
        evidence: visit_id -> VisitEvidence, for every visit with at least
            one main_frame request recorded. A visit whose main_frame
            request never got a matching response row (the load aborted
            before any response landed) gets empty statuses and headers.
    """
    if not db_path.is_file() or db_path.stat().st_size == 0:
        return {}
    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as connection:
        has_http_requests = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'http_requests'"
        ).fetchone()
        if has_http_requests is None:
            return {}
        main_rows = connection.execute(
            """
            SELECT hr.visit_id, hresp.response_status, hresp.content_hash, hresp.headers, hresp.time_stamp
            FROM http_requests AS hr
            LEFT JOIN http_responses AS hresp
                ON hresp.visit_id = hr.visit_id AND hresp.request_id = hr.request_id
            WHERE hr.resource_type = ?
            GROUP BY hr.visit_id, hresp.response_status, hresp.content_hash, hresp.headers, hresp.time_stamp
            """,
            (MAIN_FRAME_RESOURCE_TYPE,),
        ).fetchall()
        request_rows = connection.execute("SELECT visit_id, url FROM http_requests").fetchall()
        page_urls = dict(connection.execute("SELECT visit_id, site_url FROM site_visits").fetchall())

    main_by_visit: dict[int, _MainFrameAccumulator] = {}
    for visit_id, status, content_hash, headers, time_stamp in main_rows:
        accumulator = main_by_visit.setdefault(visit_id, _MainFrameAccumulator())
        accumulator.observe(status=status, content_hash=content_hash, headers=headers, time_stamp=time_stamp)

    requests_by_visit: dict[int, list[str]] = {}
    for visit_id, url in request_rows:
        requests_by_visit.setdefault(visit_id, []).append(url)

    evidence: dict[int, VisitEvidence] = {}
    for visit_id, accumulator in main_by_visit.items():
        urls = requests_by_visit.get(visit_id, [])
        hosts = [_host(url) for url in urls]
        page_host = _host(page_urls.get(visit_id, ""))
        first_party = sum(1 for host in hosts if host and _same_site(host, page_host))
        evidence[visit_id] = VisitEvidence(
            visit_id=visit_id,
            statuses=tuple(sorted(accumulator.statuses)),
            content_hash=accumulator.latest_content_hash,
            response_header_blobs=tuple(sorted(accumulator.header_blobs)),
            request_hosts=frozenset(h for h in hosts if h),
            total_request_count=len(urls),
            first_party_request_count=first_party,
        )
    return evidence


def classify_visit_outcome(
    evidence: VisitEvidence, html_text: str | None
) -> tuple[BlockOutcome, InterstitialMatch | None, str]:
    """
    Classify one visit from its sqlite evidence and, when available, its HTML.

    The three routes are tried in this fixed, mutually exclusive order, and
    exactly one of the four reason-code shapes below is ever produced:

    1. Any main-document status >= 400 -> blocked, reason `status_<code>`
       (the highest such status, if more than one was observed).
    2. A WAF/CDN header or vendor-host signature -> blocked, reason
       `signature_<vendor>`. Checked before the body because it is
       available even without a content store.
    3. A title/body interstitial signature over the retained HTML ->
       blocked, reason `signature_<vendor>`.
    4. Neither matched: `not_assessable` when the HTML needed for route 3
       was unavailable (no content store, no stored hash, or the hash
       missing from the store), else `served`.

    Args:
        evidence: The visit's sqlite-derived evidence.
        html_text: The decoded main-document HTML, or None when not retrievable.
    Returns:
        outcome: The visit's overall verdict.
        signature: The matched signature, from whichever route fired, or None.
        reason_code: One of the four mutually exclusive shapes above.
    """
    blocking_statuses = [s for s in evidence.statuses if s >= BLOCKED_STATUS_THRESHOLD]
    if blocking_statuses:
        return BlockOutcome.BLOCKED_STATUS, None, f"status_{max(blocking_statuses)}"

    network_signature = evidence.header_or_host_signature
    if network_signature is not None:
        return BlockOutcome.BLOCKED_SIGNATURE, network_signature, f"signature_{network_signature.vendor}"

    if html_text is not None:
        body_signature = classify_interstitial(html_text)
        if body_signature is not None:
            return BlockOutcome.BLOCKED_SIGNATURE, body_signature, f"signature_{body_signature.vendor}"
        return BlockOutcome.SERVED, None, "served"

    return BlockOutcome.SERVED, None, "not_assessable"


@dataclass(frozen=True)
class VisitVerdict:
    """One visit's block verdict and the evidence behind it."""

    visit_id: int
    site_id: str
    condition: str
    replicate: int
    statuses: tuple[int, ...]
    signature: InterstitialMatch | None
    signature_assessable: bool
    reason_code: str
    outcome: BlockOutcome
    total_request_count: int = 0
    first_party_request_count: int = 0

    @classmethod
    def build(
        cls,
        *,
        site_id: str,
        condition: str,
        replicate: int,
        evidence: VisitEvidence,
        html_text: str | None,
    ) -> Self:
        """
        Build a visit verdict by classifying the given evidence and HTML.

        Args:
            site_id: The site identity, from the sidecar visit manifest.
            condition: The consent-arm condition name.
            replicate: The replicate number within (site_id, condition).
            evidence: The visit's sqlite-derived evidence.
            html_text: The decoded main-document HTML, or None when not retrievable.
        Returns:
            verdict: The built visit verdict.
        """
        outcome, signature, reason_code = classify_visit_outcome(evidence, html_text)
        return cls(
            visit_id=evidence.visit_id,
            site_id=site_id,
            condition=condition,
            replicate=replicate,
            statuses=evidence.statuses,
            signature=signature,
            signature_assessable=html_text is not None,
            reason_code=reason_code,
            outcome=outcome,
            total_request_count=evidence.total_request_count,
            first_party_request_count=evidence.first_party_request_count,
        )

    def to_json(self) -> dict[str, object]:
        """Project the verdict to the dict written to the block-verdicts.jsonl sidecar."""
        return {
            "visit_id": self.visit_id,
            "site_id": self.site_id,
            "condition": self.condition,
            "replicate": self.replicate,
            "statuses": list(self.statuses),
            "signature_vendor": self.signature.vendor if self.signature is not None else None,
            "signature_evidence": self.signature.evidence if self.signature is not None else None,
            "signature_assessable": self.signature_assessable,
            "reason_code": self.reason_code,
            "outcome": self.outcome.value,
            "total_request_count": self.total_request_count,
            "first_party_request_count": self.first_party_request_count,
        }

    @classmethod
    def from_json(cls, row: dict[str, object]) -> Self:
        """Rebuild a visit verdict from one decoded block-verdicts.jsonl row."""
        signature = None
        if row.get("signature_vendor") is not None:
            signature = InterstitialMatch(vendor=row["signature_vendor"], evidence=row["signature_evidence"])
        return cls(
            visit_id=row["visit_id"],
            site_id=row["site_id"],
            condition=row["condition"],
            replicate=row["replicate"],
            statuses=tuple(row["statuses"]),
            signature=signature,
            signature_assessable=row["signature_assessable"],
            reason_code=row["reason_code"],
            outcome=BlockOutcome(row["outcome"]),
            total_request_count=row.get("total_request_count", 0),
            first_party_request_count=row.get("first_party_request_count", 0),
        )


def group_by_site(verdicts: Iterable[VisitVerdict]) -> dict[str, list[VisitVerdict]]:
    """Group visit verdicts by site_id, preserving encounter order within each group."""
    grouped: dict[str, list[VisitVerdict]] = {}
    for verdict in verdicts:
        grouped.setdefault(verdict.site_id, []).append(verdict)
    return grouped


@dataclass(frozen=True)
class SiteExclusion:
    """One site's usability verdict across every arm it attempted."""

    site_id: str
    usable: bool
    reason: str | None


def site_usability(verdicts: Sequence[VisitVerdict]) -> SiteExclusion:
    """
    Decide one site's usability from the verdicts of every arm it attempted.

    A site is usable only if every attempted visit was served. Dropping
    only the blocked arm and keeping the rest is wrong: it would leave a
    comparison between a real arm and a missing one, and the site's
    contribution to the arm contrast is unusable either way. This holds
    unchanged whether one condition is active or three -- nothing here
    counts or names arms, so the rule needs no change when the remaining
    consent-interaction conditions come online. `not_assessable` visits
    count as served: the status and network-level routes found no block,
    and the absence of a content store is not evidence of one.

    "Every attempted arm" means every arm present in `verdicts`, not every
    arm the study design calls for: run against a cell mid-sweep, before
    every condition has executed for a site, this reports that site usable
    on the arms attempted so far, with nothing here flagging that the set
    is incomplete. A caller reading `exclusion-report.json` from an
    in-progress crawl should treat it as provisional.

    Args:
        verdicts: Every visit verdict belonging to one site.
    Returns:
        exclusion: The site's usability verdict. `reason` is None when usable.
    Raises:
        ValueError: When verdicts is empty -- there is no "every attempted
            arm" to decide over. Callers group verdicts by site_id first
            (see `group_by_site`), so an empty group never reaches this call.
    """
    if not verdicts:
        raise ValueError("site_usability requires at least one visit verdict")
    site_id = verdicts[0].site_id
    blocked = [v for v in verdicts if v.outcome != BlockOutcome.SERVED]
    if not blocked:
        return SiteExclusion(site_id=site_id, usable=True, reason=None)
    detail = ", ".join(f"{v.condition}#{v.replicate}={v.reason_code}" for v in blocked)
    return SiteExclusion(site_id=site_id, usable=False, reason=f"blocked arm(s): {detail}")


@dataclass(frozen=True)
class ExclusionReport:
    """Aggregate per-arm and per-site reason-code counts, and the excluded site list."""

    per_arm_counts: dict[str, dict[str, int]] = field(default_factory=dict)
    per_site_counts: dict[str, dict[str, int]] = field(default_factory=dict)
    excluded_sites: tuple[SiteExclusion, ...] = ()

    def to_json(self) -> dict[str, object]:
        """Project the report to the dict written to the exclusion-report.json sidecar."""
        return {
            "per_arm": self.per_arm_counts,
            "per_site": self.per_site_counts,
            "excluded_sites": [
                {"site_id": exclusion.site_id, "reason": exclusion.reason}
                for exclusion in self.excluded_sites
            ],
        }


def _count_reason_codes(verdicts: Iterable[VisitVerdict]) -> dict[str, int]:
    """Count verdicts by their mutually exclusive reason_code."""
    counts: dict[str, int] = {}
    for verdict in verdicts:
        counts[verdict.reason_code] = counts.get(verdict.reason_code, 0) + 1
    return counts


def build_exclusion_report(verdicts: Sequence[VisitVerdict]) -> ExclusionReport:
    """
    Build the per-arm, per-site, and excluded-site summary of one cell's block pass.

    Counts break down by `reason_code` rather than the three-way `outcome`:
    the reason code keeps non-2xx statuses, signature vendors, and
    not-assessable visits separate, which is what a hand-labelling pass
    needs to check the classifier's own precision and recall against.

    Args:
        verdicts: Every visit verdict in the cell.
    Returns:
        report: The aggregate exclusion report.
    """
    per_arm: dict[str, list[VisitVerdict]] = {}
    for verdict in verdicts:
        per_arm.setdefault(verdict.condition, []).append(verdict)
    per_arm_counts = {condition: _count_reason_codes(group) for condition, group in sorted(per_arm.items())}

    by_site = group_by_site(verdicts)
    per_site_counts = {site_id: _count_reason_codes(group) for site_id, group in sorted(by_site.items())}
    exclusions = sorted(
        (site_usability(group) for group in by_site.values()), key=lambda exclusion: exclusion.site_id
    )
    excluded = tuple(exclusion for exclusion in exclusions if not exclusion.usable)
    return ExclusionReport(
        per_arm_counts=per_arm_counts, per_site_counts=per_site_counts, excluded_sites=excluded
    )
