"""The uniform agent model: one spec, one builder for *every* agent.

In this variant every agent is the same kind of thing — an ADK ``LlmAgent``
described by a declarative :class:`AgentSpec` and constructed by the single
:func:`build_agent` function. There is no special "orchestrator" class: an agent
that coordinates others is simply one whose spec declares ``peers`` (and usually
a planner instruction + the capable model tier). Whether an agent delegates is
therefore *data*, not a separate code path.

Peers are attached uniformly: :func:`build_agent` asks the injected
``AgentResolver`` for whatever peers the cluster configuration resolved for this
process (the selected agent's declared ``peers`` by default, overridable via the
``A2A_PEERS`` environment variable — see ``app/cluster/config.py``). A leaf agent
resolves to no peers and simply gets an empty ``sub_agents`` list.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from google.adk.agents import LlmAgent

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
        tools: Tool callables the agent can invoke.
        peers: Default peer names this agent can delegate to over A2A. Resolved
            to in-cluster URLs by ``app/cluster/config.py`` and overridable via
            the ``A2A_PEERS`` environment variable. Empty for leaf agents.
    """

    name: str
    description: str
    instruction: str
    tier: str
    tools: tuple[Callable[..., object], ...] = ()
    peers: tuple[str, ...] = field(default_factory=tuple)


def build_agent(spec: AgentSpec, models: Models, resolver: AgentResolver) -> LlmAgent:
    """Build the ``LlmAgent`` for any agent spec.

    This is the single construction path for every agent. Peers (if any) are
    attached as ``RemoteA2aAgent`` children via the resolver; a leaf agent
    resolves to an empty list and simply has no sub-agents.

    Args:
        spec: The agent definition.
        models: The shared model bundle (resolved via dependency injection).
        resolver: The resolver that turns the configured peers into
            ``RemoteA2aAgent`` children over A2A.

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
        tools=list(spec.tools),
        # Uniform for every agent: attach whatever peers the cluster config
        # resolved for this process (empty for a leaf agent).
        sub_agents=list(resolver.resolve_all()),
    )
