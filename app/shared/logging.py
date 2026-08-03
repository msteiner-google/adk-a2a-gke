"""Structured logging via loguru, correlated with OpenTelemetry traces.

This module makes every log line in a generated project consistent and
trace-aware, with one function call (`configure_logging`, usually via
`shared.observability.configure_observability`):

- **One stream.** An `InterceptHandler` is installed on the stdlib root logger so
  records from libraries that use `logging` (ADK, uvicorn, `google.genai`, ...)
  flow *through* loguru instead of forming a second, differently-formatted
  stream.
- **Trace correlation.** A loguru patcher stamps the active span's
  `trace_id`/`span_id` onto every record. In JSON mode it also emits the
  `logging.googleapis.com/trace` + `spanId` fields so Cloud Logging links each
  log entry to its trace in the console.
- **Structured output.** JSON to stdout in production (picked up by the GKE /
  Cloud Run logging agents and mapped to Cloud Logging fields), or a colorized
  human-readable format on a TTY. Controlled by `LOG_FORMAT` (`json`/`console`)
  and `LOG_LEVEL`.

The heavy DI wiring lives in `shared.observability`; this module is deliberately
importable and callable on its own.
"""

from __future__ import annotations

import logging
import os
import sys
import traceback
from typing import TYPE_CHECKING, Any

from loguru import logger

from .telemetry import current_trace_ids

if TYPE_CHECKING:
    from loguru import Record

# Env vars controlling logging behavior.
LOG_LEVEL_ENV = "LOG_LEVEL"
LOG_FORMAT_ENV = "LOG_FORMAT"
PROJECT_ENV = "GOOGLE_CLOUD_PROJECT"

# Level used when neither an argument nor `LOG_LEVEL` is provided.
DEFAULT_LOG_LEVEL = "INFO"

# Maps loguru level names to Cloud Logging severities. Unknown levels fall back
# to "DEFAULT" (Cloud Logging's unspecified severity).
_GCP_SEVERITY = {
    "TRACE": "DEBUG",
    "DEBUG": "DEBUG",
    "INFO": "INFO",
    "SUCCESS": "NOTICE",
    "WARNING": "WARNING",
    "ERROR": "ERROR",
    "CRITICAL": "CRITICAL",
}

# stdlib loggers that libraries pre-configure with their own handlers; we clear
# those and let records propagate to the root `InterceptHandler` instead, so
# everything funnels through loguru exactly once.
_STDLIB_LOGGERS_TO_RESET = (
    "uvicorn",
    "uvicorn.error",
    "uvicorn.access",
    "gunicorn",
    "gunicorn.error",
    "fastapi",
    "google_adk",
    "google.adk",
    "google.genai",
)

# Colorized format for local/interactive runs (JSON is used otherwise).
_CONSOLE_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> "
    "<level>{level: <8}</level> "
    "<cyan>{name}</cyan>:<cyan>{line}</cyan> "
    "trace=<yellow>{extra[trace_id]}</yellow> - <level>{message}</level>"
)

# Guards `configure_logging` so repeated calls (e.g. re-imports) are no-ops.
_configured = False


class InterceptHandler(logging.Handler):
    """Routes stdlib ``logging`` records into loguru.

    Installed on the root logger by `configure_logging`, this preserves the
    original level and finds the correct caller frame so loguru reports the real
    source location (not this handler).
    """

    def emit(self, record: logging.LogRecord) -> None:
        """Forward a stdlib record to loguru at the corresponding level.

        Args:
            record: The stdlib log record to forward.
        """
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        # Walk out of the logging module frames so loguru attributes the message
        # to the code that actually logged it.
        frame, depth = logging.currentframe(), 2
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(
            level, record.getMessage()
        )


def gcp_severity(level_name: str) -> str:
    """Map a loguru level name to a Cloud Logging severity.

    Args:
        level_name: The loguru level name (e.g. ``"WARNING"``).

    Returns:
        The corresponding Cloud Logging severity, or ``"DEFAULT"`` if unknown.
    """
    return _GCP_SEVERITY.get(level_name.upper(), "DEFAULT")


def trace_field(project: str, trace_id: str) -> str:
    """Build the Cloud Logging ``trace`` field value for a trace id.

    Args:
        project: The GCP project id. When empty, the bare trace id is returned
            (still useful, but Cloud Logging links traces only when the
            ``projects/<project>/traces/<id>`` form is used).
        trace_id: The 32-hex-char trace id.

    Returns:
        Either ``projects/<project>/traces/<trace_id>`` or the bare trace id.
    """
    return f"projects/{project}/traces/{trace_id}" if project else trace_id


