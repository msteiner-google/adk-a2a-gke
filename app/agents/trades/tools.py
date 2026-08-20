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

"""Tools for the trades agent: a BigQuery read gated behind human approval.

:func:`run_trade_query` is the second gated action in this repo, and it is
deliberately built to the same shape as ``app/agents/math/tools.py``: **one
function, two behaviours**, chosen by whether ``approved_by`` is present. With
no approver it validates the SQL and returns a proposal; with one it runs the
query. There is no second code path that queries without that check, which is
what makes "no query runs unreviewed" a property of the code rather than a hope
about the prompt (``docs/design-decisions.md``, D5).

Why gate a read at all
----------------------
The action is read-only, so the risk is not corruption. It is that a model
composes a query nobody read, against a table nobody scoped, billed to an
account nobody watched -- and returns a confident number derived from the wrong
rows. Approval puts a human in front of the SQL *and* the number's provenance,
which is the review a financial answer actually needs. This is the general case:
plenty of actions worth gating are reads.

Reproducing the approved query
------------------------------
The math specialist can recompute its result from ``expression`` because
arithmetic is deterministic. SQL generation is not: ask a model the same
question twice and the text differs. So the approved SQL travels back in the
request (``TradesRequest.sql``) and this tool **refuses to run without it**
rather than regenerating something similar. Two independent things then hold:

- the tool canonicalises the SQL, so a re-send that only differs in whitespace
  or a trailing semicolon still matches what was approved; and
- the caller compares the echoed ``sql`` against the case record
  (``cases.find_execution``), so a genuinely different query is reported as
  ``approved_not_confirmed`` rather than recorded as the approved one.

What the SQL validator is and is not
------------------------------------
:func:`validate_sql` rejects anything that is not a single read-only statement
against :data:`~app.agents.trades.dataset.TABLE`. It is a guard rail, not the
security boundary. It works on masked text -- comments and string literals are
blanked first, so a keyword hidden in a literal cannot trip or evade it -- and
it enforces: one statement, ``SELECT``/``WITH`` only, no DDL/DML/procedural
keywords, every backticked identifier equal to the allowed table, and no
fully-qualified reference smuggled in outside backticks.

What actually confines this agent is the combination of three things: the human
who reads the SQL before it runs, the pod's IAM (``roles/bigquery.jobUser``
only -- it can start jobs and read public data, and holds ``dataViewer`` on
nothing), and ``maximum_bytes_billed``. Treat the validator as the thing that
keeps the review meaningful, and do not add a bypass for "trusted" callers.
"""

from __future__ import annotations

import datetime as dt
import decimal
import os
import re
from typing import TYPE_CHECKING, Any

# ToolContext must be imported at RUNTIME (see app/agents/gating.py) -- ADK
# evaluates this annotation with typing.get_type_hints() to build the tool's
# declaration, and a TYPE_CHECKING-only import breaks every tool in the module.
from google.adk.tools.tool_context import ToolContext

from app.agents.gating import (
    AWAITING_APPROVAL,
    REFUSED,
    gated,
    require_approval,
)
from app.agents.statuses import EXECUTED
from app.agents.trades.dataset import TABLE

if TYPE_CHECKING:
    from collections.abc import Iterable

QUERY_ACTION = "run_trade_query"
"""Names the effect in a proposal, so a reviewer sees what they are approving."""

#: Rows actually returned to the caller if it asks for no limit of its own.
DEFAULT_ROW_LIMIT = 20

#: Hard ceiling on returned rows, overridable with ``TRADES_MAX_ROWS``. The
#: result crosses A2A as text inside a model's context, so this is a payload
#: budget, not a database concern: aggregate in SQL, do not page through here.
MAX_ROWS_ENV = "TRADES_MAX_ROWS"
DEFAULT_MAX_ROWS = 50

#: Byte ceiling passed to BigQuery as ``maximum_bytes_billed``; the job is
#: killed rather than billed beyond it. Default 1 GiB against a 300 MB table,
#: so an ordinary full scan fits and a runaway self-join does not.
MAX_BYTES_ENV = "TRADES_MAX_BYTES_BILLED"
DEFAULT_MAX_BYTES = 1024 * 1024 * 1024

#: Location the query job runs in. ``bigquery-public-data`` lives in the US
#: multi-region and a job in the wrong location fails with "dataset not found",
#: which reads like a permissions problem and is not.
LOCATION_ENV = "TRADES_LOCATION"
DEFAULT_LOCATION = "US"

