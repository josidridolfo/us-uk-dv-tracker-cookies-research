-- One row per outbound request captured in the pre-consent window.
-- Not classified yet -- EasyPrivacy / DDG matching happens in int/.
CREATE OR REPLACE TABLE raw.requests AS
SELECT *
FROM read_parquet(
    '*/requests.parquet',
    filename = true,
    union_by_name = true
);
