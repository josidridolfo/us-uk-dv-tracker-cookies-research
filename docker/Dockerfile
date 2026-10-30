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

# Baked in rather than read with -e, so the manifest's repo_commit reflects
# the exact tree the image was built from. scripts/build-crawl-image.sh is
# the only sanctioned way to set this: it refuses on a dirty working tree.
ARG DV_REPO_COMMIT
ENV DV_REPO_COMMIT=${DV_REPO_COMMIT}

# Crawl output lands on a mounted volume so captures outlive the container.
ENV DV_DATA_ROOT=/data
VOLUME ["/data"]

# The real entrypoint is `python -m dv_tracking.crawl.openwpm_run <geo> ...`
# (see README); it is not a console script because this image never pip-
# installs the project. The default command is an import smoke test against
# the actual entrypoint module, not bare `import openwpm`: a bare import
# succeeds even when every submodule's own dependencies (selenium, the
# storage providers, utilities) are missing, since `openwpm/__init__.py`
# does not import them. Importing the entrypoint module pulls in the full
# chain -- task_manager, command_sequence, commands.*, config, errors,
# storage.sql_provider, utilities.platform_utils, selenium, and the
# dv_tracking side -- so a missing dependency fails at `docker run`, not mid-crawl.
CMD ["python", "-c", "import dv_tracking.crawl.openwpm_run, sys; print('crawl entrypoint import ok on', sys.version.split()[0])"]