#: Every query this process actually ran, newest last. A module-level list, like
#: ``math.tools.PUBLICATIONS``: it is what lets a test assert the effect
#: happened exactly once, and only after approval.
EXECUTIONS: list[dict[str, Any]] = []

# Statement kinds and procedural constructs that have no business in a read.
# Matched as whole words against masked text (comments and string literals
# already blanked), so a column called `create_time` does not trip it.
_FORBIDDEN = (
    "insert",
    "update",
    "delete",
    "merge",
    "truncate",
    "drop",
    "alter",
    # "create" also covers CREATE OR REPLACE, which is why "replace" itself is
    # NOT on this list: REPLACE() is an ordinary string function and a SELECT
    # that uses it is a perfectly good read.
    "create",
    "grant",
    "revoke",
    "call",
    "execute",
    "begin",
    "commit",
    "rollback",
    "export",
    "load",
    "assert",
    "declare",
    "set",
    "external_query",
    "information_schema",
)
_FORBIDDEN_RE = re.compile(r"\b(" + "|".join(_FORBIDDEN) + r")\b", re.IGNORECASE)

# Targets of a FROM/JOIN that are NOT backtick-quoted. Used to catch a
# fully-qualified `project.dataset.table` slipped in without backticks.
_FROM_TARGET_RE = re.compile(r"\b(?:from|join)\s+([A-Za-z_][\w.-]*)", re.IGNORECASE)


#: Stands in for a backtick-quoted identifier in masked text. A real, dotless
#: identifier rather than blanks: blanking the table name out of
#: ``FROM `t` JOIN evil.d.t`` leaves ``FROM`` adjacent to ``JOIN``, the FROM/JOIN
#: scanner consumes ``JOIN`` as the first target, and the qualified reference
#: after it is never examined. Found by a test, not by reading the code.
_IDENT_PLACEHOLDER = " _t "


def _scan(sql: str) -> list[tuple[str, str]]:
    """Split a statement into classified spans in one pass.

    Both the canonicaliser and the validator need the same thing — to know which
    parts of the text are code and which are comments, string literals or quoted
    identifiers — so they share one scanner rather than each growing an
    approximate version of it.

    Args:
        sql: The raw statement.

    Returns:
        ``(kind, text)`` pairs covering the input exactly, where ``kind`` is one
        of ``code``, ``comment``, ``string`` or ``ident``.
    """
    spans: list[tuple[str, str]] = []
    code: list[str] = []
    index = 0
    length = len(sql)

    def flush() -> None:
        if code:
            spans.append(("code", "".join(code)))
            code.clear()

    while index < length:
        char = sql[index]
        rest = sql[index:]
        if rest.startswith("--") or char == "#":
            end = sql.find("\n", index)
            end = length if end == -1 else end
            kind = "comment"
        elif rest.startswith("/*"):
            end = sql.find("*/", index + 2)
            end = length if end == -1 else end + 2
            kind = "comment"
        elif char == "`":
            closing = sql.find("`", index + 1)
            end = length if closing == -1 else closing + 1
            kind = "ident"
        elif char in ("'", '"'):
            quote = char * 3 if rest.startswith(char * 3) else char
            end = index + len(quote)
            while end < length:
                if sql[end] == "\\":
                    end += 2
                    continue
                if sql.startswith(quote, end):
                    end += len(quote)
                    break
                end += 1
            end = min(end, length)
            kind = "string"
        else:
            code.append(char)
            index += 1
            continue
        flush()
        spans.append((kind, sql[index:end]))
        index = end

    flush()
    return spans


def _mask(sql: str) -> tuple[str, list[str]]:
    """Blank out comments and literals, and collect backticked names.

    Args:
        sql: The statement to mask.

    Returns:
        Text safe to pattern-match — a keyword inside a literal can neither trip
        the denylist nor hide from it — and every backtick-quoted identifier, in
        order.
    """
    masked: list[str] = []
    identifiers: list[str] = []
    for kind, text in _scan(sql):
        if kind == "code":
            masked.append(text)
        elif kind == "ident":
            identifiers.append(text.strip("`"))
            masked.append(_IDENT_PLACEHOLDER)
        else:
            masked.append(" " * len(text))
    return "".join(masked), identifiers


