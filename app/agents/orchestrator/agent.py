"""The orchestrator agent.

The orchestrator is the usual entry point of the multi-agent system: it plans
how to answer a request and delegates sub-tasks to other agents in the cluster.
It is **not** a special kind of agent — it is an ordinary :class:`AgentSpec`
whose ``peers`` list is non-empty, so the uniform ``build_agent`` attaches those
peers as ``RemoteA2aAgent`` children (resolved by ``app/cluster/resolver.py``).

Delegation uses ADK's built-in agent transfer: the peers are attached as
``sub_agents``, so the planner LLM can route to them by name, and their
descriptions (fetched from each peer's agent card) tell it when to do so. The
invocation context — including shared session state written via ``remember`` —
propagates across the A2A hop to the chosen agent.
"""

from __future__ import annotations

from app.agents.base import AgentSpec
from app.agents.common import recall, remember

_INSTRUCTION = (
    "You are the orchestrator of a team of specialist agents. Your job is to "
    "understand the user's request, break it into sub-tasks, and delegate each "
    "sub-task to the most suitable agent, then synthesize their results into a "
    "single clear answer.\n\n"
    "Guidelines:\n"
    "- Inspect the available agents and their descriptions; transfer a sub-task "
    "to the one whose expertise matches.\n"
    "- Break complex requests into steps and delegate them in a sensible order.\n"
    "- Use `remember` to record facts that later steps or agents need, and "
    "`recall` to retrieve them; this shared context travels with the request.\n"
    "- When the agents have returned what you need, compose the final answer "
    "yourself. Do not expose internal delegation mechanics to the user.\n"
    "- When the user asks you to draft a plan for their review, transfer to the "
    "`planner` agent, which drafts it, collects the human's feedback and returns "
    "the revised plan."
)

# The orchestrator is just an agent whose peers are non-empty. The peers are
# DEFAULTS: an operator can override them at deploy time via A2A_PEERS.
SPEC = AgentSpec(
    name="orchestrator",
    description=(
        "Plans and coordinates a team of specialist agents to answer complex, "
        "multi-step requests."
    ),
    instruction=_INSTRUCTION,
    tier="capable",
    tools=(remember, recall),
    peers=("research", "math", "planner"),
)
