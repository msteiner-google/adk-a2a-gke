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

"""Call a remote peer with an explicit payload instead of the conversation.

This is the mechanism behind design decision D1
(``docs/design-decisions.md``). A peer attached as an ADK *sub-agent*
is reached with ``transfer_to_agent``, and ``RemoteA2aAgent`` then rebuilds the
outbound A2A message from the caller's **session events** — every turn since the
peer last replied, with other agents' replies folded in and prefixed
``For context:``. Measured on this repo's own agents, that was **ten message
parts** including the user's phone number and a different specialist's answer,
where the task needed one (``docs/design-decisions.md``, D1).

A peer attached as a *tool* is reached through ``AgentTool``, which runs it
against a fresh in-memory session whose only event is the tool's arguments. The
peer therefore receives precisely the payload the caller composed. That single
change is what makes delegation *functional* — the caller must state what it
wants — and it is what removes context pollution, cross-agent data leakage and
the quadratic token cost of re-sending the transcript to every specialist.

What this class adds on top of ``AgentTool``
--------------------------------------------
``AgentTool`` derives its declaration from the wrapped agent's ``input_schema``,
which only exists on an ``LlmAgent``. A ``RemoteA2aAgent`` has none, so the
declaration degrades to a bare, undocumented ``request`` string — which invites
the caller to fill it with the transcript, undoing what attaching the peer as a
tool just bought. :class:`PeerTool` supplies a schema explicitly instead, so the
calling model sees named parameters and the arguments are validated before they
leave the pod.

**Which schema is a per-peer choice**, and the resolver always passes one: the
peer's declared contract from ``app/agents/contracts.py`` when it has an entry
in ``PAYLOADS``, and otherwise ``resolver.UnknownPeerRequest`` — still free
text, but a correlation id plus one field documented as a self-contained task.
Declaring a contract is therefore optional; see the tiers in
``app/agents/contracts.py`` for when to bother.

Note ADK's ``AgentTool`` docstring recommends ``mode='single_turn'`` on a
sub-agent over direct ``AgentTool`` use. That advice does not apply here:
``mode`` is a field on ``LlmAgent``, not ``BaseAgent``, so it does not exist on
``RemoteA2aAgent``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, override

from google.adk.tools.agent_tool import AgentTool

# ToolContext is imported at RUNTIME, not under TYPE_CHECKING: ADK evaluates
# tool annotations with typing.get_type_hints() when it builds a declaration,
# and `from __future__ import annotations` makes them strings. See the fuller
# note in app/agents/documents.py's sibling modules and AGENTS.md.
from google.adk.tools.tool_context import ToolContext
from google.genai import types

if TYPE_CHECKING:
    from google.adk.agents import BaseAgent
    from pydantic import BaseModel


class PeerTool(AgentTool):
    """A remote peer, called with a typed payload rather than the transcript."""

    def __init__(
        self,
        agent: BaseAgent,
        *,
        payload_schema: type[BaseModel],
        description: str = "",
    ) -> None:
        """Wrap a peer agent behind a typed tool declaration.

        Args:
            agent: The peer, normally a ``RemoteA2aAgent`` pointed at its card.
            payload_schema: The contract this peer accepts (see
                ``app/agents/contracts.py``). Its JSON Schema becomes the tool's
                parameters, and arguments are validated against it before the
                call leaves this pod.
            description: Overrides the peer's description in the declaration.
                Defaults to whatever the wrapped agent carries.
        """
        super().__init__(agent=agent)
        self._payload_schema = payload_schema
        if description:
            self.description = description

    @property
    def payload_schema(self) -> type[BaseModel]:
        """The contract this peer accepts."""
        return self._payload_schema

    @override
    def _get_declaration(self) -> types.FunctionDeclaration:
        """Declare the peer's typed contract to the calling model.

        Returns:
            A declaration whose parameters are the payload schema, so the model
            fills named fields instead of composing a free-text request.
        """
        return types.FunctionDeclaration(
            name=self.name,
            description=self.description,
            # The pydantic JSON Schema is passed through verbatim. This is the
            # SAME representation AgentTool itself produces from an
            # input_schema, and the same one published in the agent card -- so
            # a non-ADK caller reading the card sees an identical contract.
            parameters_json_schema=self._payload_schema.model_json_schema(),
        )

    @override
    async def run_async(
        self, *, args: dict[str, Any], tool_context: ToolContext
    ) -> Any:
        """Validate the payload, then delegate over A2A.

        Validating here rather than at the peer is deliberate: a malformed
        delegation is the caller's bug, and failing in-process gives the model a
        usable error on the same turn instead of a remote 4xx several hops away.

        Args:
            args: Raw arguments the model produced.
            tool_context: Injected by ADK.

        Returns:
            The peer's reply.

        Raises:
            ValueError: If the arguments do not satisfy the peer's contract.
        """
        try:
            payload = self._payload_schema.model_validate(args)
        except Exception as exc:
            raise ValueError(f"Invalid payload for peer {self.name!r}: {exc}") from exc
        # Hand the validated, canonical form to AgentTool, which serialises it
        # to JSON as the single message the peer receives.
        return await super().run_async(
            args=payload.model_dump(mode="json"), tool_context=tool_context
        )
