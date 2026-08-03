"""Distributed-tracing propagation test for the multi-agent cluster.

Proves the behavior cross-pod observability hinges on: when one agent calls
another over A2A, the callee **continues the caller's trace** instead of starting
a new one. ``app/fast_api_app.py`` instruments the FastAPI app so the standard
W3C ``traceparent`` header on an inbound request is extracted into a child span.
This test exercises that instrumentation directly with a ``TestClient`` (no
cluster, no network).
"""

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from starlette.testclient import TestClient

from app.shared.telemetry import instrument_fastapi_app

# A fixed caller trace id, as it would arrive in an inbound A2A `traceparent`.
_CALLER_TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
_TRACEPARENT = f"00-{_CALLER_TRACE_ID}-00f067aa0ba902b7-01"


def _attach_in_memory_exporter() -> InMemorySpanExporter:
    """Capture spans from the global tracer provider via an in-memory exporter.

    Importing ``app`` already installed a real ``TracerProvider`` (through
    ``configure_observability``), and OpenTelemetry allows a provider to be set
    only once per process, so we attach an exporter to whatever provider exists
    rather than replacing it.

    Returns:
        The in-memory exporter recording finished spans.
    """
    exporter = InMemorySpanExporter()
    provider = trace.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        provider = TracerProvider()
        trace.set_tracer_provider(provider)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter


def test_inbound_a2a_request_continues_caller_trace():
    exporter = _attach_in_memory_exporter()

    from fastapi import FastAPI

    app = FastAPI()
    instrument_fastapi_app(app)

    @app.get("/ping")
    def ping() -> dict[str, bool]:
        return {"ok": True}

    client = TestClient(app)
    response = client.get("/ping", headers={"traceparent": _TRACEPARENT})
    assert response.status_code == 200

    server_spans = [
        s for s in exporter.get_finished_spans() if s.kind == trace.SpanKind.SERVER
    ]
    assert server_spans, "no server span was recorded (app not instrumented)"
    # The server span joined the caller's trace rather than starting a new one.
    assert format(server_spans[0].context.trace_id, "032x") == _CALLER_TRACE_ID
