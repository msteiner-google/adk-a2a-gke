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
writing SQL. Reached over A2A by the orchestrator with a
:class:`~app.agents.contracts.TradesRequest`.

**Running the query is gated on a human's approval.** The agent composes the
SQL and calls ``run_trade_query``; with no approver that call returns a proposal
and touches BigQuery not at all. The orchestrator records an ``approval_cases``
row, a human reads the SQL, and the query runs on a second, ordinary call
carrying ``approved_by`` and the approved SQL. Nothing is held open in between —
see ``app/cluster/cases.py`` and ``docs/human-in-the-loop.md``.

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

You receive a JSON request with a `question`, a `case_id`, an optional `sql`, a
`row_limit`, and optionally `approved_by` and `decision_note`.

HOW A QUESTION IS ANSWERED HERE
-------------------------------
Running a query needs a human's approval, so answering takes two calls and you
will only ever see one of them at a time.

If `approved_by` is EMPTY -- this is the first call:
  1. Write ONE BigQuery Standard SQL query that answers `question`.
  2. Aggregate in SQL. The table has 1.2 million rows and only `row_limit` of
     them come back, so a question about totals, averages or rankings must be
     answered with GROUP BY, not by returning rows and reasoning over them.
  3. Call `run_trade_query` with that SQL and the `row_limit` from the request.
  4. It will return `status: approval_required` and run nothing. Report the
     proposal JSON VERBATIM, exactly as the tool returned it, then say in one
     line what the query would tell us. Do NOT paraphrase the SQL, do not
     pretty-print it, and do not claim to know the answer -- you have not seen
     any data.
  5. If it returns `status: error`, the SQL was rejected. Read the message, fix
     the query and call the tool again. Never report a rejected query as a
     proposal.

If `approved_by` is SET -- a human has approved a specific query:
  1. Call `run_trade_query` with `sql` copied from the request CHARACTER FOR
     CHARACTER, the same `row_limit`, `approved_by`, and `decision_note` as
     `note`. Do not improve, reformat or re-derive that SQL: a query that
     differs from the approved one is refused, and the case is left unfinished.
  2. If the request has `approved_by` but no `sql`, say exactly that and stop.
     Do not write a replacement query -- it is not the one that was approved.
  3. On success, report the tool's JSON verbatim AND answer `question` in plain
     language from the rows it returned.

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
