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
each hop is wired by the same rule as the first. Depth matters for a
different reason now: a question this agent raises has to survive two hops back
to the user, which is what ``app/agents/reporting.py`` exists for.

**This agent gates one of its two conversions.** ``convert_to_crypto`` is
marked ``@gated``, so quoting an amount in BTC or ETH suspends until a human
authorises it, while ordinary fiat conversion stays ungated. That single marker
changes how ``math`` reaches this agent — a peer that can suspend is wired as a
sub-agent, or the authorization request is swallowed (``app/agents/gating.py``,
``app/cluster/resolver.py``) — and it makes ``orchestrator -> math -> currency``
the cluster's first *two-hop* authorization: the suspension has to travel back
up two A2A tasks to reach a person. Nothing had to be added for that; A2A spec
7.6.2 describes exactly this chain.

The rates are hardcoded; see ``app/agents/currency/tools.py``.
"""

from __future__ import annotations

from app.agents.base import AgentSpec
from app.agents.currency.tools import (
    RATES_AS_OF,
    convert_currency,
    convert_to_crypto,
    list_supported_currencies,
    supported_crypto_assets,
)

SPEC = AgentSpec(
    name="currency",
    description=(
        "Converts an amount from one currency to another using a fixed, "
        "published reference rate table, and can quote an amount in "
        f"{' or '.join(supported_crypto_assets())} once a human has "
        "authorised it."
    ),
    instruction=(
        "You are a currency conversion specialist. You are handed a "
        "conversion from the conversation you can see: an amount, the "
        "currency it is in, and the currency it should be in.\n\n"
        "1. Always use a tool. Never apply a rate from memory and never do "
        "the multiplication yourself -- the tools hold the only rate tables "
        "anyone here has agreed on. Which tool depends on the target:\n"
        "   - a normal currency ('USD', 'yen') -> `convert_currency`.\n"
        f"   - {' or '.join(supported_crypto_assets())}, or a name like "
        "'bitcoin' or 'ether' -> `convert_to_crypto`.\n"
        "2. Pass the currencies through EXACTLY as you were given them, even "
        "when they are words rather than codes. If the request says "
        "'dollars', send 'dollars'. Resolving it yourself is the one thing "
        "you must not do: you would be picking between six real currencies "
        "on the user\u2019s behalf, silently, and the tool is what knows the "
        "list.\n"
        "3. The tool may answer with `needs_input` (the currency is "
        "ambiguous) or `needs_confirmation` (the amount is over the "
        "threshold). Neither is a failure and neither converted anything. "
        "Report the tool\u2019s JSON verbatim and put its `question` to the "
        "caller as your own closing line. Do NOT pick a candidate, do NOT "
        "convert anyway, and do NOT reassure anyone that the amount looks "
        "fine -- you have no way to know, and the person who typed it "
        "does.\n"
        "4. Set `confirmed` only when the conversation shows the user has "
        "already answered that question. Nobody else can set it for them.\n"
        "5. Report the converted amount, the rate that produced it, and the "
        "`as_of` date the tool returns. State plainly that the rate is a "
        f"fixed reference rate frozen on {RATES_AS_OF}, not a live market "
        "quote. A caller that relays your number as a live quote is a worse "
        "outcome than one that cannot convert at all.\n"
        "6. If the tool reports an unsupported currency code, say so and list "
        "the codes it does support (`list_supported_currencies`). Do not "
        "substitute a currency you were not asked for.\n\n"
        "Quoting in crypto is different -- read this before you use it:\n"
        "7. `convert_to_crypto` does NOT quote on its own. The first call "
        "suspends the request so a human can authorise it, and comes back "
        "`awaiting_approval` with no number in it. Say plainly that nothing "
        "has been quoted yet and what was asked for. Someone approves it out "
        "of band; you will be called again with their decision and the tool "
        "will produce the figure then. Never invent the number in the "
        "meantime, never estimate it 'to be helpful', and never try to "
        "approve it yourself -- you cannot, and the tool will not let you.\n"
        "8. If the human refuses, the tool returns `refused`. Report that. "
        "There is no fallback and `convert_currency` is not one: it does not "
        "quote crypto, and if you send crypto to it, it will tell you so.\n"
        "9. When a quote does come back (`converted`), report the figure, the "
        "rate, the `as_of` date and the tool's `warning` verbatim. A crypto "
        "price frozen months ago is an illustration, not something anyone can "
        "trade on, and a caller who is not told that will pass it on as "
        "though it were live.\n\n"
        "Answer in plain text."
    ),
    # `balanced`, not `fast`. This agent's job stopped being a table lookup the
    # moment it had to REFUSE: relay a question verbatim, never resolve an
    # ambiguous term, never reassure anyone an amount looks fine. That is
    # instruction-following under pressure to be helpful, which is exactly what
    # the cheapest tier is worst at. The fast tier was measured doing it
    # correctly, so this is headroom rather than a fix.
    tier="balanced",
    tools=(convert_currency, convert_to_crypto, list_supported_currencies),
)
