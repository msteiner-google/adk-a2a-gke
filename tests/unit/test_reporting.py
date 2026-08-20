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

"""Guards for the callbacks that make structured results survive A2A.

This is the one piece of machinery in the approval flow that is not obvious, so
it is worth stating what breaks without it: in the first live two-process run
the specialist replied *"I can propose to publish '391.0'…"* — accurate prose,
no structure — and the orchestrator opened no case while telling the user it had
published the result. The callbacks remove the model from that hop.

There are two of them, and the last section of this file is why: the after-model
hook folds the JSON into the model's own reply so a turn stays one message, and
the after-agent hook only fills a gap that hook left. While the after-agent hook
restated unconditionally — repeating the model's wording above the JSON — every
HITL turn rendered twice in the ADK web UI, once per event.
"""

import json
from types import SimpleNamespace
from typing import Any, cast

from google.adk.agents.callback_context import CallbackContext
from google.adk.events.event import Event
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from app.agents.contracts import (
    APPROVAL_REQUIRED,
    EXECUTED,
    NEEDS_CONFIRMATION,
    NEEDS_INPUT,
    PUBLISHED,
)
from app.agents.reporting import (
    AUDITED_STATUSES,
    RESULT_HEADER,
    attach_structured_results,
    restate_structured_results,
)

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


def _model_reply(text: str = "", *, partial: bool | None = None) -> LlmResponse:
    """A final, non-streamed model response carrying plain text."""
    return LlmResponse(
        content=types.Content(role="model", parts=[types.Part(text=text)]),
        partial=partial,
    )


def _text_of(response: LlmResponse) -> str:
    parts = response.content.parts if response.content else None
    return "".join(p.text or "" for p in parts or [])


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
    # The prose and the JSON travel together, in the model's own reply -- so the
    # caller's AgentTool, which sees only the LAST content, gets both.
    spoken = "I can propose publishing '391.0'. Nothing has been published yet."
    altered = attach_structured_results(
        _context([_tool_event("publish_result", _PROPOSAL)]),
        _model_reply(spoken),
    )
    assert altered is not None
    body = _text_of(altered)
    assert body.startswith(spoken)
    assert json.loads(body.split(RESULT_HEADER)[1].strip())["proposal"]["value"] == (
        "391.0"
    )


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


# --- Surviving more than one hop ----------------------------------------------
#
# A peer reached through PeerTool answers as TEXT, so on the caller its reply is
# a *string* tool result. Without scanning inside those strings, a question or a
# proposal raised two levels down (orchestrator -> math -> currency) is invisible
# here, and reaches the user only if the middle agent's model chooses to repeat
# it -- the exact dependency this callback exists to remove.

_QUESTION = {
    "status": NEEDS_INPUT,
    "reason": "ambiguous_currency",
    "field": "from_currency",
    "term": "dollars",
    "candidates": ["USD", "CAD", "AUD", "NZD", "SGD", "HKD"],
    "question": "Which currency does 'dollars' mean here?",
}


def _peer_reply(payload: dict[str, Any], prose: str = "") -> str:
    """The peer's own text. ADK delivers it as {"result": <this string>}."""
    return f"{prose}\n\nStructured result(s):\n{json.dumps(payload, sort_keys=True)}"


def test_a_question_from_a_peer_two_hops_down_is_restated():
    content = restate_structured_results(
        _context(
            [
                _tool_event("currency", {"result": _peer_reply(_QUESTION, "check")}),
                _text_event("The currency specialist needs to know which dollar."),
            ]
        )
    )
    assert content is not None
    body = "".join(p.text or "" for p in content.parts or [])
    assert RESULT_HEADER in body
    # Verbatim, so the orchestrator's scan finds the same object the currency
    # agent emitted -- candidates included.
    assert json.dumps(_QUESTION, sort_keys=True) in body


def test_a_peer_reply_wrapped_by_adk_is_scanned_too():
    # ADK routinely wraps a tool's return value as {"result": <string>}.
    content = restate_structured_results(
        _context([_tool_event("currency", {"result": _peer_reply(_QUESTION)})])
    )
    assert content is not None
    body = "".join(p.text or "" for p in content.parts or [])
    assert json.dumps(_QUESTION, sort_keys=True) in body


