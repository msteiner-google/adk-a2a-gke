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

"""Registry of the agents in the cluster.

Every agent is defined the same way — a declarative :class:`AgentSpec` in its own
``app/agents/<name>/agent.py`` — and registered here. This module is the single
source of truth for *which* agents exist and their default peer topology.

- :data:`AGENTS` maps a name -> its :class:`AgentSpec`. The name is also the
  Kubernetes Service name and the value of ``AGENT_NAME`` for that agent's pods.
- :data:`DEFAULT_AGENT` is the agent a process becomes when ``AGENT_NAME`` is
  unset (handy for ``adk web`` and tests).
There is no special "orchestrator" type: an agent that coordinates others is
simply one whose spec lists ``peers``, and those peers are attached as remote
tools rather than sub-agents (see ``app/cluster/peer_tool.py``). Delegation is
not limited to one level, and the registry below is not a tree: ``math``
declares ``currency`` as a peer, so ``orchestrator -> math -> currency`` is an
ordinary chain of A2A calls with no special handling anywhere. Each hop carries
its own typed request; depth changes nothing about what a specialist can see.

To add an agent, create ``app/agents/<name>/`` with an ``agent.py`` exposing a
``SPEC``, register it below, add its request contract to
``app/agents/statuses.py`` if it reports a new status, and (for the cluster)
add a Deployment/Service in
``infra/kustomize/base/workers.yaml``.
"""

from __future__ import annotations

from app.agents.base import AgentSpec, build_agent
from app.agents.currency.agent import SPEC as CURRENCY
from app.agents.gating import is_gated
from app.agents.math.agent import SPEC as MATH
from app.agents.orchestrator.agent import SPEC as ORCHESTRATOR
from app.agents.planner.agent import SPEC as PLANNER
from app.agents.research.agent import SPEC as RESEARCH
from app.agents.trades.agent import SPEC as TRADES

# Registered agents, keyed by name. Order is preserved for stable listings.
AGENTS: dict[str, AgentSpec] = {
    spec.name: spec
    for spec in (ORCHESTRATOR, RESEARCH, MATH, PLANNER, TRADES, CURRENCY)
}

# The agent a process becomes when AGENT_NAME is unset.
DEFAULT_AGENT = ORCHESTRATOR.name


def suspending_agents() -> frozenset[str]:
    """Return the names of agents that can suspend awaiting authorization.

    An agent owning a gated tool must be reached as a sub-agent, or the
    authorization request it raises is swallowed by ``AgentTool`` and never
    reaches a human. Derived from the registry rather than listed, so marking a
    tool with ``@gated`` is the single act that wires its agent correctly.

    Returns:
        The names of every registered agent with at least one gated tool.
    """
    return frozenset(
        name
        for name, spec in AGENTS.items()
        if any(is_gated(tool) for tool in spec.tools)
    )


def agent_descriptions() -> dict[str, str]:
    """Return each registered agent's one-line capability summary, by name.

    This is what a caller's model routes on. A peer is reached by URL, and ADK
    resolves its agent card **lazily, at first invocation** — long after the
    LLM had to choose which specialist to hand the sub-task to
    (``google/adk/agents/remote_a2a_agent.py``: "a parent agent reads the
    description to build its transfer instruction, which happens before this
    agent ever runs"). Left to the resolver's own placeholder, every peer is
    described identically apart from its name, and the choice is made on the
    bare word ``math`` versus ``trades``. Measured: a currency conversion
    routed to ``trades`` and to ``research``.

    Taking the description from the registry costs nothing — the specs are
    already the source of truth for the cards those descriptions end up in —
    and it is why this lives here rather than in ``app/cluster``: the resolver
    cannot import ``app.agents`` (``agents.base`` imports the resolver), so
    ``app/cluster/di.py`` passes this in, exactly as it does for
    :func:`suspending_agents`.

    Returns:
        Agent name -> the ``description`` from its :class:`AgentSpec`.
    """
    return {name: spec.description for name, spec in AGENTS.items()}


__all__ = [
    "AGENTS",
    "DEFAULT_AGENT",
    "AgentSpec",
    "agent_descriptions",
    "build_agent",
    "suspending_agents",
]
