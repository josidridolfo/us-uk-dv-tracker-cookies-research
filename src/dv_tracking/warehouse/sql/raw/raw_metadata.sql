-- One row per (crawl_date, geolocation, site_id, trial). Fields come
-- straight from metadata.json, consolidated into per-cell parquet first.
CREATE OR REPLACE TABLE raw.metadata AS
SELECT *
FROM read_parquet(
    '*/metadata.parquet',
    filename = true,
    union_by_name = true
);