def canonical_sql(sql: str) -> str:
    """Return the one spelling of a statement that this agent will run.

    Drops comments, collapses runs of whitespace *outside* string literals, and
    removes a trailing semicolon, so a re-send that a model reflowed is still
    recognisably the approved query. This is
    :func:`~app.agents.math.tools.canonical_value`'s counterpart: the caller
    confirms an execution by comparing the echoed SQL against the approved
    proposal, and that comparison is only useful if the same query necessarily
    produces the same string.

    Canonicalising rather than loosening the comparison keeps the check strict:
    a query that reads different rows still fails it.

    Comments go before the newlines do, and that order is load-bearing rather
    than tidy. Collapse first and a comment on the line above a SELECT lands on
    the same line as it, commenting the whole query out — which BigQuery reports
    as a syntax error long after anyone is looking at this function.

    Args:
        sql: The statement as the model wrote it.

    Returns:
        The canonical single-line form.
    """
    parts: list[str] = []
    for kind, text in _scan(sql):
        if kind == "comment":
            parts.append(" ")
        elif kind == "code":
            parts.append(re.sub(r"\s+", " ", text))
        else:
            # A literal or a quoted identifier is content, not formatting:
            # collapsing whitespace inside it would change what the query means.
            parts.append(text)
    return "".join(parts).strip().rstrip(" ;").strip()


def validate_sql(sql: str) -> str | None:
    """Check that a statement is a single read against the allowed table.

    Args:
        sql: The statement to check (canonical or not).

    Returns:
        ``None`` when the statement is acceptable, otherwise a message saying
        precisely what is wrong, written to be handed back to the model.
    """
    statement = canonical_sql(sql)
    if not statement:
        return "The query is empty."

    masked, identifiers = _mask(statement)

    if ";" in masked:
        return "Only a single statement is allowed; remove the ';'."

    first = masked.lstrip().split(" ", 1)[0].lower()
    if first not in ("select", "with"):
        return f"A query must start with SELECT or WITH, not {first.upper()!r}."

    if forbidden := _FORBIDDEN_RE.search(masked):
        return (
            f"The keyword {forbidden.group(1).upper()!r} is not allowed: this "
            f"agent may only run a read-only SELECT."
        )

    if wrong := [name for name in identifiers if name != TABLE]:
        return (
            f"Only `{TABLE}` may be queried; found {wrong[0]!r}. Quote the "
            f"table in backticks exactly as given."
        )

    if TABLE not in identifiers:
        return f"The query must read from `{TABLE}`."

    if qualified := [t for t in _FROM_TARGET_RE.findall(masked) if t.count(".") >= 2]:
        return (
            f"{qualified[0]!r} is a fully-qualified reference outside "
            f"backticks. Only `{TABLE}` may be queried."
        )

    return None


