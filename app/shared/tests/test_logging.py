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

"""Tests for loguru setup: severity mapping, trace correlation, stdlib intercept.

Hermetic: no network. Trace context comes from a local in-memory tracer (see
`support.in_memory_tracer`) so no global OpenTelemetry state is touched.
"""

from __future__ import annotations

import logging as stdlib_logging
from typing import TYPE_CHECKING

import pytest
from loguru import logger

from .. import logging as slog
from .support import in_memory_tracer

if TYPE_CHECKING:
    from loguru import Record


@pytest.fixture(autouse=True)
def _reset_logging():
    """Reset loguru sinks + the module guard around each test."""
    slog._configured = False
    logger.remove()
    yield
    logger.remove()
    slog._configured = False


# --- pure helpers -----------------------------------------------------------


@pytest.mark.parametrize(
    ("level", "severity"),
    [
        ("TRACE", "DEBUG"),
        ("DEBUG", "DEBUG"),
        ("INFO", "INFO"),
        ("SUCCESS", "NOTICE"),
        ("WARNING", "WARNING"),
        ("ERROR", "ERROR"),
        ("CRITICAL", "CRITICAL"),
        ("SOMETHING", "DEFAULT"),
    ],
)
def test_gcp_severity_mapping(level, severity):
    assert slog.gcp_severity(level) == severity


def test_trace_field_with_and_without_project():
    assert slog.trace_field("proj", "abc123") == "projects/proj/traces/abc123"
    assert slog.trace_field("", "abc123") == "abc123"


# --- stdlib interception ----------------------------------------------------


def test_intercept_handler_forwards_to_loguru():
    captured: list[Record] = []
    logger.add(lambda m: captured.append(m.record), level="DEBUG", format="{message}")

    std_logger = stdlib_logging.getLogger("some.library")
    std_logger.handlers = [slog.InterceptHandler()]
    std_logger.setLevel(stdlib_logging.WARNING)
    std_logger.propagate = False
    std_logger.warning("library says %s", "hi")

    assert len(captured) == 1
    record = captured[0]
    assert record["message"] == "library says hi"
    assert record["level"].name == "WARNING"


# --- structured payload with trace correlation ------------------------------


def _capture_record(project: str, *, in_span: bool) -> Record:
    """Log one message (optionally inside a span) and return its loguru record."""
    holder: dict[str, Record] = {}
    logger.configure(patcher=slog._make_patcher(project))
    logger.add(lambda m: holder.__setitem__("record", m.record), format="{message}")

    if in_span:
        tracer, _ = in_memory_tracer()
        with tracer.start_as_current_span("work"):
            logger.info("hello {k}", k="world")
    else:
        logger.info("hello {k}", k="world")
    return holder["record"]


def test_build_gcp_payload_includes_trace_fields_in_span():
    record = _capture_record("myproj", in_span=True)
    payload = slog.build_gcp_payload(record)

    assert payload["severity"] == "INFO"
    assert payload["message"] == "hello world"
    assert payload["logging.googleapis.com/trace"].startswith("projects/myproj/traces/")
    assert len(payload["logging.googleapis.com/spanId"]) == 16
    assert payload["extra"] == {"k": "world"}


def test_build_gcp_payload_omits_trace_fields_without_span():
    record = _capture_record("myproj", in_span=False)
    payload = slog.build_gcp_payload(record)

    assert "logging.googleapis.com/trace" not in payload
    assert "logging.googleapis.com/spanId" not in payload


def test_build_gcp_payload_captures_exception():
    holder: dict[str, Record] = {}
    logger.configure(patcher=slog._make_patcher(""))
    logger.add(lambda m: holder.__setitem__("record", m.record), format="{message}")
    try:
        raise ValueError("boom")
    except ValueError:
        logger.opt(exception=True).error("failed")

    payload = slog.build_gcp_payload(holder["record"])
    assert "ValueError: boom" in payload["exception"]


# --- configuration ----------------------------------------------------------


def test_configure_logging_is_idempotent(monkeypatch):
    calls = {"n": 0}
    real_configure = logger.configure

    def counting_configure(*args, **kwargs):
        calls["n"] += 1
        return real_configure(*args, **kwargs)

    monkeypatch.setattr(logger, "configure", counting_configure)

    slog.configure_logging(json_logs=True)
    slog.configure_logging(json_logs=True)
    assert calls["n"] == 1  # second call short-circuits


def test_configure_logging_routes_stdlib_root():
    slog.configure_logging(json_logs=True, force=True)
    assert any(
        isinstance(h, slog.InterceptHandler)
        for h in stdlib_logging.getLogger().handlers
    )
