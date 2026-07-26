"""
Canonical owner map bridging DDG Tracker Radar owners and Open-Cookie-DB vendors.

The two lists name the same corporate owner differently: Radar labels a request
host's `tracker_owner` (e.g. "Google", "Wix.com"), while the cookie database labels
a cookie's `cookie_vendor` (e.g. "Google Analytics", "Wix.com"). Collapsing both to a
common canonical owner lets a site's third-party trackers be joined against its
first-party tracking cookies by owner -- the basis for the pure-3p vs first-party-context
split. The alias groups are built from the owners/vendors actually observed in the run.
"""

from __future__ import annotations

# Each canonical owner maps to every Radar-owner and cookie-vendor spelling seen for it.
# Members are matched case-insensitively; an owner not listed here is its own canonical.
CANONICAL_OWNER_ALIASES: dict[str, frozenset[str]] = {
    "Google": frozenset({"Google", "Google Analytics", "Google Tag Manager", "DoubleClick"}),
    "Meta": frozenset({"Facebook", "Meta"}),
    "Wix": frozenset({"Wix.com", "Wix"}),
    "Microsoft": frozenset({"Microsoft", "Bing", "Clarity", "Bing / Microsoft"}),
    "Automattic": frozenset({"Automattic", "WooCommerce", "WordPress"}),
    "HubSpot": frozenset({"HubSpot", "Hubspot"}),
    "ByteDance": frozenset({"ByteDance", "TikTok"}),
}

# Reverse index: a lowercased alias -> its canonical owner.
ALIAS_TO_CANONICAL_OWNER: dict[str, str] = {
    alias.lower(): canonical
    for canonical, aliases in CANONICAL_OWNER_ALIASES.items()
    for alias in aliases
}

# Cookie categories that mark a first-party cookie as a tracking (analytics/marketing) cookie.
FIRST_PARTY_TRACKING_COOKIE_CATEGORIES: frozenset[str] = frozenset({"analytics", "marketing"})


def canonical_owner(name: str | None) -> str | None:
    """
    Maps a Radar owner or cookie vendor to its canonical owner.

    Args:
        name: A DDG `tracker_owner` or Open-Cookie-DB `cookie_vendor`, or None.
    Returns:
        owner: The canonical owner; the trimmed input when unmapped; None when the
            input is None or blank (an unknown owner that cannot be shown first-party).
    """
    if name is None:
        return None
    trimmed = name.strip()
    if not trimmed:
        return None
    return ALIAS_TO_CANONICAL_OWNER.get(trimmed.lower(), trimmed)
