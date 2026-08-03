"""Root agent definition for the GKE multi-agent variant.

The *same* container image runs every agent in the cluster; the ``AGENT_NAME``
environment variable selects which one this process becomes at startup. Every
agent is built the same way — there is no special "orchestrator" code path. An
agent that coordinates others is simply one whose spec declares ``peers`` (see
``app/agents``); the uniform ``build_agent`` attaches whatever peers the cluster
configuration resolved for this process.

- ``AGENT_NAME=orchestrator`` (default): the planner. Its spec lists ``research``
  and ``math`` as peers, so ``build_agent`` attaches them as ``RemoteA2aAgent``
  children reached over A2A.
- ``AGENT_NAME=research`` / ``AGENT_NAME=math`` (or any registered agent): a leaf
  agent with no peers, served over A2A so others can reach it.

Dependency injection ties everything together (see ``app/cluster/di.py``):
- the shared ``ModelModule`` provides the Gemini model tiers,
- ``ClusterModule`` provides the config + the ``AgentResolver`` (the resolver
  that connects this agent to the others in the cluster), and
- ``SessionModule`` provides the pluggable session/memory/artifact services.

Keep the ``app`` object exported — the base serving/deployment layer imports it,
and ``App(name=...)`` must equal the agent directory (``app``).
"""

import os

from a2a.server.tasks import TaskStore
from google.adk.apps import App
from google.adk.artifacts.base_artifact_service import BaseArtifactService
from google.adk.memory import BaseMemoryService
from google.adk.sessions import BaseSessionService
from injector import Injector

from .agents import AGENTS, build_agent
from .cluster.db import Database
from .cluster.di import ClusterModule, SessionModule
from .cluster.resolver import AgentResolver
from .shared.config import ModelModule, Models
from .shared.observability import configure_observability

# Enable structured (loguru) logging + OpenTelemetry tracing before anything
# else. The service name is this pod's agent (orchestrator / research / math) so
# each shows up distinctly in Cloud Trace; an explicit OTEL_SERVICE_NAME still
# wins. Trace context propagates across A2A hops (httpx injects `traceparent`
# outbound; the serving overlay extracts it inbound), so one trace spans the
# whole cluster. See app/shared/observability.py and app/fast_api_app.py.
configure_observability(
    service_name=os.environ.get("OTEL_SERVICE_NAME") or os.environ.get("AGENT_NAME", "")
)

# One injector wires the shared models, the cluster resolver, and the session /
# memory backends. Resolving real classes keeps `injector.get(...)` type-safe.
_injector = Injector([ModelModule(), ClusterModule(), SessionModule()])

models = _injector.get(Models)
resolver = _injector.get(AgentResolver)

# Pluggable session, memory and artifact services plus the A2A task store
# (in-memory by default; durable backends via SESSION_BACKEND / MEMORY_BACKEND /
# ARTIFACT_STORAGE_URI / TASK_STORE_BACKEND). Resolved here so misconfiguration
# fails fast at startup.
#
# These are the instances the serving layer actually uses: app/fast_api_app.py
# imports them for the Runner and the A2A routes. Do not let a refactor reduce
# them to unused exports -- if the serving layer builds its own, agents will
# quietly run on per-pod in-memory state while this module reports otherwise.
session_service = _injector.get(BaseSessionService)
memory_service = _injector.get(BaseMemoryService)
# Blob-store-agnostic artifact storage (app/cluster/artifacts.py ->
# app/shared/artifacts.py). Shared by every agent so a file saved by a worker is
# loadable by the orchestrator on the same session.
artifact_service = _injector.get(BaseArtifactService)
task_store = _injector.get(TaskStore)

# The shared engine holder, exported so the serving layer can dispose of the
# pool and stop the AlloyDB connector's background refresh tasks on shutdown.
database = _injector.get(Database)

# Select this process's agent from the registry by name. Every agent is built by
# the same `build_agent`; peers (if any) are attached uniformly by the resolver.
_name = resolver.config.name
if _name not in AGENTS:
    raise KeyError(f"Unknown AGENT_NAME {_name!r}; registered: {sorted(AGENTS)}")
root_agent = build_agent(AGENTS[_name], models, resolver)

# The `App` wraps the root agent for serving/deployment.
# NOTE: `name` must match the agent directory (default: "app").
app = App(
    root_agent=root_agent,
    name="app",
)
