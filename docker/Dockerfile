# syntax=docker/dockerfile:1

# Crawl image: the pinned OpenWPM base plus this project's source.
#
# The base is built by scripts/fetch-openwpm.sh from a commit-verified upstream
# clone. Source is placed on PYTHONPATH rather than pip-installed, which keeps
# the project out of OpenWPM's conda environment and avoids resolving this
# project's analysis dependencies inside the crawl image. The analysis stage
# runs outside the container and owns those.
#
# /opt/OpenWPM is named on PYTHONPATH explicitly. Upstream ships no [project]
# or [build-system] table, so `openwpm` is never pip-installed and resolves
# only as a directory on the path; upstream's image gets that for free from its
# own working directory, which this image overrides.

ARG OPENWPM_BASE=dv-openwpm-base:v0.38.0
FROM ${OPENWPM_BASE}

SHELL ["/bin/bash", "-c"]

WORKDIR /opt/dv-tracking
COPY src ./src
ENV PYTHONPATH=/opt/OpenWPM:/opt/dv-tracking/src
ENV PYTHONUNBUFFERED=1

# Crawl output lands on a mounted volume so captures outlive the container.
ENV DV_DATA_ROOT=/data
VOLUME ["/data"]

# Until the OpenWPM entrypoint lands, the default command only proves the
# environment imports. The fuller browser and egress checks are documented in
# the README as explicit `docker compose run` invocations.
CMD ["python", "-c", "import openwpm, sys; print('openwpm import ok on', sys.version.split()[0])"]
