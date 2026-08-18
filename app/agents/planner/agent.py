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

"""The planner agent: drafts a plan for a human to review.

This agent used to be a graph — an ADK ``Workflow`` whose middle node yielded a
``RequestInput`` to pause the whole invocation until a human replied. That was
strategy C of the coroutine-based HITL design, and it went when that design did:
see ``docs/design-decisions.md`` for why, and ``docs/design-decisions.md``
for the evidence that motivated the change.

What replaces it is simpler and framework-neutral: the planner **drafts and
returns**. It holds nothing open, so a review that takes a week costs nothing.
The human step happens where it belongs — in the caller's business workflow,
against a case record — and a revision is an ordinary second call carrying the
reviewer's feedback in the payload.
"""

from __future__ import annotations

from app.agents.base import AgentSpec

SPEC = AgentSpec(
    name="planner",
    description=(
        "Drafts a step-by-step plan for a human to review. Use when the user "
        "wants a plan they can approve or amend before it is acted on."
    ),
    instruction=(
        "You are a planning specialist. You receive a JSON request with an "
        "`objective`, an optional `constraints` field, and a `case_id`.\n\n"
        "- Produce a concise, numbered plan that achieves the objective and "
        "respects every constraint.\n"
        "- Each step must be concrete enough for someone else to execute "
        "without asking you what you meant.\n"
        "- Where a step is risky or irreversible, say so on the step itself, so "
        "a reviewer can see what they are approving.\n"
        "- If the request carries reviewer feedback on an earlier draft, "
        "produce the revised plan and note briefly what changed.\n"
        "- The request is all the context you have: you cannot see the "
        "conversation it came from. Return the plan as plain text."
    ),
    tier="balanced",
)
