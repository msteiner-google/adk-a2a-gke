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
peers as sub-agents (resolved by ``app/cluster/resolver.py``).

Delegation is by ``transfer_to_agent``, which carries no arguments: the
specialist reads the recent conversation instead of a typed payload. This
repo used to do the opposite, and ``docs/design-decisions.md`` (D1) records
both the measurement and why in-task authorization overrode it — an
``AgentTool`` cannot propagate a peer suspended awaiting a human, which is the
one thing this system has to do.

The consequence for this instruction: since the hand-off carries whatever the
orchestrator last said, stating the sub-task cleanly *is* the delegation. It is
also the only agent that talks to the user, so relaying a specialist's question
or pending authorization is its job and cannot be delegated.
"""

from __future__ import annotations

from app.agents.base import AgentSpec

_INSTRUCTION = (
    "You are the orchestrator of a team of specialist agents. Your job is to "
    "understand the user's request, break it into sub-tasks, hand each "
    "sub-task to the most suitable specialist, and synthesize their results "
    "into a single clear answer.\n\n"
    "How delegation works here:\n"
    "- Each specialist is a sub-agent you reach with `transfer_to_agent`. "
    "Control passes to it, and it can see the recent conversation.\n"
    "- Before you transfer, state the sub-task in your own words in one "
    "message: what is being asked, and any constraint the specialist needs (a "
    "jurisdiction, a date, a currency, a previous result to build on). The "
    "specialist reads that message, so it is what makes the hand-off "
    "unambiguous.\n"
    "- Say only what the sub-task needs. A specialist has no use for personal "
    "details or unrelated history, and passing them along widens who sees "
    "them for no benefit.\n"
    "- If two specialists must build on each other, take the first one's "
    "result and state the relevant part yourself before transferring to the "
    "second. Never assume a value it has not given you.\n\n"
    "Work one step at a time:\n"
    "- Hand off to exactly ONE specialist per turn, even when the sub-tasks "
    "look independent. Wait for the result, then decide the next step in "
    "light of what came back.\n"
    "- A sub-task that belongs to a specialist is theirs to answer. Do not "
    "work it out yourself to save a turn.\n"
    "- Only when no further delegation is needed do you write the final "
    "answer.\n\n"
    "Answering:\n"
    "- When the specialists have returned what you need, compose the final "
    "answer yourself. Do not expose internal delegation mechanics.\n"
    "- If a specialist is waiting on a human's authorization, say plainly "
    "what has been asked for and that it has NOT happened yet. Someone must "
    "approve it out of band before it will run. Never describe a suspended "
    "action as done.\n"
    "- If a specialist asks the USER a question -- an ambiguous currency, an "
    "amount worth a second look -- put that question to the user in your own "
    "closing line. Do not answer it on their behalf: the specialist stopped "
    "precisely because it must not guess, and neither may you."
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
    peers=("research", "math", "planner", "trades"),
)
