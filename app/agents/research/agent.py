"""The research agent.

A focused leaf agent (no peers) that answers factual questions. It runs as its
own Deployment/Service in the cluster and is reached over A2A by any agent that
lists it as a peer (by default, the orchestrator).
"""

from __future__ import annotations

from app.agents.base import AgentSpec
from app.agents.common import recall, remember
from app.agents.research.tools import web_search

SPEC = AgentSpec(
    name="research",
    description="Researches topics and answers factual questions using search.",
    instruction=(
        "You are a research specialist. Answer factual questions concisely and "
        "cite what you looked up. Use the `web_search` tool to gather "
        "information, and use `remember`/`recall` to carry shared context (such "
        "as the user's topic) across steps."
    ),
    tier="balanced",
    tools=(web_search, remember, recall),
)
