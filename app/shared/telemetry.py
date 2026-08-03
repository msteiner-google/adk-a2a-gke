"""OpenTelemetry tracing setup: exporter selection, propagation, instrumentation.

ADK already emits GenAI-semantic-convention spans (``invoke_agent``,
``execute_tool``, ``generate_content {model}``) over OpenTelemetry. This module
does the *thin* configuration around that so traces are useful in a generated
project — especially a multi-agent one:

- **Exporter selection.** Cloud Trace by default (zero-config on GCP), redirected
  to any OTLP collector/Tempo/Jaeger simply by setting
  ``OTEL_EXPORTER_OTLP_ENDPOINT`` — see `select_exporter_kind`. The actual
  provider setup reuses ADK's public helpers
  (`google.adk.telemetry.setup.maybe_set_otel_providers` +
  `google.adk.telemetry.google_cloud.get_gcp_exporters`), which are cooperative:
  if a ``TracerProvider`` is already installed (e.g. by ADK's ``otel_to_cloud``
  serving path) they leave it alone.
- **Context propagation.** Installs the W3C ``tracecontext`` + ``baggage``
  propagators globally so the ``traceparent`` header is read on inbound requests
  and written on outbound ones.
- **A2A propagation.** Instruments the httpx client (outbound A2A calls inject
  ``traceparent``) and, best-effort, FastAPI (inbound A2A calls extract it) so a
  single trace spans every hop in the cluster. `instrument_fastapi_app` is
  provided for serving layers that build the app explicitly.

Everything here is idempotent and best-effort: missing optional instrumentation
packages degrade to "no instrumentation" rather than crashing serving.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any, Literal

from opentelemetry import trace
from opentelemetry.baggage.propagation import W3CBaggagePropagator
from opentelemetry.propagate import set_global_textmap
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

if TYPE_CHECKING:
    from collections.abc import Mapping

    from opentelemetry.sdk.resources import Resource
    from opentelemetry.trace import Tracer

# Standard OTel env vars we key exporter selection off of.
OTLP_ENDPOINT_ENV = "OTEL_EXPORTER_OTLP_ENDPOINT"
OTLP_TRACES_ENDPOINT_ENV = "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"
SERVICE_NAME_ENV = "OTEL_SERVICE_NAME"
PROJECT_ENV = "GOOGLE_CLOUD_PROJECT"

# Fallback service name when neither an argument nor OTEL_SERVICE_NAME is set.
DEFAULT_SERVICE_NAME = "adk-agent"

# Which span exporter to configure.
ExporterKind = Literal["otlp", "cloud", "none"]

# Guards `configure_tracing` so repeated calls are no-ops.
_configured = False


def current_trace_ids() -> tuple[str, str]:
    """Return the active span's ``(trace_id, span_id)`` as zero-padded hex.

    Returns:
        A ``(trace_id, span_id)`` tuple (32- and 16-hex-char strings), or
        ``("", "")`` when there is no valid current span.
    """
    ctx = trace.get_current_span().get_span_context()
    if ctx.is_valid:
        return format(ctx.trace_id, "032x"), format(ctx.span_id, "016x")
    return "", ""


def select_exporter_kind(
    env: Mapping[str, str], *, has_adc: bool = False
) -> ExporterKind:
    """Choose the trace exporter from the environment (pure decision).

    Precedence: an explicit OTLP endpoint wins (redirect to a collector/Tempo/
    Jaeger); otherwise a GCP context (project set or ADC available) uses Cloud
    Trace; otherwise no exporter is configured (spans are still created locally).

    Args:
        env: The environment mapping to read (e.g. ``os.environ``).
        has_adc: Whether Application Default Credentials are available. Passed in
            (rather than detected here) to keep this function pure/testable.

    Returns:
        ``"otlp"``, ``"cloud"``, or ``"none"``.
    """
    if env.get(OTLP_ENDPOINT_ENV) or env.get(OTLP_TRACES_ENDPOINT_ENV):
        return "otlp"
    if env.get(PROJECT_ENV) or has_adc:
        return "cloud"
    return "none"


def _has_adc() -> bool:
    """Return whether Application Default Credentials can be resolved.

    Returns:
        True if `google.auth.default` succeeds, False otherwise. Failures are
        swallowed so credential probing never breaks startup.
    """
    try:
        import google.auth

        google.auth.default()
    except Exception:
        # ADC probing must never crash startup; treat any failure as "no ADC".
        return False
    return True


def _resource(env: Mapping[str, str], service_name: str) -> Resource:
    """Build the OTel resource, setting ``service.name``.

    Args:
        env: The environment mapping (for ``OTEL_SERVICE_NAME``).
        service_name: Explicit service name override, or empty to use the env
            var, then `DEFAULT_SERVICE_NAME`.

    Returns:
        A `Resource` describing this service.
    """
    from opentelemetry.sdk.resources import SERVICE_NAME, Resource

    name = service_name or env.get(SERVICE_NAME_ENV) or DEFAULT_SERVICE_NAME
    return Resource.create({SERVICE_NAME: name})


def _instrument_http() -> None:
    """Instrument the httpx client (best-effort) so outbound calls propagate.

    The httpx client instrumentation injects the W3C ``traceparent`` header on
    outbound A2A calls (ADK's ``RemoteA2aAgent`` and the a2a-sdk client use
    httpx), so the callee can continue the trace. It is wrapped so a missing
    optional package or an already-instrumented state is a no-op.

    Inbound extraction is done per-app via `instrument_fastapi_app` (global
    FastAPI instrumentation does not reliably cover an already-built serving
    app), so it is intentionally not attempted here.
    """
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        HTTPXClientInstrumentor().instrument()
    except Exception:
        # Best-effort: never let instrumentation break startup.
        pass


def instrument_fastapi_app(app: Any) -> None:
    """Instrument a specific FastAPI app instance for inbound trace extraction.

    Global FastAPI instrumentation only covers apps created *after* it runs, so a
    serving layer that constructs its app explicitly (e.g. an overlaid
    ``fast_api_app.py``) should call this on that app to guarantee inbound A2A
    requests continue the caller's trace. Safe to call more than once.

    Args:
        app: The FastAPI application instance to instrument. Typed ``Any`` so
            ``shared`` need not import FastAPI, keeping this package portable.
    """
    # Best-effort (see `_instrument_http`): never let instrumentation break serving.
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app)
    except Exception:
        pass


def _is_real_tracer_provider(provider: object) -> bool:
    """Return whether a real SDK ``TracerProvider`` is already installed.

    Args:
        provider: The provider returned by ``trace.get_tracer_provider()``.

    Returns:
        True if it is an SDK `TracerProvider` (so exporting is already wired),
        False if it is still OTel's default no-op proxy.
    """
    return isinstance(provider, TracerProvider)


def _setup_provider(env: Mapping[str, str], service_name: str) -> None:
    """Install a ``TracerProvider`` with the env-selected exporter.

    Args:
        env: The environment mapping used for exporter selection + resource.
        service_name: Explicit service name override (or empty for env/default).
    """
    resource = _resource(env, service_name)

    # Only probe ADC when it could change the decision (no OTLP + no project).
    needs_adc = not (
        env.get(OTLP_ENDPOINT_ENV)
        or env.get(OTLP_TRACES_ENDPOINT_ENV)
        or env.get(PROJECT_ENV)
    )
    kind = select_exporter_kind(env, has_adc=_has_adc() if needs_adc else False)

    if kind == "none":
        # No exporter, but a real provider so spans record + propagate locally.
        trace.set_tracer_provider(TracerProvider(resource=resource))
        return

    from google.adk.telemetry.setup import maybe_set_otel_providers

    if kind == "cloud":
        from google.adk.telemetry.google_cloud import get_gcp_exporters

        # maybe_set_otel_providers is a no-op if a provider is already set.
        maybe_set_otel_providers(
            [get_gcp_exporters(enable_cloud_tracing=True)], resource
        )
    else:  # "otlp" — maybe_set_otel_providers adds OTLP exporters from env vars.
        maybe_set_otel_providers([], resource)


def configure_tracing(
    env: Mapping[str, str] | None = None,
    *,
    service_name: str = "",
    force: bool = False,
) -> None:
    """Set up propagation, HTTP instrumentation, and span export (idempotent).

    Always installs the W3C propagators and HTTP instrumentation. A span exporter
    is configured only when no real ``TracerProvider`` exists yet, so this
    cooperates with ADK's ``otel_to_cloud`` serving path rather than fighting it.

    Args:
        env: Environment mapping (defaults to ``os.environ``).
        service_name: Explicit ``service.name`` (else ``OTEL_SERVICE_NAME`` /
            default).
        force: Re-run even if already configured (mainly for tests).
    """
    global _configured
    if _configured and not force:
        return

    env = os.environ if env is None else env

    set_global_textmap(
        CompositePropagator([TraceContextTextMapPropagator(), W3CBaggagePropagator()])
    )
    _instrument_http()

    if not _is_real_tracer_provider(trace.get_tracer_provider()):
        _setup_provider(env, service_name)

    _configured = True


def get_tracer(name: str = "app") -> Tracer:
    """Return a named tracer for creating custom spans.

    Args:
        name: The instrumentation scope name (usually the module ``__name__``).

    Returns:
        An OpenTelemetry `Tracer`.
    """
    return trace.get_tracer(name)
