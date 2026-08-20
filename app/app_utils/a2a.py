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

"""Attach A2A (Agent2Agent) endpoints to the FastAPI app.

func:`attach_a2a_routes` registers the dynamic
agent-card endpoint and the JSON-RPC endpoint so the same app serves A2A
alongside the adk_api routes, reachable by A2A clients and Gemini Enterprise A2A
registration.

**A2A protocol v1.0 (a2a-sdk 1.x).** The 0.3-era wrapper classes
(``A2AFastAPIApplication`` / ``A2AStarletteApplication``) were removed; routes
now come from factory functions in ``a2a.server.routes`` that return plain
Starlette ``Route`` objects. Three consequences are load-bearing here:

* ``DefaultRequestHandler`` now *requires* ``agent_card``. In 0.3 the card was
  handed to the application wrapper, so the handler never saw it; in 1.0 the
  handler answers ``GetExtendedAgentCard`` itself and cannot be built without it.
* ``add_a2a_routes_to_fastapi`` is used rather than ``FastAPI(routes=...)`` or a
  bare ``app.routes.extend``. All three mount working endpoints, but only this
  helper registers them as ``APIRoute`` instances, so the A2A endpoints keep
  appearing in ``/docs`` and ``/openapi.json`` — which is how this app was
  discoverable before the upgrade.
* ``EXTENDED_AGENT_CARD_PATH`` no longer exists. The authenticated extended card
  moved from its own well-known URL to the ``GetExtendedAgentCard`` RPC method
  on the JSON-RPC route, so there is no third URL to mount.

``enable_v0_3_compat=True`` keeps the JSON-RPC route answering 0.3-shaped
requests as well as 1.0 ones. It costs one extra dispatch branch and is what
lets a peer still running a 0.3 client — another squad's agent, an older
sidecar, the A2A Inspector — keep working while the fleet rolls forward.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import (
    add_a2a_routes_to_fastapi,
    create_agent_card_routes,
    create_jsonrpc_routes,
)
from a2a.server.tasks import TaskStore
from a2a.types import AgentCapabilities, AgentExtension
from a2a.utils.constants import AGENT_CARD_WELL_KNOWN_PATH
from google.adk.a2a.executor.a2a_agent_executor import A2aAgentExecutor
from google.adk.a2a.utils.agent_card_builder import AgentCardBuilder
from google.adk.workflow import Workflow

if TYPE_CHECKING:
    from fastapi import FastAPI
    from google.adk.agents import BaseAgent
    from google.adk.runners import Runner

# URI advertised on the agent card describing the executor extension shipped
# by ADK. Kept as a module-level constant so callers can override or extend
# the capabilities list when needed.
_ADK_AGENT_EXECUTOR_EXTENSION_URI = (
    "https://google.github.io/adk-docs/a2a/a2a-extension/"
)


def _default_capabilities() -> AgentCapabilities:
    """Returns the default A2A capabilities advertised by every agent."""
    return AgentCapabilities(
        streaming=True,
        extensions=[
            AgentExtension(
                uri=_ADK_AGENT_EXECUTOR_EXTENSION_URI,
                description=("Ability to use the new agent executor implementation"),
            ),
        ],
    )


async def attach_a2a_routes(
    app: FastAPI,
    *,
    agent: BaseAgent | Workflow,
    runner: Runner,
    task_store: TaskStore,
    rpc_path: str,
    capabilities: AgentCapabilities | None = None,
    agent_version: str | None = None,
    app_url: str | None = None,
) -> None:
    """Register A2A routes (JSON-RPC + agent-card endpoints) under ``rpc_path``.

    Builds a dynamic agent card from ``agent`` and mounts the routes on ``app``.
    ``agent`` may be an ``LlmAgent`` or a graph node (ADK's ``AgentCardBuilder``
    accepts ``BaseAgent | Workflow``), so a graph-rooted agent is served here on
    exactly the same path as a model-driven one.
    The ``runner`` should share the session/artifact/memory services with the
    standard ADK path. ``capabilities``, ``agent_version``, and ``app_url``
    override their defaults (streaming + ADK extension, ``AGENT_VERSION``,
    ``APP_URL``). Call once per app — typically in a FastAPI ``lifespan``, since
    the card is built asynchronously; repeated calls register duplicate routes.
    """
    resolved_app_url = app_url or os.getenv("APP_URL", "http://0.0.0.0:8000")
    resolved_agent_version = agent_version or os.getenv("AGENT_VERSION", "0.1.0")
    resolved_capabilities = capabilities or _default_capabilities()

    agent_card = await AgentCardBuilder(
        agent=agent,
        capabilities=resolved_capabilities,
        rpc_url=f"{resolved_app_url}{rpc_path}",
        agent_version=resolved_agent_version,
    ).build()

    request_handler = DefaultRequestHandler(
        agent_executor=A2aAgentExecutor(runner=runner),
        task_store=task_store,
        agent_card=agent_card,
    )

    add_a2a_routes_to_fastapi(
        app,
        agent_card_routes=create_agent_card_routes(
            agent_card,
            card_url=f"{rpc_path}{AGENT_CARD_WELL_KNOWN_PATH}",
        ),
        jsonrpc_routes=create_jsonrpc_routes(
            request_handler,
            rpc_path,
            enable_v0_3_compat=True,
        ),
    )
