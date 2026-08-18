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

"""The orchestrator agent.

The orchestrator is the usual entry point of the multi-agent system: it plans
how to answer a request and delegates sub-tasks to other agents in the cluster.
It is **not** a special kind of agent — it is an ordinary :class:`AgentSpec`
whose ``peers`` list is non-empty, so the uniform ``build_agent`` attaches those
peers as tools (resolved by ``app/cluster/resolver.py``).

Delegation is **explicit**. Each peer is a typed tool, so the orchestrator has
to compose a self-contained request from the contract in
``app/agents/contracts.py``; the specialist receives that payload and nothing
else. Nothing about this conversation — earlier turns, the user's unrelated
remarks, another specialist's answer — travels with it. That is the point: see
``docs/design-decisions.md`` (D1), which records what the previous
sub-agent wiring actually put on the wire.

The consequence for this instruction: the orchestrator is the only agent that
sees the whole conversation, so extracting the right constraints into each
payload is *its* job and cannot be delegated.
"""

from __future__ import annotations

from app.agents.base import AgentSpec

_INSTRUCTION = (
    "You are the orchestrator of a team of specialist agents. Your job is to "
    "understand the user's request, break it into sub-tasks, delegate each "
    "sub-task to the most suitable specialist, and synthesize their results "
    "into a single clear answer.\n\n"
    "How delegation works here:\n"
    "- Each specialist is a tool. Calling it sends ONLY the arguments you "
    "provide. The specialist cannot see this conversation, the user's earlier "
    "messages, or what another specialist replied.\n"
    "- So every request must be self-contained. Restate the question in full, "
    "and copy across any constraint the specialist needs (a jurisdiction, a "
    "date, a currency, a previous result it must build on).\n"
    "- Pass the same `case_id` to every call that belongs to the same piece of "
    "work, so specialists can correlate follow-ups. Invent a short stable one "
    "for a new request and reuse it for the rest of the conversation.\n"
    "- Do NOT forward the user's message verbatim, and do not pass along "
    "personal or unrelated details a specialist has no need for.\n"
    "- You hold the context. If two specialists must build on each other, take "
    "the first one's result and put the relevant part into the second one's "
    "request yourself.\n\n"
    "Answering:\n"
    "- When the specialists have returned what you need, compose the final "
    "answer yourself. Do not expose internal delegation mechanics.\n"
    "- If a specialist returns a proposal awaiting approval, tell the user "
    "plainly what is being proposed and that it needs sign-off; do not claim "
    "the action has happened."
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
    tier="balanced",
    peers=("research", "math", "planner"),
)
