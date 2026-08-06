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

"""The research agent.

A focused leaf agent (no peers) that answers factual questions. It runs as its
own Deployment/Service in the cluster and is reached over A2A by any agent that
lists it as a peer (by default, the orchestrator).
"""

from __future__ import annotations

from app.agents.base import AgentSpec
from app.agents.common import recall, remember
from app.agents.hitl_strategies import request_input
from app.agents.research.tools import web_search

SPEC = AgentSpec(
    name="research",
    description="Researches topics and answers factual questions using search.",
    instruction=(
        "You are a research specialist. Answer factual questions concisely and "
        "cite what you looked up. Use the `web_search` tool to gather "
        "information, and use `remember`/`recall` to carry shared context (such "
        "as the user's topic) across steps. If the request is ambiguous and a "
        "single clarification would change your answer, call "
        "`adk_request_input` to ask the human before searching -- do not guess "
        "and do not ask more than once."
    ),
    tier="balanced",
    tools=(web_search, request_input, remember, recall),
)
