"""Registry of the agents in the cluster.

Every agent is defined the same way — a declarative :class:`AgentSpec` in its own
``app/agents/<name>/agent.py`` — and registered here. This module is the single
source of truth for *which* agents exist and their default peer topology.

- :data:`AGENTS` maps a name -> its :class:`AgentSpec`. The name is also the
  Kubernetes Service name and the value of ``AGENT_NAME`` for that agent's pods.
- :data:`DEFAULT_AGENT` is the agent a process becomes when ``AGENT_NAME`` is
  unset (handy for ``adk web`` and tests).

There is no special "orchestrator" type: the orchestrator is simply the agent
whose spec lists ``peers``. Nor is there a special *graph* type: an agent whose
spec carries a ``root_node`` (``planner``) is served as an ADK ``Workflow``
instead of an ``LlmAgent``, but is registered, deployed and reached identically.

To add an agent, create ``app/agents/<name>/`` with
an ``agent.py`` exposing a ``SPEC``, register it below, and (for the cluster) add
a Deployment/Service in ``infra/kustomize/base/workers.yaml``. Nothing else needs
to change.
"""

from __future__ import annotations

from app.agents.base import AgentSpec, build_agent
from app.agents.math.agent import SPEC as MATH
from app.agents.orchestrator.agent import SPEC as ORCHESTRATOR
from app.agents.planner.agent import SPEC as PLANNER
from app.agents.research.agent import SPEC as RESEARCH

# Registered agents, keyed by name. Order is preserved for stable listings.
AGENTS: dict[str, AgentSpec] = {
    spec.name: spec for spec in (ORCHESTRATOR, RESEARCH, MATH, PLANNER)
}

# The agent a process becomes when AGENT_NAME is unset.
DEFAULT_AGENT = ORCHESTRATOR.name

__all__ = ["AGENTS", "DEFAULT_AGENT", "AgentSpec", "build_agent"]
