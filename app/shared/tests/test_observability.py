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
