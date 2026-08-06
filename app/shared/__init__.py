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