def _positive_int_env(name: str, default: int) -> int:
    """Read a positive integer from the environment, falling back on nonsense.

    Args:
        name: The environment variable to read.
        default: Value to use when unset, unparsable or non-positive.

    Returns:
        The configured limit.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _row_cap() -> int:
    """Return the maximum number of rows a caller may ask for."""
    return _positive_int_env(MAX_ROWS_ENV, DEFAULT_MAX_ROWS)


def _clamp_rows(row_limit: int) -> int:
    """Clamp a requested row count into ``1..row_cap``.

    Args:
        row_limit: What the caller asked for.

    Returns:
        The row count that will actually be used.
    """
    cap = _row_cap()
    if row_limit <= 0:
        return min(DEFAULT_ROW_LIMIT, cap)
    return min(row_limit, cap)


def _jsonify(value: Any) -> Any:
    """Convert a BigQuery cell into something JSON-serialisable.

    Args:
        value: A cell value, possibly nested.

    Returns:
        The value as a JSON-safe equivalent.
    """
    if isinstance(value, dt.datetime | dt.date | dt.time):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, dict):
        return {key: _jsonify(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonify(item) for item in value]
    return value


def _rows_to_dicts(rows: Iterable[Any]) -> list[dict[str, Any]]:
    """Materialise BigQuery result rows as JSON-safe dictionaries.

    Args:
        rows: The row iterator from a finished query job.

    Returns:
        One dictionary per row.
    """
    return [{key: _jsonify(value) for key, value in dict(row).items()} for row in rows]


def _execute(statement: str, row_limit: int) -> dict[str, Any]:
    """Run the approved statement against BigQuery.

    Separated from :func:`run_trade_query` so the gate above it stays a handful
    of readable lines, and so a test can stub the one function that needs a
    network.

    Args:
        statement: The canonical, validated SQL.
        row_limit: How many rows to return.

    Returns:
        The rows plus what the job cost, or an ``error`` status describing why
        the query did not run.
    """
    # Imported inside the function: building a client performs credential
    # discovery, and nothing about importing this module should need ADC.
    from google.cloud import bigquery

    try:
        client = bigquery.Client(project=os.environ.get("GOOGLE_CLOUD_PROJECT") or None)
        job = client.query(
            statement,
            job_config=bigquery.QueryJobConfig(
                use_query_cache=True,
                # The query is killed rather than billed past this. A ceiling on
                # spend is the one control a human approving the SQL cannot
                # apply by reading it.
                maximum_bytes_billed=_positive_int_env(
                    MAX_BYTES_ENV, DEFAULT_MAX_BYTES
                ),
                labels={"agent": "trades"},
            ),
            location=os.environ.get(LOCATION_ENV, "").strip() or DEFAULT_LOCATION,
        )
        result = job.result(max_results=row_limit)
        rows = _rows_to_dicts(result)
    except Exception as exc:
        # Reported, never raised: a BigQuery failure is an answer the reviewer
        # needs ("it did not run, and why"), not a crashed turn.
        return {
            "status": "error",
            "action": QUERY_ACTION,
            "sql": statement,
            "error": f"{type(exc).__name__}: {exc}",
        }

    return {
        "rows": rows,
        "row_count": len(rows),
        "total_rows": int(getattr(result, "total_rows", len(rows)) or len(rows)),
        "bytes_processed": int(job.total_bytes_processed or 0),
        "cache_hit": bool(job.cache_hit),
    }


@gated
def run_trade_query(
    sql: str,
    tool_context: ToolContext,
    row_limit: int = DEFAULT_ROW_LIMIT,
) -> dict[str, Any]:
    """Run a read-only query against the trade data, once a human has approved.

    One function, two behaviours, chosen by whether an approval is present. With
    no ``approved_by`` the BigQuery call below is unreachable, so a model that
    decides to run its own query simply cannot: the most it can do is propose
    one. Invalid SQL is rejected in both directions, so a reviewer is never
    shown a proposal that could not have run.

    Args:
        sql: The SQL to run.
        tool_context: Injected by ADK. Carries the authorization decision.
        row_limit: How many rows to return, clamped to the configured cap.

    Returns:
        ``status='awaiting_approval'`` while suspended, ``status='refused'`` if
        a human declined, ``status='executed'`` with the rows once approved, or
        ``status='error'`` if the SQL is refused by the validator.
    """
    statement = canonical_sql(sql)
    rows_wanted = _clamp_rows(row_limit)

    if problem := validate_sql(statement):
        return {
            "status": "error",
            "action": QUERY_ACTION,
            "sql": statement,
            "error": problem,
        }

    # Ask for authorization only AFTER validation: a statement that could never
    # run is rejected above rather than put in front of a human. ADK re-executes
    # this function with the same arguments once a decision arrives, so the
    # BigQuery call below stays unreachable until then (app/agents/gating.py).
    decision = require_approval(
        tool_context,
        summary=(
            f"Run a read-only BigQuery query against {TABLE} and return up to "
            f"{rows_wanted} rows: {statement}"
        ),
        # The canonicalised SQL and the clamped row count -- what will actually
        # run, not what the model typed.
        proposal={
            "action": QUERY_ACTION,
            "sql": statement,
            "row_limit": rows_wanted,
        },
    )
    if decision.pending:
        return {"status": AWAITING_APPROVAL, "action": QUERY_ACTION, "sql": statement}
    if not decision.granted:
        return {"status": REFUSED, "action": QUERY_ACTION, "note": decision.note}

    outcome = _execute(statement, rows_wanted)
    if outcome.get("status") == "error":
        # A failed query is not a performed effect: report it as an error so the
        # case stays re-drivable instead of being closed as done.
        return outcome

    record = {
        # `status`, `action`, `sql` and `row_limit` are what the caller matches
        # against the approved proposal (cases.find_execution). Changing any of
        # them here breaks confirmation, and the case is correctly refused.
        "status": EXECUTED,
        "action": QUERY_ACTION,
        "sql": statement,
        "row_limit": rows_wanted,
        "approved_by": decision.approved_by,
        "note": decision.note,
        **outcome,
    }
    EXECUTIONS.append(record)
    return record
