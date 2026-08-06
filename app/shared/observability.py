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

"""One-call observability bootstrap + DI wiring for logging and tracing.

This ties `shared.logging` and `shared.telemetry` together so ``app/agent.py``
can enable structured, trace-correlated logging and distributed
tracing with a single import at startup:

    from .shared.observability import configure_observability

    configure_observability()          # idempotent: logging + tracing + A2A
    models = Injector([ModelModule()]).get(Models)

`configure_observability` is imperative (it sets global logging + OTel state) and
idempotent, mirroring how ``app/agent.py`` already resolves DI at import time.
For code that prefers *injecting* a tracer, `ObservabilityModule` provides an OTel
`Tracer`; install it alongside `ModelModule` (whose `GoogleCloudProject` binding
it reuses for the Cloud Logging trace field):

    Injector([ModelModule(), ObservabilityModule()])
"""

from __future__ import annotations

import os

from injector import Module, inject, provider, singleton

# `Tracer` must be importable at runtime (not just under TYPE_CHECKING): injector
# resolves the provider's return annotation via `get_type_hints`, which evaluates
# it — a deferred/TYPE_CHECKING-only name raises `NameError` at binding time.
from opentelemetry.trace import Tracer

from .logging import PROJECT_ENV, configure_logging
from .project_types import GoogleCloudProject
from .telemetry import configure_tracing, get_tracer


def configure_observability(
    *,
    project: str = "",
    service_name: str = "",
    log_level: str | None = None,
    json_logs: bool | None = None,
) -> None:
    """Configure structured logging and distributed tracing (idempotent).

    Safe to call more than once and safe to call before or after ADK's own
    telemetry setup — tracing cooperates with an existing provider.

    Args:
        project: GCP project id for the Cloud Logging trace field. Defaults to
            ``GOOGLE_CLOUD_PROJECT``.
        service_name: ``service.name`` resource attribute for traces. Defaults to
            ``OTEL_SERVICE_NAME`` then the module default.
        log_level: Log level override (defaults to ``LOG_LEVEL`` then ``INFO``).
        json_logs: Force JSON (True) or console (False) logs; None auto-detects.
    """
    project = project or os.environ.get(PROJECT_ENV, "")
    configure_logging(project=project, level=log_level, json_logs=json_logs)
    configure_tracing(service_name=service_name)


class ObservabilityModule(Module):
    """Injector module providing an OTel ``Tracer`` for custom spans.

    It reuses the shared `GoogleCloudProject` binding (provided by
    `ModelModule`), so install the two together. Resolving observability is a
    side effect handled by `configure_observability`; this module only exposes
    the tracer for constructor injection.
    """

    @singleton
    @provider
    @inject
    def provide_tracer(self, project: GoogleCloudProject) -> Tracer:
        """Provide the (singleton) application tracer.

        Args:
            project: The injected GCP project (reused from `ModelModule`); kept
                as a dependency so the DI graph matches the logging trace field.

        Returns:
            A named OpenTelemetry `Tracer`.
        """
        del project  # Bound for graph consistency; not needed to build a tracer.
        return get_tracer("app")
