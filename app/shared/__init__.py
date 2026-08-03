"""Shared agent library — cross-cutting utilities used across the codebase.

Models (tier selection and construction), observability (logging + tracing),
secrets, and artifact storage live here. This package sits at the bottom of the
dependency graph: it must not import from ``app.agents`` or ``app.cluster``, and
it stays free of project-specific logic so it can be reused as-is by other
services. It is also kept portable to Python 3.11, which is why it imports
``override`` from ``typing_extensions`` rather than ``typing``.
"""

__all__ = [
    "artifacts",
    "config",
    "logging",
    "model_catalog",
    "model_factory",
    "model_selection",
    "observability",
    "project_types",
    "secrets",
    "telemetry",
    "tools",
]
