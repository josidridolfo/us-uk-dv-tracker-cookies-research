"""
The DV support site corpus.

A DVSupportSite is one row of the corpus CSV at
data/corpus/dv_support_sites.csv. The list is loaded through the record's own
alternative constructor.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from dv_tracking import PROJECT_ROOT

REQUIRED_COLUMNS: tuple[str, ...] = (
    "site_id",
    "org_name",
    "country",
    "category",
    "url",
    "notes",
)

DEFAULT_SITES_PATH: Path = PROJECT_ROOT / "data" / "corpus" / "dv_support_sites.csv"


@dataclass(frozen=True)
class DVSupportSite:
    """One DV support site: a single row of the corpus CSV."""

    site_id: str
    org_name: str
    country: str
    category: str
    url: str
    notes: str

    @staticmethod
    def significant_rows(rows: list[list[str]]) -> list[list[str]]:
        """
        Drop comment and blank rows from parsed CSV rows.

        Args:
            rows: The raw CSV rows as parsed by csv.reader.
        Returns:
            significant: Rows that are non-empty and do not start with a comment marker.
        """
        return [row for row in rows if row and not row[0].lstrip().startswith("#")]

    @staticmethod
    def row_to_site(header: list[str], fields: list[str], data_row_number: int) -> DVSupportSite:
        """
        Build one DVSupportSite from a header and a data row.

        Args:
            header: The stripped column names, in file order.
            fields: The raw cell values for this data row.
            data_row_number: The 1-based data-row index, used in error messages.
        Returns:
            site: The DV support site for this row.
        """
        record = {column: value.strip() for column, value in zip(header, fields, strict=False)}
        # notes may be blank; every other required column must carry a value.
        for column in REQUIRED_COLUMNS[:-1]:
            if not record.get(column):
                raise ValueError(
                    f"data row {data_row_number}: empty value for required field {column!r}"
                )
        return DVSupportSite(
            site_id=record["site_id"],
            org_name=record["org_name"],
            country=record["country"],
            category=record["category"],
            url=record["url"],
            notes=record.get("notes", ""),
        )

    @classmethod
    def load_all(cls, path: Path | None = None) -> list[DVSupportSite]:
        """
        Load every DV support site from the six-column corpus CSV.

        Args:
            path: Corpus CSV path; defaults to DEFAULT_SITES_PATH when None.
        Returns:
            sites: One DVSupportSite per data row, comment and blank rows skipped.
        """
        csv_path = DEFAULT_SITES_PATH if path is None else path
        if not csv_path.is_file():
            raise FileNotFoundError(f"site list CSV not found at {csv_path}")

        with csv_path.open("r", encoding="utf-8", newline="") as handle:
            rows = cls.significant_rows(list(csv.reader(handle)))

        if not rows:
            raise ValueError(f"site list CSV at {csv_path} is empty")

        header = [column.strip() for column in rows[0]]
        missing = [column for column in REQUIRED_COLUMNS if column not in header]
        if missing:
            raise ValueError(f"site list CSV at {csv_path} missing column(s): {', '.join(missing)}")

        return [
            cls.row_to_site(header, fields, data_row_number)
            for data_row_number, fields in enumerate(rows[1:], start=1)
        ]
