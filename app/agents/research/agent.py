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

A caller reaches it with ``transfer_to_agent``, so what it sees is the recent
conversation rather than a typed payload. There is still no shared state and no
way to ask the caller for more, which is why the instruction tells it to work
with what it was given and say precisely what is missing when something is.
"""

from __future__ import annotations

from app.agents.base import AgentSpec
from app.agents.documents import read_document
from app.agents.research.tools import web_search

SPEC = AgentSpec(
    name="research",
    description=(
        "Researches topics and answers factual questions using search, and can "
        "read source documents passed to it by reference."
    ),
    instruction=(
        "You are a research specialist. You are handed a question from the "
        "conversation you can see.\n\n"
        "- If the conversation carries document references (``gs://`` "
        "pointers), call `read_document` on each one FIRST and base your "
        "answer on what they actually say. They are the primary source; "
        "nobody has read them for you.\n"
        "- Use the `web_search` tool for anything the documents do not cover, "
        "rather than answering from memory.\n"
        "- Answer concisely and cite what you looked up -- name the document "
        "or the search that supports each claim.\n"
        "- Respect any stated constraints exactly.\n"
        "- If something essential is genuinely missing, state precisely what "
        "you would need and answer as far as you can -- do not invent it.\n"
        "- Return the answer as plain text."
    ),
    tier="balanced",
    tools=(web_search, read_document),
)
