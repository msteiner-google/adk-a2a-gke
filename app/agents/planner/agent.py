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
