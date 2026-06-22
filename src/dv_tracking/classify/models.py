"""
Analyzed records: the RAW -> DERIVED boundary for the crawl trace.

An AnalyzedRequest carries the raw request fields and the registered domain and
third-party flag captured with the request during the crawl.
"""

from __future__ import annotations

from dataclasses import dataclass

from dv_tracking.crawl.models import RawRequest


@dataclass(frozen=True)
class AnalyzedRequest:
    """A captured request with its registered domain and third-party flag."""

    url: str
    host: str
    method: str
    resource_type: str
    registered_domain: str
    is_third_party: bool

    @classmethod
    def from_raw(cls, raw: RawRequest) -> AnalyzedRequest:
        """
        Carry the request's registered domain and party through from the raw record.

        Args:
            raw: The captured raw request.
        Returns:
            analyzed: The request carrying its registered domain and party.
        """
        return cls(
            url=raw.url,
            host=raw.host,
            method=raw.method,
            resource_type=raw.resource_type,
            registered_domain=raw.registered_domain,
            is_third_party=raw.is_third_party,
        )
