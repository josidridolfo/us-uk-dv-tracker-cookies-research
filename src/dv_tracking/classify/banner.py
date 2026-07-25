"""
Multi-source cookie-banner detection.

Four signals are combined per site; the finding records which fired and the
highest-ranked one (primary_method) for cross-source auditing:

1. tcf_api -- IAB TCF runtime probe. Strongest signal.
2. consentomatic -- DOM match against pinned Consent-O-Matic selectors. Names the CMP.
3. hostname -- a known CMP-vendor host tell. Coarser: proves the asset loaded.
4. heuristic -- z-index + cookie-keyword DOM scan. False-positive prone; ranked last.
"""

from __future__ import annotations

from dataclasses import dataclass

from dv_tracking.crawl.models import BannerSignals, TCFProbeResult

# Vendor name -> host suffixes. Names align with Consent-O-Matic CMP keys so
# hostname and consentomatic hits collapse into one vendor entry.
CMP_VENDOR_HOSTS: dict[str, tuple[str, ...]] = {
    "OneTrust": ("onetrust.com", "cookielaw.org", "otcdn.com"),
    "Cookiebot": ("cookiebot.com", "cookiebot.eu"),
    "Sourcepoint": ("sourcepoint.com", "sp-prod.net"),
    "Quantcast Choice": ("quantcast.com", "quantcast.mgr.consensu.org"),
    "TrustArc": ("trustarc.com", "truste.com"),
    "Usercentrics": ("usercentrics.eu", "usercentrics.com"),
    "Didomi": ("didomi.io", "privacy-center.org"),
    "Iubenda": ("iubenda.com",),
    "Tealium": ("tealiumiq.com", "tiqcdn.com"),
    "Termly": ("termly.io",),
    "Cookie Information": ("cookieinformation.com",),
    "Consent Manager": ("consentmanager.net",),
    "Sharethis": ("sharethis.com",),
    "UniConsent": ("uniconsent.com",),
    "IAB TCF runtime": ("consensu.org",),
}

# Consent-O-Matic rule key -> canonical vendor name in CMP_VENDOR_HOSTS.
CONSENTOMATIC_TO_VENDOR: dict[str, str] = {
    "onetrust": "OneTrust",
    "onetrust-stackoverflow": "OneTrust",
    "optanon": "OneTrust",
    "optanon-alternative": "OneTrust",
    "cookiebot": "Cookiebot",
    "sourcepoint": "Sourcepoint",
    "sourcepointframe": "Sourcepoint",
    "sourcepointpopup": "Sourcepoint",
    "quantcast": "Quantcast Choice",
    "quantcast2": "Quantcast Choice",
    "trustarcbar": "TrustArc",
    "trustarcframe": "TrustArc",
    "didomi.io": "Didomi",
    "tealium.com": "Tealium",
    "consentmanager.net": "Consent Manager",
    "cookieinformation": "Cookie Information",
    "sharethis": "Sharethis",
    "uniconsent": "UniConsent",
}

# Ranking of signals; lower index = higher confidence.
METHOD_RANK: tuple[str, ...] = ("tcf_api", "consentomatic", "hostname", "heuristic")

# Keyword sets tagging captured banner text as consent / opt-out / mixed / notice.
CONSENT_KEYWORDS: tuple[str, ...] = (
    "accept all", "reject all", "accept cookies", "reject cookies",
    "manage preferences", "cookie settings", "i accept", "i agree",
    "accept", "decline", "agree",
)
OPT_OUT_KEYWORDS: tuple[str, ...] = (
    "do not sell", "do not sell or share", "your privacy choices",
    "california privacy", "us privacy", "opt out of sale",
    "opt-out", "opt out", "limit the use", "privacy rights",
)


@dataclass(frozen=True)
class BannerFinding:
    """Banner-detection outcome for one site visit."""

    banner_present: bool
    detection_methods: tuple[str, ...]
    primary_method: str | None
    cmp_vendors: tuple[str, ...]
    cmp_vendor: str | None
    hostname_match: str | None
    tcf_present: bool
    consentomatic_matched: bool
    heuristic_matched: bool
    usp_api_present: bool
    gpp_api_present: bool
    banner_type: str | None
    close_button_present: bool
    quick_exit_present: bool


