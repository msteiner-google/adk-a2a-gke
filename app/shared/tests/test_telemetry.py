"""Tests for OpenTelemetry setup: exporter selection, propagation, tracing.

All hermetic: no network, no GCP credentials. The provider-building side of the
``cloud``/``otlp`` branches is intentionally covered only via the pure
`select_exporter_kind` decision (constructing the real exporters would need
credentials / a collector).
"""

import pytest
from opentelemetry.propagate import extract, inject
from opentelemetry.sdk.trace import TracerProvider

from .. import telemetry
from .support import in_memory_tracer


@pytest.fixture(autouse=True)
def _reset_configured():
    """Reset the module-level guard so each test starts unconfigured."""
    telemetry._configured = False
    yield
    telemetry._configured = False


# --- exporter selection (pure) ---------------------------------------------


def test_select_exporter_kind_prefers_otlp_endpoint():
    assert (
        telemetry.select_exporter_kind({"OTEL_EXPORTER_OTLP_ENDPOINT": "http://c"})
        == "otlp"
    )
    assert (
        telemetry.select_exporter_kind(
            {"OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "http://c/v1/traces"}
        )
        == "otlp"
    )


def test_select_exporter_kind_otlp_wins_over_project():
    # An explicit OTLP endpoint means "switch away from Cloud Trace".
    env = {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://c", "GOOGLE_CLOUD_PROJECT": "p"}
    assert telemetry.select_exporter_kind(env) == "otlp"


def test_select_exporter_kind_cloud_from_project_or_adc():
    assert telemetry.select_exporter_kind({"GOOGLE_CLOUD_PROJECT": "p"}) == "cloud"
    assert telemetry.select_exporter_kind({}, has_adc=True) == "cloud"


def test_select_exporter_kind_none_without_gcp_or_otlp():
    assert telemetry.select_exporter_kind({}) == "none"
    assert telemetry.select_exporter_kind({}, has_adc=False) == "none"


# --- trace id extraction ----------------------------------------------------


def test_current_trace_ids_outside_span():
    assert telemetry.current_trace_ids() == ("", "")


def test_current_trace_ids_inside_span():
    tracer, _ = in_memory_tracer()
    with tracer.start_as_current_span("demo"):
        trace_id, span_id = telemetry.current_trace_ids()
    assert len(trace_id) == 32
    assert len(span_id) == 16
    assert int(trace_id, 16) != 0


# --- custom spans -----------------------------------------------------------


def test_custom_span_is_recorded():
    tracer, exporter = in_memory_tracer()
    with tracer.start_as_current_span("rank_candidates"):
        pass
    assert [s.name for s in exporter.get_finished_spans()] == ["rank_candidates"]


# --- configure_tracing behavior --------------------------------------------


def test_configure_tracing_sets_w3c_propagator(monkeypatch):
    # Isolate: don't build a provider or globally instrument httpx/fastapi.
    monkeypatch.setattr(telemetry, "_setup_provider", lambda *a, **k: None)
    monkeypatch.setattr(telemetry, "_instrument_http", lambda: None)

    telemetry.configure_tracing(env={"OTEL_EXPORTER_OTLP_ENDPOINT": "http://c"})

    # A traceparent is injected when a span is current, proving the W3C
    # tracecontext propagator is installed globally.
    tracer, _ = in_memory_tracer()
    carrier: dict[str, str] = {}
    with tracer.start_as_current_span("parent"):
        inject(carrier)
    assert "traceparent" in carrier


def test_propagation_round_trip(monkeypatch):
    monkeypatch.setattr(telemetry, "_setup_provider", lambda *a, **k: None)
    monkeypatch.setattr(telemetry, "_instrument_http", lambda: None)
    telemetry.configure_tracing(env={"OTEL_EXPORTER_OTLP_ENDPOINT": "http://c"})

    tracer, _ = in_memory_tracer()
    carrier: dict[str, str] = {}
    with tracer.start_as_current_span("caller"):
        expected, _ = telemetry.current_trace_ids()
        inject(carrier)

    # Extracting the carrier reconstructs the same trace id (the A2A hop).
    ctx = extract(carrier)
    span_ctx = telemetry.trace.get_current_span(ctx).get_span_context()
    assert format(span_ctx.trace_id, "032x") == expected


def test_configure_tracing_is_idempotent(monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(
        telemetry,
        "_setup_provider",
        lambda *a, **k: calls.__setitem__("n", calls["n"] + 1),
    )
    monkeypatch.setattr(telemetry, "_instrument_http", lambda: None)
    monkeypatch.setattr(telemetry, "_is_real_tracer_provider", lambda _p: False)

    telemetry.configure_tracing(env={})
    telemetry.configure_tracing(env={})
    assert calls["n"] == 1  # second call short-circuits

    telemetry.configure_tracing(env={}, force=True)
    assert calls["n"] == 2  # force re-runs


def test_configure_tracing_defers_to_existing_provider(monkeypatch):
    # Simulate ADK's otel_to_cloud having already installed a real provider.
    monkeypatch.setattr(telemetry, "_instrument_http", lambda: None)
    monkeypatch.setattr(
        telemetry.trace, "get_tracer_provider", lambda: TracerProvider()
    )
    called = {"setup": False}
    monkeypatch.setattr(
        telemetry, "_setup_provider", lambda *a, **k: called.__setitem__("setup", True)
    )

    telemetry.configure_tracing(env={"GOOGLE_CLOUD_PROJECT": "p"})
    assert called["setup"] is False  # cooperated; did not replace the provider


def test_setup_provider_none_installs_bare_provider(monkeypatch):
    # No OTLP + no project + no ADC -> a real provider but no exporter.
    monkeypatch.setattr(telemetry, "_has_adc", lambda: False)
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        telemetry.trace,
        "set_tracer_provider",
        lambda provider: captured.__setitem__("provider", provider),
    )

    telemetry._setup_provider({}, "svc")
    assert isinstance(captured["provider"], TracerProvider)
