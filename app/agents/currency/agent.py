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

"""The currency agent.

A leaf agent (no peers) that converts an amount between currencies. It is the
cluster's example of a **second-tier specialist**: its caller is not the
orchestrator but the ``math`` agent, which delegates the conversion rather than
applying a rate itself.

That shape is worth being explicit about, because it is the thing people assume
this architecture cannot do. Nothing in ``build_agent`` distinguishes a
coordinator from a leaf — an agent that delegates is one whose spec declares
``peers`` — so ``orchestrator -> math -> currency`` needs no new machinery, and
each hop is the same explicit typed request as the first. The rules do not
loosen with depth: this agent sees a
:class:`~app.agents.contracts.CurrencyRequest` and nothing else, and it can no
more see the user's original question than ``math`` can see the conversation the
orchestrator is holding.

The rates are hardcoded; see ``app/agents/currency/tools.py``.
"""

from __future__ import annotations

from app.agents.base import AgentSpec
from app.agents.currency.tools import (
    RATES_AS_OF,
    convert_currency,
    list_supported_currencies,
)

SPEC = AgentSpec(
    name="currency",
    description=(
        "Converts an amount from one currency to another using a fixed, "
        "published reference rate table."
    ),
    instruction=(
        "You are a currency conversion specialist. You receive a JSON request "
        "with an `amount`, a `from_currency`, a `to_currency` and a `case_id`."
        "\n\n"
        "1. Always use the `convert_currency` tool. Never apply a rate from "
        "memory and never do the multiplication yourself -- the tool holds the "
        "only rate table anyone here has agreed on.\n"
        "2. Report the converted amount, the rate that produced it, and the "
        "`as_of` date the tool returns. State plainly that the rate is a fixed "
        f"reference rate frozen on {RATES_AS_OF}, not a live market quote. A "
        "caller that relays your number as a live quote is a worse outcome "
        "than one that cannot convert at all.\n"
        "3. If the tool reports an unsupported currency code, say so and list "
        "the codes it does support (`list_supported_currencies`). Do not "
        "substitute a currency you were not asked for.\n\n"
        "The request is all the context you have: you cannot see the "
        "conversation it came from. Return your answer as plain text."
    ),
    tier="fast",
    tools=(convert_currency, list_supported_currencies),
)