class BannerClassifier:
    """Combines the four banner signals into a BannerFinding, owning the CMP knowledge."""

    @staticmethod
    def classify_banner_type(text: str | None) -> str | None:
        """
        Tags captured banner text as consent, opt_out, mixed, or notice.

        Args:
            text: The captured banner evidence text, or None.
        Returns:
            banner_type: The tag, or None when no text was captured.
        """
        if not text:
            return None
        lowered = text.lower()
        has_consent = any(keyword in lowered for keyword in CONSENT_KEYWORDS)
        has_opt_out = any(keyword in lowered for keyword in OPT_OUT_KEYWORDS)
        if has_consent and has_opt_out:
            return "mixed"
        if has_consent:
            return "consent"
        if has_opt_out:
            return "opt_out"
        return "notice"

    @staticmethod
    def canonical_vendor(consentomatic_name: str) -> str:
        """
        Maps a Consent-O-Matic rule key to a canonical CMP vendor label.

        Args:
            consentomatic_name: The Consent-O-Matic rule key.
        Returns:
            vendor: The canonical vendor label, or the key itself when unmapped.
        """
        return CONSENTOMATIC_TO_VENDOR.get(consentomatic_name.lower(), consentomatic_name)

    @staticmethod
    def hostname_signal(hosts: list[str]) -> tuple[str | None, str | None]:
        """
        Finds the first CMP-vendor host tell among a set of hosts.

        Args:
            hosts: The request hosts to scan.
        Returns:
            vendor_and_host: The (vendor, matched host) pair, or (None, None).
        """
        if not hosts:
            return None, None
        lowered = [host.lower() for host in hosts]
        for vendor, suffixes in CMP_VENDOR_HOSTS.items():
            for host in lowered:
                for suffix in suffixes:
                    if host == suffix or host.endswith("." + suffix):
                        return vendor, host
        return None, None

    @staticmethod
    def pick_primary_method(methods: list[str]) -> str | None:
        """
        Picks the highest-confidence method that fired.

        Args:
            methods: The methods that fired.
        Returns:
            primary: The highest-ranked method, or None when none fired.
        """
        for candidate in METHOD_RANK:
            if candidate in methods:
                return candidate
        return None

    @staticmethod
    def dedupe_preserve_order(values: list[str]) -> list[str]:
        """
        Drops duplicate values, keeping first-seen order.

        Args:
            values: The values to deduplicate.
        Returns:
            deduped: The values with later duplicates removed.
        """
        seen: set[str] = set()
        deduped: list[str] = []
        for value in values:
            if value not in seen:
                seen.add(value)
                deduped.append(value)
        return deduped

    def detect(
        self,
        banner_signals: BannerSignals | None,
        consentomatic_bundle: object | None,
        tcf_result: TCFProbeResult | None,
        hostname: list[str],
    ) -> BannerFinding:
        """
        Combines the four signals into a per-site banner finding.

        Args:
            banner_signals: The raw DOM/JS probe outputs, or None.
            consentomatic_bundle: A bundle carrying .rules restricting recognised CMP
                tells, or None to accept any consentomatic match.
            tcf_result: The TCF probe outcome, or None.
            hostname: The request hosts scanned for a CMP-vendor tell.
        Returns:
            finding: The combined banner-detection finding.
        """
        methods: list[str] = []
        vendors: list[str] = []
        hostname_match: str | None = None
        consentomatic_evidence: str | None = None
        heuristic_evidence: str | None = None

        tcf_present = tcf_result is not None and tcf_result.api_present
        if tcf_present:
            methods.append("tcf_api")
            vendors.append("IAB TCF runtime")

        consentomatic_matches = self.recognised_matches(banner_signals, consentomatic_bundle)
        consentomatic_matched = bool(consentomatic_matches)
        if consentomatic_matched:
            methods.append("consentomatic")
            consentomatic_evidence = "matched " + ", ".join(consentomatic_matches)
            for match in consentomatic_matches:
                vendors.append(self.canonical_vendor(match))

        host_vendor, host_hit = self.hostname_signal(hostname)
        if host_vendor is not None and host_hit is not None:
            methods.append("hostname")
            hostname_match = host_hit
            vendors.append(host_vendor)

        heuristic_matched = banner_signals is not None and banner_signals.dom_heuristic_matched
        if heuristic_matched:
            methods.append("heuristic")
            heuristic_evidence = banner_signals.dom_heuristic_evidence

        deduped_vendors = tuple(self.dedupe_preserve_order(vendors))
        banner_type = self.classify_banner_type(consentomatic_evidence or heuristic_evidence)

        return BannerFinding(
            # Union of all signals (incl. the p=0.82 DOM heuristic and the asset-loaded hostname
            # tell); NOT a verified-CMP rate. The API-verified vs heuristic split is carried on
            # SitePrivacyFindings.
            banner_present=bool(methods),
            detection_methods=tuple(methods),
            primary_method=self.pick_primary_method(methods),
            cmp_vendors=deduped_vendors,
            cmp_vendor=deduped_vendors[0] if deduped_vendors else None,
            hostname_match=hostname_match,
            tcf_present=tcf_present,
            consentomatic_matched=consentomatic_matched,
            heuristic_matched=heuristic_matched,
            # usp/gpp presence is recorded but not added to methods; SitePrivacyFindings.
            # banner_api_verified folds them into the API-verified signal.
            usp_api_present=banner_signals.usp_api_present if banner_signals else False,
            gpp_api_present=banner_signals.gpp_api_present if banner_signals else False,
            banner_type=banner_type,
            close_button_present=banner_signals.close_button_present if banner_signals else False,
            quick_exit_present=banner_signals.quick_exit_present if banner_signals else False,
        )

    @staticmethod
    def recognised_matches(
        banner_signals: BannerSignals | None, consentomatic_bundle: object | None
    ) -> tuple[str, ...]:
        """
        The consentomatic matches recognised for this crawl.

        Args:
            banner_signals: The raw probe outputs carrying the matches, or None.
            consentomatic_bundle: A bundle whose .rules restrict recognised matches,
                or None to accept every match.
        Returns:
            matches: The recognised consentomatic rule keys, in probe order.
        """
        if banner_signals is None:
            return ()
        matches = banner_signals.consentomatic_matches
        rules = getattr(consentomatic_bundle, "rules", None)
        if rules is None:
            return matches
        allowed = set(rules)
        return tuple(match for match in matches if match in allowed)
