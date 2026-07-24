"""
DuckDB warehouse for crawl outputs.

Builds layered views raw -> stg -> int -> marts. consolidate_raw folds the
per-trial JSONL under a RawStore into per-cell parquet; build runs each layer's
SQL as CREATE OR REPLACE, so re-running is idempotent.
"""

from __future__ import annotations

import os
from pathlib import Path

import duckdb

from dv_tracking.persistence import RawStore

LAYERS: tuple[str, ...] = ("raw", "stg", "int", "marts")
DEFAULT_SQL_DIR: Path = Path(__file__).parent / "warehouse" / "sql"


class Warehouse:
    """Builds a layered DuckDB warehouse from consolidated crawl parquet."""

    def __init__(self, db_path: Path, sql_dir: Path) -> None:
        """
        Binds the warehouse to a DuckDB file and a SQL model directory.

        Args:
            db_path: Path to the DuckDB file, created if missing.
            sql_dir: Directory holding sql/<layer>/*.sql model files.
        Returns:
            None
        """
        self.db_path = db_path
        self.sql_dir = Path(sql_dir).resolve()
        self.parquet_root = db_path.parent.resolve()

    @staticmethod
    def jsonl_to_parquet_sql(glob: str, out_path: Path, crawl_date: str, geolocation: str) -> str:
        """
        Builds the COPY statement that augments JSONL rows with identity keys and writes parquet.

        Args:
            glob: Absolute glob matching the per-trial JSONL files for one cell.
            out_path: The parquet file to write.
            crawl_date: The crawl date attached to every row.
            geolocation: The exit geolocation attached to every row.
        Returns:
            statement: A DuckDB COPY statement.
        """
        return (
            "COPY (SELECT * EXCLUDE (filename), "
            "regexp_extract(filename, '([^/]+)/trial_[0-9]+/', 1) AS site_id, "
            "CAST(regexp_extract(filename, 'trial_([0-9]+)/', 1) AS INTEGER) AS trial, "
            f"'{crawl_date}' AS crawl_date, '{geolocation}' AS geolocation "
            f"FROM read_json_auto('{glob}', filename = true, union_by_name = true, "
            "format = 'newline_delimited')) "
            f"TO '{out_path}' (FORMAT parquet)"
        )

    def consolidate_raw(self, raw_store: RawStore, out_dir: Path) -> Path:
        """
        Folds the per-trial JSONL under a RawStore into per-cell parquet.

        Args:
            raw_store: The store whose raw_root holds the crawl-cell directories.
            out_dir: The directory the per-cell parquet is written under.
        Returns:
            parquet_root: The directory the parquet was written to (also stored for build).
        """
        out_dir = out_dir.resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        connection = duckdb.connect()
        try:
            for cell_dir in sorted(p for p in raw_store.raw_root.iterdir() if p.is_dir()):
                crawl_date, geolocation = cell_dir.name.split("_", 1)
                cell_out = out_dir / cell_dir.name
                cell_out.mkdir(parents=True, exist_ok=True)
                metadata_glob = str(cell_dir / "*" / "trial_*" / "metadata.json")
                requests_glob = str(cell_dir / "*" / "trial_*" / "requests.jsonl")
                cookies_glob = str(cell_dir / "*" / "trial_*" / "cookies.jsonl")
                # metadata.json already carries the identity keys.
                connection.execute(
                    f"COPY (SELECT * FROM read_json_auto('{metadata_glob}', "
                    "union_by_name = true)) "
                    f"TO '{cell_out / 'metadata.parquet'}' (FORMAT parquet)"
                )
                connection.execute(
                    self.jsonl_to_parquet_sql(
                        requests_glob, cell_out / "requests.parquet", crawl_date, geolocation
                    )
                )
                connection.execute(
                    self.jsonl_to_parquet_sql(
                        cookies_glob, cell_out / "cookies.parquet", crawl_date, geolocation
                    )
                )
        finally:
            connection.close()
        self.parquet_root = out_dir
        return out_dir

    def build(self) -> None:
        """
        Runs the layer SQL against the DuckDB file in layer order.

        Args:
            None
        Returns:
            None
        """
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        prior_cwd = Path.cwd()
        os.chdir(self.parquet_root)
        try:
            connection = duckdb.connect(str(self.db_path))
            try:
                for layer in LAYERS:
                    layer_dir = self.sql_dir / layer
                    if not layer_dir.is_dir():
                        continue
                    connection.execute(f"CREATE SCHEMA IF NOT EXISTS {layer};")
                    for sql_file in sorted(layer_dir.glob("*.sql")):
                        connection.execute(sql_file.read_text(encoding="utf-8"))
            finally:
                connection.close()
        finally:
            os.chdir(prior_cwd)
