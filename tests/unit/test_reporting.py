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

"""Guards for the callback that makes structured results survive A2A.

This is the one piece of machinery in the approval flow that is not obvious, so
it is worth stating what breaks without it: in the first live two-process run
the specialist replied *"I can propose to publish '391.0'…"* — accurate prose,
no structure — and the orchestrator opened no case while telling the user it had
published the result. The callback removes the model from that hop.
"""

import json
from types import SimpleNamespace
from typing import Any, cast

from google.adk.agents.callback_context import CallbackContext
from google.adk.events.event import Event
from google.genai import types

from app.agents.contracts import APPROVAL_REQUIRED
from app.agents.reporting import RESULT_HEADER, restate_structured_results

INVOCATION = "inv-1"
AGENT = "math"


def _tool_event(name: str, response: Any) -> Event:
    return Event(
        author=AGENT,
        invocation_id=INVOCATION,
        content=types.Content(
            role="model",
            parts=[
                types.Part(
                    function_response=types.FunctionResponse(
                        id="c1", name=name, response=response
                    )
                )
            ],
        ),
    )


def _text_event(
    text: str, *, author: str = AGENT, invocation: str = INVOCATION
) -> Event:
    return Event(
        author=author,
        invocation_id=invocation,
        content=types.Content(role="model", parts=[types.Part(text=text)]),
    )


def _context(events: list[Event]) -> CallbackContext:
    """A CallbackContext just real enough for the callback's private reach."""
    return cast(
        CallbackContext,
        SimpleNamespace(
            _invocation_context=SimpleNamespace(
                invocation_id=INVOCATION,
                agent=SimpleNamespace(name=AGENT),
                session=SimpleNamespace(events=events),
            )
        ),
    )


_PROPOSAL = {
    "status": APPROVAL_REQUIRED,
    "action": "publish_result",
    "proposal": {"action": "publish_result", "value": "391.0", "label": "q3"},
    "summary": "Publish '391.0' under label 'q3'.",
}


def test_a_turn_with_no_structured_result_is_left_alone():
    # The common case. The callback must not rewrite ordinary replies.
    content = restate_structured_results(
        _context([_tool_event("calculate", {"result": "391.0"}), _text_event("391")])
    )
    assert content is None


def test_a_proposal_is_restated_verbatim():
    content = restate_structured_results(
        _context(
            [
                _tool_event("publish_result", _PROPOSAL),
                _text_event("I can propose publishing '391.0'. Nothing published yet."),
            ]
        )
    )
    assert content is not None
    body = "".join(p.text or "" for p in content.parts or [])
    assert RESULT_HEADER in body
    # The machine-readable copy must be present and parseable, whatever the
    # model said above it.
    payload = json.loads(body.split(RESULT_HEADER)[1].strip())
    assert payload["proposal"]["value"] == "391.0"


def test_the_models_own_wording_is_preserved():
    spoken = "I can propose publishing '391.0'. Nothing has been published yet."
    content = restate_structured_results(
        _context([_tool_event("publish_result", _PROPOSAL), _text_event(spoken)])
    )
    assert content is not None
    body = "".join(p.text or "" for p in content.parts or [])
    assert body.startswith(spoken)


def test_an_execution_result_is_restated_too():
    # The return leg. Without this the caller cannot confirm the action ran and
    # correctly reports approved_not_confirmed forever.
    published = {
        "status": "published",
        "action": "publish_result",
        "value": "391.0",
        "label": "q3",
        "approved_by": "compliance@bnp",
    }
    content = restate_structured_results(
        _context([_tool_event("publish_result", published), _text_event("Published.")])
    )
    assert content is not None
    body = "".join(p.text or "" for p in content.parts or [])
    assert json.loads(body.split(RESULT_HEADER)[1].strip())["value"] == "391.0"


def test_an_adk_wrapped_result_is_unwrapped():
    # ADK sometimes hands back {"result": <the dict>}; both shapes must work.
    content = restate_structured_results(
        _context([_tool_event("publish_result", {"result": _PROPOSAL})])
    )
    assert content is not None


def test_results_from_other_invocations_are_ignored():
    # A session accumulates turns. Only this invocation's results may be
    # restated, or an old proposal would be re-reported and re-opened.
    stale = _tool_event("publish_result", _PROPOSAL)
    stale.invocation_id = "inv-0"
    assert restate_structured_results(_context([stale, _text_event("hello")])) is None


def test_the_same_result_is_not_restated_twice():
    content = restate_structured_results(
        _context(
            [
                _tool_event("publish_result", _PROPOSAL),
                _tool_event("publish_result", _PROPOSAL),
            ]
        )
    )
    assert content is not None
    body = "".join(p.text or "" for p in content.parts or [])
    assert body.count('"summary"') == 1
