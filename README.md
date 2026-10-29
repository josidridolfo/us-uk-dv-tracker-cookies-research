# us-uk-dv-tracker-cookies-research

Work in progress.

A sector-specific web-measurement study of pre-consent third-party tracking on
United Kingdom and United States domestic violence support websites. The study
visits each site with a fresh browser, records the network requests, cookies,
and storage set before any consent interaction, and compares the two
jurisdictions at an aggregate level.

Each raw trial's `metadata.json` carries an `exit_info` object recording the
pre-crawl gate's observed exit (`ip`, `country`, `org`, `residential`,
`via_proxy`, `checked_at_utc`), constant across every trial in a run, with an
optional supplementary `provider` label.

## Credentials

The IPRoyal residential-exit credentials live in a git-excluded `.env` at the
repo root, holding the keys `IPROYAL_HOST`, `IPROYAL_PORT`, `IPROYAL_USERNAME`,
and `IPROYAL_PASSWORD`. The entry points (`dv-crawl`, `dv-validate-vpn`) load
this file automatically, so no manual `source` step is needed. An already
exported environment variable overrides the file value.

`IPROYAL_COUNTRY` and `IPROYAL_SESSION` are deliberately not kept in that file.
Country follows the jurisdiction under crawl and the session token is generated
per run, so both are supplied at invocation; `IPROYAL_LIFETIME` defaults to
`10m`. The relay refuses to start if any of the four is absent.

## Crawl container

The crawl stage runs in a container; the analysis stage runs on the host. The
browser is the unbranded Firefox that OpenWPM pins, which is published for
`linux/amd64` only, so reported crawls run on an amd64 host. Emulated runs on
another architecture distort the fixed dwell windows and are suitable for
development only.

Two build steps, because upstream's Dockerfile takes its own source tree as the
build context:

```bash
./scripts/fetch-openwpm.sh   # pinned clone -> dv-openwpm-base
docker compose build
```

Both commands run from the repo root. `compose.yaml` lives there rather than
beside the Dockerfile because Compose discovers `.env` in the directory of the
first compose file, and the residential-exit credentials live at the root.

`fetch-openwpm.sh` clones OpenWPM at the pinned tag, refuses to build if the
tag no longer resolves to the pinned commit, rewrites upstream's floating base
image to a pinned digest, and records the result in
`data/openwpm-base-provenance.txt`.

The `relay` service exists because Firefox cannot authenticate to an upstream
proxy without a prompt. It terminates an unauthenticated listener on the
compose network and forwards upstream with the residential-exit credentials
attached, so the browser is configured with `relay:8080` and no credential.

Starting the relay for one jurisdiction:

```bash
IPROYAL_COUNTRY=gb IPROYAL_SESSION=$(openssl rand -hex 8) \
    docker compose up -d relay
```

Verifying the stack:

```bash
docker compose run --rm crawl                    # imports OpenWPM
docker compose run --rm -e HTTPS_PROXY=http://relay:8080 crawl \
    python -c "import urllib.request as r, json; \
print(json.load(r.urlopen('https://ipinfo.io/json')))"   # exit through the relay
```

The explicit proxy variable on the second command matters: without it the
request egresses directly and reports the host's own address rather than the
residential exit. The pre-crawl gate enforces that the exit reads residential
and in-country before any site is visited.

## Cookie essentiality

A cookie is treated as essential when it is both strictly necessary and
short-lived. Strict necessity is taken from the category assigned by the pinned
Open Cookie Database. Short-lived means the cookie is either a session cookie or
has an observed expiry within 30 days of the crawl instant. The observed
lifetime is used -- the `expires_unix` value captured during the crawl, or the
session flag -- not any declared retention period. Everything else is
non-essential: analytics and marketing cookies, cookies whose name is not
matched by the database, and strictly-necessary cookies whose observed lifetime
runs beyond the window. The reference instant is the crawl start recorded per
trial, so the classification is stable across re-analysis.

## Sites & Results
Available upon request to verified researchers. Message me or email me with the subject [UK-US DV Data Request]: josid@ridol.fo
