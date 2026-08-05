"""The planner agent's spec: a graph-rooted agent.

Unlike every other agent here, this one is not an ``LlmAgent``. Its spec carries
a ``root_node``, so ``build_agent`` serves the graph in
``app/agents/planner/workflow.py`` directly. It is still an ordinary cluster
member: one registry entry, one Deployment/Service, its own agent card, reachable
over A2A exactly like the others.

``instruction``/``tier``/``tools`` are unused for a graph agent (there is no model
to instruct), and it declares no peers — a graph has no ``sub_agents``, so it can
be delegated *to* but cannot delegate onwards.
"""

from __future__ import annotations

from app.agents.base import AgentSpec
from app.agents.planner.workflow import planner_workflow

SPEC = AgentSpec(
    name="planner",
    description=(
        "Drafts a step-by-step plan, has a human review it, and returns the "
        "revised plan. Use when the user wants to approve or amend a plan "
        "before it is finalised."
    ),
    # Unused for a graph agent; kept non-empty so the agent card reads sensibly
    # and so the spec shape stays uniform across the registry.
    instruction="",
    tier="fast",
    root_node=planner_workflow,
)