def _make_patcher(project: str):
    """Build a loguru patcher that stamps trace ids onto each record.

    Args:
        project: The GCP project id, carried on the record for the JSON sink.

    Returns:
        A patcher callable suitable for ``logger.configure(patcher=...)``.
    """

    def patch(record: Record) -> None:
        trace_id, span_id = current_trace_ids()
        record["extra"]["trace_id"] = trace_id
        record["extra"]["span_id"] = span_id
        record["extra"]["gcp_project"] = project

    return patch


def build_gcp_payload(record: Record) -> dict[str, Any]:
    """Render a loguru record as a Cloud Logging structured-log dict.

    Args:
        record: The loguru record (already patched with trace ids).

    Returns:
        A JSON-serializable dict using Cloud Logging's special fields
        (``severity``, ``message``, ``logging.googleapis.com/*``).
    """
    extra = dict(record["extra"])
    trace_id = extra.pop("trace_id", "")
    span_id = extra.pop("span_id", "")
    project = extra.pop("gcp_project", "")

    payload: dict[str, Any] = {
        "severity": gcp_severity(record["level"].name),
        "message": record["message"],
        "timestamp": record["time"].isoformat(),
        "logging.googleapis.com/sourceLocation": {
            "file": str(record["file"].path),
            "line": str(record["line"]),
            "function": record["function"],
        },
    }

    if trace_id:
        payload["logging.googleapis.com/trace"] = trace_field(project, trace_id)
        payload["logging.googleapis.com/spanId"] = span_id
        payload["logging.googleapis.com/trace_sampled"] = True

    if extra:
        payload["extra"] = extra

    exception = record["exception"]
    if exception is not None:
        payload["exception"] = "".join(
            traceback.format_exception(
                exception.type, exception.value, exception.traceback
            )
        )

    return payload


def _make_json_sink(serialize=None):
    """Build a loguru sink that writes one Cloud Logging JSON object per line.

    Args:
        serialize: Optional JSON encoder (defaults to ``json.dumps``); injectable
            for tests.

    Returns:
        A sink callable suitable for ``logger.add(...)``.
    """
    import json

    encode = serialize or (lambda obj: json.dumps(obj, default=str))

    def sink(message: Any) -> None:
        sys.stdout.write(encode(build_gcp_payload(message.record)) + "\n")
        sys.stdout.flush()

    return sink


def _use_json(explicit: bool | None) -> bool:
    """Decide whether to emit JSON logs.

    Args:
        explicit: An explicit override, or None to auto-detect.

    Returns:
        True for JSON output, False for the console format. When not overridden,
        ``LOG_FORMAT`` wins, then a non-TTY stderr implies JSON.
    """
    if explicit is not None:
        return explicit
    fmt = os.environ.get(LOG_FORMAT_ENV, "").strip().lower()
    if fmt == "json":
        return True
    if fmt == "console":
        return False
    return not sys.stderr.isatty()


def configure_logging(
    *,
    project: str = "",
    level: str | None = None,
    json_logs: bool | None = None,
    force: bool = False,
) -> None:
    """Configure loguru + route stdlib logging through it (idempotent).

    Args:
        project: GCP project id used to build the Cloud Logging trace field. When
            empty, falls back to ``GOOGLE_CLOUD_PROJECT``.
        level: Minimum level (e.g. ``"INFO"``). Defaults to ``LOG_LEVEL`` then
            ``DEFAULT_LOG_LEVEL``.
        json_logs: Force JSON (True) or console (False) output; None auto-detects
            via ``LOG_FORMAT`` / TTY.
        force: Reconfigure even if already configured (mainly for tests).
    """
    global _configured
    if _configured and not force:
        return

    project = project or os.environ.get(PROJECT_ENV, "")
    level = (level or os.environ.get(LOG_LEVEL_ENV) or DEFAULT_LOG_LEVEL).upper()

    # Funnel stdlib logging into loguru via a single root handler.
    logging.basicConfig(handlers=[InterceptHandler()], level=level, force=True)
    for name in _STDLIB_LOGGERS_TO_RESET:
        std_logger = logging.getLogger(name)
        std_logger.handlers = []
        std_logger.propagate = True

    logger.configure(patcher=_make_patcher(project))
    logger.remove()
    if _use_json(json_logs):
        logger.add(_make_json_sink(), level=level, format="{message}")
    else:
        logger.add(sys.stderr, level=level, format=_CONSOLE_FORMAT, colorize=True)

    _configured = True
