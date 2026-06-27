-- One row per cookie observation. Values are captured verbatim; the
-- fresh researcher-controlled context per trial is what makes that safe.
CREATE OR REPLACE TABLE raw.cookies AS
SELECT *
FROM read_parquet(
    '*/cookies.parquet',
    filename = true,
    union_by_name = true
);
