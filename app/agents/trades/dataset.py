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

"""What the trades agent knows about the Cymbal Investments dataset.

Kept in its own module, apart from the spec and the tools, because it plays two
roles that must not drift apart: it is interpolated into the agent's instruction
(so the model can write correct SQL first time) **and** :data:`TABLE` is the
allow-list the SQL validator in ``app/agents/trades/tools.py`` enforces. One
constant, one table, no way for the prompt to describe something the guard does
not permit.

Everything here was read off the live table rather than transcribed from the
dataset's marketing description — the schema from ``bq show``, the cardinalities
and value domains from aggregate queries against all 1,222,562 rows. That
matters for two claims the model relies on and the published description does
not make: ``Sides`` is a REPEATED record that in practice holds exactly one
entry per trade, and ``PartyRole`` has exactly one value. A model that assumes
otherwise writes defensive, slow SQL; a model told so writes the query a human
analyst would.
"""

from __future__ import annotations

#: The one table this agent may query. The validator rejects a statement that
#: reads anything else, so widening the agent's reach is an edit here plus a
#: matching IAM grant -- not something a generated query can do on its own.
TABLE = "bigquery-public-data.cymbal_investments.trade_capture_report"

#: Everything the model needs to write correct SQL against :data:`TABLE`.
#: Interpolated into the agent instruction.
DATASET_GUIDE = f"""\
THE DATA
--------
One table, and you may query no other:
  `{TABLE}`

It holds 1,222,562 FIX 4.4 Trade Capture Reports from Cymbal Investments'
automated trading bots, covering trade dates 2020-05-18 to 2020-11-23.

Each row is one contract-for-difference on the level of an index. The bot goes
LONG (betting the level rises) or SHORT (betting it falls) at `LastPx`, and the
contract settles automatically one minute later at `StrikePrice`. So a row is
a complete, already-closed round trip: entry price, exit price, direction and
size are all present, and no join is needed to compute what the trade made.

COLUMNS
-------
  SendingTime    TIMESTAMP  when the FIX message was sent
  TargetCompID   STRING     firm receiving the message
  SenderCompID   STRING     firm sending the message (always 'MDOC')
  Symbol         STRING     contract traded, e.g. 'ESU0', 'NQZ0', 'BTCV0'
  Quantity       INTEGER    contract size
  OrderID        STRING     order identifier
  TransactTime   TIMESTAMP  when the trade was executed
  StrikePrice    FLOAT      price the CFD CLOSED at  (the exit)
  LastPx         FLOAT      price the CFD was ENTERED at (the entry)
  MaturityDate   TIMESTAMP  contract expiry
  TradeReportID  STRING     id of this trade report
  TradeDate      DATE       date the trade executed
  CFICode        STRING     instrument classification (always 'MMMXXX')
  Sides          REPEATED RECORD
    .Side        STRING     'LONG' or 'SHORT'
    .OrderID     STRING
    .PartyIDs    REPEATED RECORD
      .PartyID       STRING  the trading bot, e.g. 'MOMOES', 'PREDICTBTC'
      .PartyIDSource STRING
      .PartyRole     STRING  always 'INITIATING TRADER'

NESTING
-------
`Sides` and `Sides.PartyIDs` are REPEATED, so they must be flattened with
UNNEST before their fields can be read. In this dataset both hold exactly one
element per row -- 1,222,562 rows produce 1,222,562 flattened rows -- so the
cross join below neither drops nor duplicates trades, and no DISTINCT is needed:

  FROM `{TABLE}` AS t,
  UNNEST(t.Sides) AS s,
  UNNEST(s.PartyIDs) AS p

`PartyRole` has one value across the whole table, so filtering on it is never
what makes a query correct.

THE BOTS
--------
`PartyID` names the bot. It concatenates the ALGORITHM it runs with the
INSTRUMENT it trades, and there are nine, one per pair:

  algorithms:   PREDICT, LUCKY, MOMO
  instruments:  ES (S&P 500 futures), NQ (Nasdaq 100 futures), BTC (bitcoin)

  PREDICTES  PREDICTNQ  PREDICTBTC
  LUCKYES    LUCKYNQ    LUCKYBTC
  MOMOES     MOMONQ     MOMOBTC

To compare ALGORITHMS rather than bots, group on the prefix; to compare
INSTRUMENTS, group on the suffix. Neither is a stored column, so derive it:

  REGEXP_EXTRACT(p.PartyID, r'^(PREDICT|LUCKY|MOMO)')  AS algorithm
  REGEXP_EXTRACT(p.PartyID, r'(ES|NQ|BTC)$')           AS instrument

`Symbol` is the specific contract, not the instrument: ESM0/ESU0/ESZ0 are three
successive S&P contracts, and BTC has seven (BTCK0 ... BTCZ0). Group on the
derived instrument, not on Symbol, unless the question is about one contract.

PROFIT
------
There is no profit column. Direction decides the sign, and this expression is
the definition of trader profit for this dataset -- use it verbatim:

  CASE WHEN s.Side = 'LONG'
       THEN (t.StrikePrice - t.LastPx) * t.Quantity
       ELSE (t.LastPx - t.StrikePrice) * t.Quantity
  END AS profit

A LONG makes money when the exit is above the entry; a SHORT when it is below.
Aggregating this with SUM is how the performance of a bot, an algorithm or the
whole book is judged. Profits here are small absolute numbers (tens to a few
thousand over the whole period), which is expected, not a units error.

WORKED EXAMPLE
--------------
Total profit and win rate per algorithm, most profitable first:

  SELECT
    REGEXP_EXTRACT(p.PartyID, r'^(PREDICT|LUCKY|MOMO)') AS algorithm,
    COUNT(*) AS trades,
    ROUND(SUM(CASE WHEN s.Side = 'LONG'
                   THEN (t.StrikePrice - t.LastPx) * t.Quantity
                   ELSE (t.LastPx - t.StrikePrice) * t.Quantity END), 2)
      AS total_profit,
    ROUND(AVG(CASE WHEN (CASE WHEN s.Side = 'LONG'
                              THEN t.StrikePrice - t.LastPx
                              ELSE t.LastPx - t.StrikePrice END) > 0
              THEN 1 ELSE 0 END), 4) AS win_rate
  FROM `{TABLE}` AS t,
  UNNEST(t.Sides) AS s,
  UNNEST(s.PartyIDs) AS p
  GROUP BY algorithm
  ORDER BY total_profit DESC
"""

__all__ = ["DATASET_GUIDE", "TABLE"]
