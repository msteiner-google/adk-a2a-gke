"""The uniform agent model: one spec, one builder for *every* agent.

Every agent is described by a declarative :class:`AgentSpec` and constructed by
the single :func:`build_agent` function. There is no special "orchestrator"
class: an agent that coordinates others is simply one whose spec declares
``peers`` (and usually a planner instruction + the capable model tier). Whether
an agent delegates is therefore *data*, not a separate code path.

**Most agents are an ``LlmAgent``; a spec may instead supply a graph.** Setting
``root_node`` makes the agent a deterministic ADK ``Workflow`` (or any other
``BaseNode``) rather than a model-driven agent — used where the control flow is
fixed and a human step belongs *in* the flow rather than in a model's judgement
(see ``app/agents/planner``). Both kinds are still one registry entry, one
builder, and one Deployment; what differs is what the spec declares, not how the
agent is wired, served, or reached over A2A.

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
from google.adk.tools.base_tool import BaseTool
from google.adk.workflow import Workflow

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
            instances when a tool needs ADK-level configuration (e.g. a
            ``FunctionTool`` declaring ``require_confirmation``).
        peers: Default peer names this agent can delegate to over A2A. Resolved
            to in-cluster URLs by ``app/cluster/config.py`` and overridable via
            the ``A2A_PEERS`` environment variable. Empty for leaf agents.
        root_node: A prebuilt graph (an ADK ``Workflow``) to serve *instead of*
            an ``LlmAgent``. When set, ``instruction``/``tier``/``tools`` are
            unused and the agent must be a leaf: a graph has no ``sub_agents``,
            so it cannot delegate over A2A. It can still be delegated *to*.
    """

    name: str
    description: str
    instruction: str
    tier: str
    tools: tuple[Callable[..., object] | BaseTool, ...] = ()
    peers: tuple[str, ...] = field(default_factory=tuple)
    root_node: Workflow | None = None


def build_agent(
    spec: AgentSpec, models: Models, resolver: AgentResolver
) -> LlmAgent | Workflow:
    """Build the root node for any agent spec.

    This is the single construction path for every agent. A spec carrying a
    ``root_node`` is served as that graph; otherwise an ``LlmAgent`` is built and
    whatever peers the cluster config resolved are attached as ``RemoteA2aAgent``
    children (a leaf resolves to an empty list).

    Args:
        spec: The agent definition.
        models: The shared model bundle (resolved via dependency injection).
        resolver: The resolver that turns the configured peers into
            ``RemoteA2aAgent`` children over A2A.

    Returns:
        The configured ``LlmAgent``, or the spec's graph when it declares one.

    Raises:
        ValueError: If the spec names an unknown model tier, or declares both a
            ``root_node`` and peers.
    """
    if spec.root_node is not None:
        # A graph cannot hold RemoteA2aAgent children, so peers would be
        # silently dropped -- refuse instead, at import time.
        if resolver.resolve_all():
            raise ValueError(
                f"Agent {spec.name!r} declares a root_node (a graph) and peers; "
                "a graph agent cannot delegate over A2A. Remove the peers, or "
                "put the graph behind an LlmAgent."
            )
        # Keep the spec the single source of truth for the agent card: a bare
        # Workflow has an empty description, and the card would advertise
        # "An ADK Agent" -- which is what PEERS read to decide whether to
        # delegate here, so an empty one quietly makes the agent unreachable in
        # practice.
        if not spec.root_node.description:
            spec.root_node.description = spec.description
        return spec.root_node
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
