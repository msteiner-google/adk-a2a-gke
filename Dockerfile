# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# The single image every agent runs. AGENT_NAME picks which one a process
# becomes at startup (app/agent.py), so there is one image, not six.
#
# Two stages. The build stage resolves and installs dependencies; the runtime
# stage receives the finished virtualenv and nothing else. What that leaves
# behind is not incidental:
#
#   - `uv` itself and its wheel cache. `uv sync` hardlinks out of that cache
#     into the venv, so in a single-stage build BOTH copies end up in the layer.
#   - the build inputs (pyproject.toml, uv.lock) and everything the dev group
#     installs.
#
# Together with dropping the [evaluation] extra from the runtime dependency set
# (see the note in pyproject.toml), the installed tree went from 702 MB to
# 430 MB -- measured by running `uv sync --frozen --no-dev` into a scratch
# environment before and after, not estimated.
#
# Prefer Cloud Build over building this on a workstation (`make image`, see
# ./cloudbuild.yaml): the wheels come down, and the layers go up, over Google's
# network instead of yours.

# --- Build stage --------------------------------------------------------------
# Must stay in lockstep with requires-python in pyproject.toml and the
# python-version pins in ruff.toml / ty.toml / pyrightconfig.json.
# tests/unit/test_python_version.py fails if they drift apart.
FROM python:3.14-slim AS build

# The uv binary out of its own image: nothing is pip-installed into the
# environment being built, and the version is pinned like any other dependency.
COPY --from=ghcr.io/astral-sh/uv:0.8.13 /uv /usr/local/bin/uv

# UV_COMPILE_BYTECODE stays OFF: .pyc files would add roughly 15% to the image
# to save a one-off cost on first import, and these are long-lived servers.
# UV_LINK_MODE=copy silences uv's warning about hardlinking across filesystems.
ENV UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /code

COPY ./pyproject.toml ./README.md ./uv.lock ./
COPY ./app ./app

# --no-dev drops pytest and friends. --frozen fails rather than quietly
# re-resolving when uv.lock is behind pyproject.toml, which is what a build
# wants: a lockfile that only holds on a workstation is not a lockfile.
RUN uv sync --frozen --no-dev

# --- Runtime stage ------------------------------------------------------------
FROM python:3.14-slim

# No uv in this stage. The venv's bin directory on PATH is enough to run
# uvicorn, alembic or python directly -- which is why
# infra/kustomize/base/migrate-job.yaml calls them without `uv run`.
ENV PATH="/code/.venv/bin:$PATH" \
    PYTHONPATH=/code \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /code

COPY --from=build /code/.venv /code/.venv
# Only ./app, deliberately. The Alembic migrations live under app/migrations/
# for exactly this reason: the migration Job runs this same image, so anything
# outside app/ is invisible to it.
COPY ./app ./app

ARG AGENT_VERSION=0.0.0
ENV AGENT_VERSION=${AGENT_VERSION}

# Nothing here writes to the image filesystem, so drop root.
RUN useradd --create-home --uid 1001 agent && chown -R agent:agent /code
USER agent

EXPOSE 8080

# The port is hardcoded rather than read from $PORT. Four places must agree if
# you change it: this line, EXPOSE above, each Deployment's containerPort +
# Service targetPort, and PORT in the ConfigMap.
CMD ["uvicorn", "app.fast_api_app:app", "--host", "0.0.0.0", "--port", "8080"]
