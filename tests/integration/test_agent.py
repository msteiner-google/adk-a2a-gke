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

from google.adk.agents import BaseAgent
from google.adk.agents.run_config import (
    RunConfig,
    # ADK 2.7.0 moved StreamingMode into a private module and re-exports it
    # here without an `__all__`, so pyright reads the re-export as private.
    # `run_config` is still the public, documented location -- importing the
    # private module to satisfy the checker would swap a cosmetic warning for
    # a real coupling to an underscore-prefixed module.
    StreamingMode,  # pyright: ignore[reportPrivateImportUsage]
)
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from app.agent import root_agent


def test_agent_stream() -> None:
    """
    Integration test for the agent stream functionality.
    Tests that the agent returns valid streaming responses.
    """

    session_service = InMemorySessionService()

    session = session_service.create_session_sync(user_id="test_user", app_name="test")
    # Every agent is an LlmAgent again -- the graph-rooted planner went with the
    # coroutine HITL design it existed to demonstrate. Runner's `agent=`
    # parameter takes a BaseAgent, so keep this as a cheap guard on what the
    # test actually drives.
    assert isinstance(root_agent, BaseAgent)
    runner = Runner(agent=root_agent, session_service=session_service, app_name="test")

    message = types.Content(
        role="user", parts=[types.Part.from_text(text="Why is the sky blue?")]
    )

    events = list(
        runner.run(
            new_message=message,
            user_id="test_user",
            session_id=session.id,
            run_config=RunConfig(streaming_mode=StreamingMode.SSE),
        )
    )
    assert len(events) > 0, "Expected at least one message"

    has_text_content = False
    for event in events:
        if (
            event.content
            and event.content.parts
            and any(part.text for part in event.content.parts)
        ):
            has_text_content = True
            break
    assert has_text_content, "Expected at least one message with text content"
