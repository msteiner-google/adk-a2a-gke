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

Performs precise arithmetic, and is reached over A2A by any agent that lists it
as a peer (by default, the orchestrator). It is delegated to **functionally**: a
caller sends a :class:`~app.agents.contracts.MathRequest` and nothing else. It
is also this repo's example of a specialist that gates an effect behind human
approval — see ``app/agents/math/tools.py`` and ``docs/human-in-the-loop.md``.

It is no longer a leaf. Its spec declares ``currency`` as a peer, so when a
request carries ``target_currency`` this agent delegates each conversion instead
of applying a rate itself — and it is delegating under exactly the rules that
apply to its own caller. It composes a
:class:`~app.agents.contracts.CurrencyRequest` per amount; the currency
specialist sees that payload and nothing about the sum being computed, let alone
the conversation the orchestrator is holding.

Nothing in ``build_agent`` had to change for that. An agent that coordinates is
one whose spec is non-empty in ``peers``, at any depth (see
``app/agents/base.py``).

**The division of labour is deliberate.** The rate belongs to the specialist
that owns rates; the arithmetic belongs here. Letting this agent multiply by a
rate it recalled would put an unversioned, unattributed number into a financial
answer — which is the failure the split exists to prevent, not a round trip it
would be clever to save.
"""

from __future__ import annotations

from app.agents.base import AgentSpec
from app.agents.math.tools import calculate, publish_result

SPEC = AgentSpec(
    name="math",
    description=(
        "Performs precise arithmetic and quantitative reasoning, converts "
        "between currencies, and can publish a result once a human has "
        "approved it."
    ),
    instruction=(
        "You are a math specialist. You receive a JSON request with an "
        "`expression`, a `case_id`, and optionally `target_currency`, "
        "`publish_as`, `approved_by` and `decision_note`.\n\n"
        "1. Always use the `calculate` tool for the arithmetic rather than "
        "computing in your head.\n"
        "2. If `target_currency` is set, the amounts in `expression` are each "
        "tagged with a currency -- a code ('250 EUR') or the user's own word "
        "('250 dollars'). Before calculating:\n"
        "   a. For EACH tagged amount not already in `target_currency`, call "
        "the `currency` specialist once, with that amount, its tag copied "
        "VERBATIM as `from_currency` -- word or code, exactly as written, "
        "never resolved by you -- `target_currency` as "
        "`to_currency`, `currency_confirmed` from the request as `confirmed`, "
        "and the SAME `case_id` as the request. Never convert "
        "using a rate of your own -- you do not have one, and a rate you "
        "recall is not a rate anyone can audit.\n"
        "   b. Substitute each converted number back into the expression, "
        "leaving the operators untouched, so what remains is plain arithmetic "
        "with no currency codes in it.\n"
        "   c. Call `calculate` on that expression. `calculate` rejects "
        "anything that is not arithmetic, so a leftover currency code is an "
        "error, not a rounding detail.\n"
        "   d. Report the result in `target_currency`, and pass on what the "
        "currency specialist said about the rate -- the rate used and the date "
        "it is from. Do not present a converted figure as a live market rate.\n"
        "3. If `publish_as` is set, call `publish_result` with the calculated "
        "value, that label, and `approved_by`/`decision_note` exactly as they "
        "appear in the request (pass empty strings if they are absent).\n"
        "4. `publish_result` decides what happens: with no approver it returns "
        "a proposal and publishes nothing, so say plainly that nothing has been "
        "published and what would be. With an approver it publishes, so report "
        "that it is done.\n"
        "5. If `publish_as` is absent, just report the calculated result.\n\n"
        "The request is all the context you have: you cannot see the "
        "conversation it came from. Return your answer as plain text."
    ),
    tier="balanced",
    peers=("currency",),
    tools=(calculate, publish_result),
)
