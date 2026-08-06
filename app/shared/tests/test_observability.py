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

"""Tests for the observability bootstrap and DI wiring (hermetic)."""

from injector import Injector, Module, provider, singleton
from opentelemetry.trace import Tracer

from .. import observability
from ..project_types import GoogleCloudProject
from .support import TEST_PROJECT


def test_configure_observability_invokes_logging_and_tracing(monkeypatch):
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        observability,
        "configure_logging",
        lambda **kw: seen.__setitem__("logging", kw),
    )
    monkeypatch.setattr(
        observability,
        "configure_tracing",
        lambda **kw: seen.__setitem__("tracing", kw),
    )

    observability.configure_observability(
        project="p", service_name="svc", log_level="DEBUG"
    )

    assert seen["logging"] == {"project": "p", "level": "DEBUG", "json_logs": None}
    assert seen["tracing"] == {"service_name": "svc"}


def test_configure_observability_defaults_project_from_env(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "env-proj")
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        observability, "configure_logging", lambda **kw: captured.update(kw)
    )
    monkeypatch.setattr(observability, "configure_tracing", lambda **kw: None)

    observability.configure_observability()
    assert captured["project"] == "env-proj"


class _ProjectModule(Module):
    """Stand-in for ModelModule's GoogleCloudProject binding."""

    @singleton
    @provider
    def provide_project(self) -> GoogleCloudProject:
        return GoogleCloudProject(TEST_PROJECT)


def test_observability_module_provides_tracer():
    injector = Injector([_ProjectModule(), observability.ObservabilityModule()])
    tracer = injector.get(Tracer)
    assert isinstance(tracer, Tracer)
