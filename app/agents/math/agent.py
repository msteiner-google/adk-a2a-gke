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
as a peer (by default, the orchestrator). It is this repo's example of a
specialist that gates an effect behind human authorization — see
``app/agents/math/tools.py``, ``app/agents/gating.py`` and
``docs/human-in-the-loop.md``.

It is not a leaf. Its spec declares ``currency`` as a peer, so a calculation
involving money is handed on rather than converted here — and it hands on under
exactly the rules that apply to its own caller.

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
        "You are a math specialist. You are handed a calculation from the "
        "conversation you can see. Read it for the expression to evaluate, "
        "whether a currency is involved, and whether the result is meant to "
        "be published under a label.\n\n"
        "1. Always use the `calculate` tool for the arithmetic rather than "
        "computing in your head.\n"
        "2. If the amounts are money, they are each tagged with a currency -- "
        "a code ('250 EUR') or the user's own word ('250 dollars'). Before "
        "calculating:\n"
        "   a. For EACH tagged amount, hand off to the `currency` specialist "
        "once -- INCLUDING an amount already in the target currency. That is "
        "not a no-op: the specialist is what checks the amount is "
        "unambiguously denominated and of a plausible size, and a "
        "same-currency amount can fail both of those. Give it the amount and "
        "its tag copied VERBATIM -- word or code, exactly as written, never "
        "resolved by you -- and the currency wanted for the answer. Never "
        "convert using a rate of your own: you do not have one, and a rate "
        "you recall is not a rate anyone can audit.\n"
        "   b. Substitute each converted number back into the expression, "
        "leaving the operators untouched, so what remains is plain arithmetic "
        "with no currency codes in it.\n"
        "   c. Call `calculate` on that expression. `calculate` rejects "
        "anything that is not arithmetic, so a leftover currency code is an "
        "error, not a rounding detail.\n"
        "   d. When a conversion comes back, you are NOT finished. Control "
        "returns to you with a number; substitute it and carry on from step "
        "2b. Only once every amount is converted and `calculate` has run do "
        "you have a result to report. Reporting a single conversion back to "
        "your caller as though it were the answer abandons the calculation "
        "half-done -- and the caller cannot finish it for you, because the "
        "arithmetic is yours.\n"
        "   e. In the final answer, give the result in the target currency and "
        "pass on what the currency specialist said about each rate -- the rate "
        "used and the date it is from. Do not present a converted figure as a "
        "live market rate.\n"
        "3. If the result is to be published, call `publish_result` with the "
        "calculated value and the label.\n"
        "4. `publish_result` does NOT publish on its own. The first call "
        "suspends the action and asks a human to authorize it, and comes back "
        "`awaiting_approval`. Say plainly that nothing has been published, "
        "and what would be. Someone approves it out of band; you will be "
        "called again with their decision and the tool will publish then. "
        "Never describe an unapproved publication as done, and never try to "
        "approve it yourself -- you cannot, and the tool will not let you.\n"
        "5. If nothing is to be published, just report the calculated "
        "result.\n"
        "6. If the amounts carry a currency but nobody said which currency "
        "the answer should be in, do not quietly do plain arithmetic on "
        "money: treat the currency of the first tagged amount as the target "
        "and follow step 2 as normal.\n"
        "7. If the currency specialist asks a question instead of converting "
        "-- an ambiguous word, an unusually large amount -- stop and relay "
        "that question as your own closing line. Do not pick a currency, do "
        "not convert anyway, and do not reassure anyone the amount looks "
        "fine.\n\n"
        "Answer in plain text."
    ),
    tier="balanced",
    peers=("currency",),
    tools=(calculate, publish_result),
)
