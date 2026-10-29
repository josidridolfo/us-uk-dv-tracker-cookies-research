"""
EasyPrivacy tracker classifier.

Evaluates a pinned EasyPrivacy snapshot with the adblock-rust engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from adblock import Engine, FilterSet

REQUEST_TYPE_BY_RESOURCE: dict[str, str] = {
    "script": "script",
    "image": "image",
    "stylesheet": "stylesheet",
    "font": "font",
    "media": "media",
    "ping": "ping",
    "xhr": "xmlhttprequest",
    "fetch": "xmlhttprequest",
    "other": "other",
}


@dataclass(frozen=True)
class EasyPrivacyMatcher:
    """EasyPrivacy tracker classifier over a pinned snapshot."""

    snapshot_path: Path
    rule_count: int
    engine: Engine = field(compare=False, repr=False)

    @classmethod
    def from_snapshot(cls, path: Path) -> EasyPrivacyMatcher:
        """
        Builds a matcher from an EasyPrivacy snapshot.

        Args:
            path: Path to the EasyPrivacy text snapshot.
        Returns:
            matcher: The matcher backed by the snapshot's network rules.
        """
        if not path.is_file():
            raise FileNotFoundError(f"EasyPrivacy snapshot not found at {path}")
        # drop blank lines and ``!`` comments
        rules = [
            line
            for raw in path.read_text(encoding="utf-8").splitlines()
            if (line := raw.strip()) and not line.startswith("!")
        ]
        filter_set = FilterSet()
        filter_set.add_filters(rules)
        return cls(snapshot_path=path, rule_count=len(rules), engine=Engine(filter_set))

    @staticmethod
    def request_type_for_resource(resource_type: str, *, is_third_party: bool) -> str:
        """
        Maps a recorded resource type to the adblock-rust request_type string.

        Args:
            resource_type: The recorded resource-type name.
            is_third_party: Whether the request is third-party to the page origin.
        Returns:
            request_type: The adblock-rust request_type; framed documents map to
                ``sub_frame`` and top-level documents to ``document``.
        """
        if resource_type == "document":
            return "sub_frame" if is_third_party else "document"
        return REQUEST_TYPE_BY_RESOURCE.get(resource_type, "other")

    def is_tracker(
        self, url: str, resource_type: str, is_third_party: bool, source_domain: str
    ) -> bool:
        """
        Whether a request matches an EasyPrivacy block rule, honouring exceptions.

        The engine derives party from ``source_url``; ``is_third_party`` only
        selects the document vs framed request type.

        Args:
            url: The full request URL.
            resource_type: The recorded resource-type name.
            is_third_party: Whether the request is third-party to the page origin.
            source_domain: The registered domain of the visited page.
        Returns:
            tracker: True when a block rule matches and no ``@@`` exception overrides it.
        """
        request_type = self.request_type_for_resource(
            resource_type, is_third_party=is_third_party
        )
        result = self.engine.check_network_urls(
            url=url,
            source_url=f"https://{source_domain}/",
            request_type=request_type,
        )
        return result.matched
