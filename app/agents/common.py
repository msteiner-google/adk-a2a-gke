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

"""Common tools shared across agents.

``remember`` / ``recall`` demonstrate **context sharing and propagation**: they
read and write ``tool_context.state``, which ADK persists through the configured
session service. Because session state travels with the invocation — including
across A2A hops to remote sub-agents — a value one agent remembers is visible to
the agents it delegates to, and survives pod restarts when a durable session
backend is configured (``SESSION_BACKEND``; see ``app/cluster/session.py``).

These are used by every agent in the cluster, so they live here rather than in a
single agent's folder. Agent-specific tools live alongside their agent (e.g.
``app/agents/math/tools.py``).
"""

from __future__ import annotations

# NOTE: ToolContext must be imported at runtime, NOT under TYPE_CHECKING. ADK
# builds each tool's function declaration by calling typing.get_type_hints() on
# the tool function, which evaluates the `tool_context: ToolContext` annotation.
# With `from __future__ import annotations` that annotation is a string, so a
# TYPE_CHECKING-only import raises `NameError: name 'ToolContext' is not defined`
# at request time (breaking every tool that takes a tool_context).
from google.adk.tools.tool_context import ToolContext

# State keys are namespaced so shared context is easy to spot in traces/logs.
_STATE_PREFIX = "shared:"


def remember(key: str, value: str, tool_context: ToolContext) -> dict[str, str]:
    """Store a value in shared session state for later steps and sub-agents.

    Use this to carry context forward — the value is written to session state,
    which propagates to delegated agents and persists across turns.

    Args:
        key: A short name for the value (e.g. ``"customer_id"``).
        value: The value to remember.
        tool_context: Injected by ADK; provides access to session state.

    Returns:
        A confirmation mapping with the stored key and value.
    """
    tool_context.state[f"{_STATE_PREFIX}{key}"] = value
    return {"status": "stored", "key": key, "value": value}


def recall(key: str, tool_context: ToolContext) -> dict[str, str]:
    """Retrieve a value previously stored with :func:`remember`.

    Args:
        key: The name the value was stored under.
        tool_context: Injected by ADK; provides access to session state.

    Returns:
        A mapping with the value, or a ``not_found`` status if the key is unset.
    """
    value = tool_context.state.get(f"{_STATE_PREFIX}{key}")
    if value is None:
        return {"status": "not_found", "key": key}
    return {"status": "found", "key": key, "value": str(value)}
