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

"""The uniform agent model: one spec, one builder for *every* agent.

Every agent is described by a declarative :class:`AgentSpec` and constructed by
the single :func:`build_agent` function. There is no special "orchestrator"
class: an agent that coordinates others is simply one whose spec declares
``peers``. Whether an agent delegates is therefore *data*, not a separate code
path.

**Peers are attached in whichever slot their behaviour requires.**
:func:`build_agent` asks the injected ``AgentResolver`` for the peers the
cluster configuration resolved for this process; a peer that can suspend
awaiting human authorization becomes a ``sub_agent``, and every other peer
becomes a tool. A leaf agent resolves to no peers and simply gets its own tools.

Neither slot does both jobs, which is why the choice is derived rather than
uniform: an ``AgentTool`` swallows a suspension, and ``transfer_to_agent`` ends
the caller's invocation so it can never use what the peer returned. See
``app/cluster/resolver.py`` for the measurements behind both halves.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from google.adk.agents import LlmAgent
from google.adk.tools.base_tool import BaseTool

from app.agents.reporting import (
    attach_structured_results,
    restate_structured_results,
)

if TYPE_CHECKING:
    from app.cluster.resolver import AgentResolver
    from app.shared.config import Models

# Valid model tiers an agent can request (keys of the shared Models bundle).
TIERS = ("fast", "balanced", "capable")


@dataclass(frozen=True)
class AgentSpec:
    """Declarative definition of an agent.

    The same spec shape describes every agent in the cluster — a coordinating
    agent is just one whose ``peers`` is non-empty.

    Attributes:
        name: Logical name (also the Kubernetes Service name and the value of
            ``AGENT_NAME`` for this agent's pods). Must be a single word valid as
            both a Python identifier and a DNS label (no hyphens/underscores).
        description: One-line capability summary; published in the agent card so
            other agents know when to delegate here.
        instruction: The system instruction for the agent's LLM.
        tier: Which shared model tier to use (``fast``/``balanced``/``capable``).
        tools: Tools the agent can invoke: plain callables, or ``BaseTool``
            instances when a tool needs ADK-level configuration.
        peers: Default peer names this agent can delegate to over A2A. Resolved
            to in-cluster URLs by ``app/cluster/config.py``, wired as a
            sub-agent or a tool by ``app/cluster/resolver.py``, and overridable
            via the ``A2A_PEERS`` environment variable. Empty for leaf agents.
    """

    name: str
    description: str
    instruction: str
    tier: str
    tools: tuple[Callable[..., object] | BaseTool, ...] = ()
    peers: tuple[str, ...] = field(default_factory=tuple)


def build_agent(spec: AgentSpec, models: Models, resolver: AgentResolver) -> LlmAgent:
    """Build the root agent for any agent spec.

    This is the single construction path for every agent: an ``LlmAgent`` with
    its own tools, plus one tool per configured peer (none for a leaf).

    Args:
        spec: The agent definition.
        models: The shared model bundle (resolved via dependency injection).
        resolver: The resolver that wires configured peers into the slot each
            one requires -- a sub-agent if it can suspend, a tool otherwise.

    Returns:
        The configured ``LlmAgent``.

    Raises:
        ValueError: If the spec names an unknown model tier.
    """
    if spec.tier not in TIERS:
        raise ValueError(
            f"Agent {spec.name!r} has unknown tier {spec.tier!r}; "
            f"expected one of {TIERS}."
        )
    model = {
        "fast": models.fast,
        "balanced": models.balanced,
        "capable": models.capable,
    }[spec.tier]
    return LlmAgent(
        name=spec.name,
        model=model,
        description=spec.description,
        instruction=spec.instruction,
        # Uniform for every agent: its own tools, plus whatever peers the
        # cluster config resolved. Which slot a peer lands in is NOT a style
        # choice -- a peer that can suspend must be a sub-agent or its
        # authorization request is swallowed, and a peer whose answer this
        # agent needs must be a tool or the invocation ends at the handoff.
        # The resolver decides; see app/cluster/resolver.py.
        tools=[*spec.tools, *resolver.resolve_tools()],
        sub_agents=list(resolver.resolve_sub_agents()),
        # Guarantee a proposal survives the A2A text boundary instead of being
        # paraphrased into uselessness by the model. The after-model hook folds
        # the JSON into the model's own reply, so the turn stays one message;
        # the after-agent hook is the fallback for a turn that ends without the
        # model speaking, and emits only what the reply does not already carry.
        # Both are no-ops for a turn that proposed nothing -- see
        # app/agents/reporting.py for the measurement that made this necessary.
        after_model_callback=attach_structured_results,
        after_agent_callback=restate_structured_results,
    )
