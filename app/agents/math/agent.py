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

"""The math agent.

A focused leaf agent (no peers) that performs precise arithmetic. It runs as its
own Deployment/Service in the cluster and is reached over A2A by any agent that
lists it as a peer (by default, the orchestrator).

It is delegated to **functionally**: a caller sends a
:class:`~app.agents.contracts.MathRequest` and nothing else. It is also this
repo's example of a specialist that gates an effect behind human approval — see
``app/agents/math/tools.py`` and ``docs/human-in-the-loop.md``.
"""

from __future__ import annotations

from app.agents.base import AgentSpec
from app.agents.math.tools import calculate, publish_result

SPEC = AgentSpec(
    name="math",
    description=(
        "Performs precise arithmetic and quantitative reasoning, and can "
        "publish a result once a human has approved it."
    ),
    instruction=(
        "You are a math specialist. You receive a JSON request with an "
        "`expression`, a `case_id`, and optionally `publish_as`, `approved_by` "
        "and `decision_note`.\n\n"
        "1. Always use the `calculate` tool for the arithmetic rather than "
        "computing in your head.\n"
        "2. If `publish_as` is set, call `publish_result` with the calculated "
        "value, that label, and `approved_by`/`decision_note` exactly as they "
        "appear in the request (pass empty strings if they are absent).\n"
        "3. `publish_result` decides what happens: with no approver it returns "
        "a proposal and publishes nothing, so say plainly that nothing has been "
        "published and what would be. With an approver it publishes, so report "
        "that it is done.\n"
        "4. If `publish_as` is absent, just report the calculated result.\n\n"
        "The request is all the context you have: you cannot see the "
        "conversation it came from. Return your answer as plain text."
    ),
    tier="balanced",
    tools=(calculate, publish_result),
)
