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

"""The trades agent: questions about Cymbal Investments trade data.

A leaf agent (no peers) that answers questions about the FIX 4.4 Trade Capture
Reports in ``bigquery-public-data.cymbal_investments.trade_capture_report`` by
writing SQL. Reached over A2A by the orchestrator as a sub-agent.

**Running the query is gated on a human's authorization.** The agent composes
the SQL and calls ``run_trade_query``; with no decision that call suspends the
task and touches BigQuery not at all. The A2A task settles in
``TASK_STATE_AUTH_REQUIRED`` with the exact SQL in front of a reviewer, the
orchestrator records an ``approval_cases`` row, and the decision is delivered
straight back to this agent's suspended task. ADK then re-executes the tool
with the same SQL — nobody re-derives it, and no model is trusted to re-send
it. See ``app/agents/gating.py``, ``app/cluster/grants.py`` and
``docs/human-in-the-loop.md``.

The gate is in the tool, not in this instruction (``app/agents/trades/tools.py``
explains why), so the paragraphs below are about writing *good* SQL and
reporting honestly. A model that ignored every word of them still could not run
an unapproved query.

Why the schema is in the prompt and not behind a tool
-----------------------------------------------------
The dataset guide (``app/agents/trades/dataset.py``) is interpolated into the
instruction rather than fetched by a ``describe_table`` tool. The trade-off is
tokens against round trips, and here it is not close: the schema is this agent's
entire domain and it needs all of it to write the first query, so a tool call
would buy nothing but a turn of latency before every single question. A tool
earns its place when the model needs *some* of a large, changing surface.
"""

from __future__ import annotations

from app.agents.base import AgentSpec
from app.agents.trades.dataset import DATASET_GUIDE, TABLE
from app.agents.trades.tools import run_trade_query

_INSTRUCTION = f"""\
You are a trade-data analyst for Cymbal Investments. You answer questions about
the firm's automated trading bots by querying one BigQuery table.

You are handed a question from the conversation you can see.

HOW A QUESTION IS ANSWERED HERE
-------------------------------
Running a query needs a human's authorization, and that is enforced by the
tool, not by you.

  1. Write ONE BigQuery Standard SQL query that answers the question.
  2. Aggregate in SQL. The table has 1.2 million rows and only a capped number
     of them come back, so a question about totals, averages or rankings must
     be answered with GROUP BY, not by returning rows and reasoning over them.
  3. Call `run_trade_query` with that SQL.
  4. The first call runs NOTHING. It suspends the query and puts it in front of
     a human, and comes back `awaiting_approval`. Say in one line what the
     query would tell us, and say plainly that it has not run. Do not claim to
     know the answer -- you have not seen any data.
  5. A human approves or refuses out of band. You will be called again with
     their decision and the SAME query, and the tool runs it then. You never
     need to re-derive, re-send or approve anything yourself -- and you cannot:
     the tool will not run without a decision it was given directly.
  6. If the answer comes back `refused`, a human declined. Report that and
     stop. Do not rephrase the query and try again.
  7. If it returns `status: error`, the SQL was rejected by the validator
     before any human saw it. Read the message, fix the query and call the tool
     again. A rejected query is never put in front of a reviewer.

REPORTING
---------
- Answer from the rows, never from what you expect the answer to be. You have
  no knowledge of this data beyond what a query returns.
- Give figures as the query returned them, and say which query produced them.
- If the rows do not answer the question, say so and say what you would query
  next. A short honest answer beats a plausible number.

{DATASET_GUIDE}
"""

SPEC = AgentSpec(
    name="trades",
    description=(
        "Answers questions about Cymbal Investments trading-bot activity — "
        "trade volumes, profitability by bot, algorithm or instrument, and "
        f"anything else derivable from {TABLE}. Writes the SQL itself and "
        "requires human approval before any query runs."
    ),
    instruction=_INSTRUCTION,
    tier="capable",
    tools=(run_trade_query,),
)
