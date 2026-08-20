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

"""Shared fixtures for the unit tests.

The only thing here is a stand-in ``ToolContext`` for the human-authorization
gate. Every gated tool reads its decision from that context, so both
``test_two_phase_approval.py`` and ``test_trades.py`` need one; building the
real thing would require a live invocation and a runner, which would make two
otherwise hermetic suites depend on a model.
"""

from __future__ import annotations

from typing import Any

from google.adk.tools.tool_confirmation import ToolConfirmation


class FakeActions:
    """The subset of ``EventActions`` a gated tool touches."""

    def __init__(self) -> None:
        self.skip_summarization = False
        self.requested_tool_confirmations: dict[str, ToolConfirmation] = {}


class FakeToolContext:
    """A stand-in for ``ToolContext`` carrying (or lacking) a decision.

    A gated tool only reads ``tool_confirmation`` and writes
    ``request_confirmation`` / ``actions``, so this covers the whole surface
    the gate uses. Cast it to ``ToolContext`` at the call site — ``ty`` will not
    accept a duck-typed value for a real annotation.
    """

    def __init__(self, confirmation: ToolConfirmation | None = None) -> None:
        self.tool_confirmation = confirmation
        self.actions = FakeActions()
        self.function_call_id = "fc-1"
        self.requested: ToolConfirmation | None = None

    def request_confirmation(
        self, *, hint: str | None = None, payload: Any = None
    ) -> None:
        """Record a request for human authorisation, as ADK would."""
        self.requested = ToolConfirmation(
            hint=hint or "", confirmed=False, payload=payload
        )
        self.actions.requested_tool_confirmations[self.function_call_id] = (
            self.requested
        )