def test_a_confirmation_request_is_restated():
    payload = {
        "status": NEEDS_CONFIRMATION,
        "reason": "large_amount",
        "question": "That is over the threshold. Confirm?",
    }
    content = restate_structured_results(
        _context([_tool_event("currency", {"result": _peer_reply(payload)})])
    )
    assert content is not None
    body = "".join(p.text or "" for p in content.parts or [])
    assert json.dumps(payload, sort_keys=True) in body


def test_a_gated_read_is_audited_like_a_gated_write():
    # Regression: AUDITED_STATUSES was {approval_required, published} when the
    # trades agent landed, so `executed` was silently NOT restated. It survived
    # only because the model chose to repeat it.
    assert EXECUTED in AUDITED_STATUSES
    assert PUBLISHED in AUDITED_STATUSES
    content = restate_structured_results(
        _context([_tool_event("run_trade_query", {"status": EXECUTED, "rows": []})])
    )
    assert content is not None


def test_ordinary_peer_prose_is_still_left_alone():
    # The scanner must not turn every peer reply into a restatement.
    content = restate_structured_results(
        _context([_tool_event("currency", {"result": "250 EUR is 272.50 USD."})])
    )
    assert content is None


# --- One reply, not two -------------------------------------------------------
#
# ADK appends whatever an after-agent callback returns as an EXTRA event, so
# restating there unconditionally showed the user the same answer twice on every
# HITL turn. The structure is folded into the model's own reply instead, and the
# after-agent hook emits only what that reply does not already carry.


def test_the_after_agent_hook_adds_nothing_once_the_reply_carries_the_json():
    # The regression. The model's event here is what attach_structured_results
    # produced, so there is nothing left to append -- and nothing to duplicate.
    tool_event = _tool_event("publish_result", _PROPOSAL)
    reply = attach_structured_results(_context([tool_event]), _model_reply("Proposed."))
    assert reply is not None
    events = [tool_event, _text_event(_text_of(reply))]
    assert restate_structured_results(_context(events)) is None


def test_a_silent_turn_still_gets_its_results_restated():
    # The gap the after-agent hook exists for: no model reply to fold into, so
    # the structure would otherwise never leave this agent.
    content = restate_structured_results(_context([_tool_event("x", _PROPOSAL)]))
    assert content is not None
    assert RESULT_HEADER in "".join(p.text or "" for p in content.parts or [])


def test_a_streamed_chunk_is_left_alone():
    # Each chunk is re-delivered in the aggregated response that follows;
    # appending here would emit the block once per chunk.
    assert (
        attach_structured_results(
            _context([_tool_event("publish_result", _PROPOSAL)]),
            _model_reply("Prop", partial=True),
        )
        is None
    )


def test_a_tool_call_response_is_left_alone():
    # Mid-turn: the results are not all in, and the text beside a function call
    # is not the agent's answer.
    response = LlmResponse(
        content=types.Content(
            role="model",
            parts=[
                types.Part(text="Let me publish that."),
                types.Part(
                    function_call=types.FunctionCall(name="publish_result", args={})
                ),
            ],
        )
    )
    assert (
        attach_structured_results(
            _context([_tool_event("publish_result", _PROPOSAL)]), response
        )
        is None
    )


def test_an_ordinary_reply_is_returned_unchanged():
    # The overwhelming majority of turns. Returning None leaves ADK's own
    # response untouched.
    assert (
        attach_structured_results(
            _context([_tool_event("calculate", {"result": "391.0"})]),
            _model_reply("391."),
        )
        is None
    )


def test_a_question_from_two_hops_down_reaches_the_reply_itself():
    # The HITL case the user sees: one message, prose then the question's JSON.
    altered = attach_structured_results(
        _context([_tool_event("currency", {"result": _peer_reply(_QUESTION, "hm")})]),
        _model_reply("Which dollars did you mean?"),
    )
    assert altered is not None
    assert json.dumps(_QUESTION, sort_keys=True) in _text_of(altered)
