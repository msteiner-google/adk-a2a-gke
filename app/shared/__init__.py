"""Shared agent library — single source of truth reused across variants.

This package lives once at the repo root (`shared/`) and is symlinked into each
variant's agent directory (e.g. `template/app/shared -> ../../shared`). At
scaffold time the CLI dereferences the symlink and copies these files verbatim
into the generated project, so every variant ships identical, up-to-date shared
code without duplicating it in the repo.

Edit the logic here once; every variant picks it up.
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
