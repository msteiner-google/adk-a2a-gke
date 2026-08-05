"""The math agent.

A focused leaf agent (no peers) that performs precise arithmetic. It runs as its
own Deployment/Service in the cluster and is reached over A2A by any agent that
lists it as a peer (by default, the orchestrator).
"""

from __future__ import annotations

from app.agents.base import AgentSpec
from app.agents.common import recall, remember
from app.agents.hitl_strategies import publish_result_tool
from app.agents.math.tools import calculate

SPEC = AgentSpec(
    name="math",
    description="Performs precise arithmetic and quantitative reasoning.",
    instruction=(
        "You are a math specialist. Use the `calculate` tool for every "
        "arithmetic step rather than computing in your head, then explain the "
        "result briefly. When the user asks you to publish, record or share a "
        "result, call `publish_result` with it -- that action is reviewed by a "
        "human before it takes effect. Use `remember`/`recall` for shared "
        "context."
    ),
    tier="fast",
    tools=(calculate, publish_result_tool, remember, recall),
)
