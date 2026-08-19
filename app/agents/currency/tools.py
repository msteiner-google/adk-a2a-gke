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

"""Tools specific to the currency agent.

The rate table below is **hardcoded**, exactly as the current scope asks for. It
is a frozen indicative snapshot, not a market feed: every conversion carries
``rate_source='hardcoded'`` and the ``as_of`` date so a caller can neither
mistake it for live pricing nor quietly relay it as such.

Why a table of USD prices rather than a matrix of pairs
-------------------------------------------------------
:data:`_USD_PER_UNIT` stores what one unit of each currency is worth in USD, and
every pair is derived from it. A matrix of ``N * (N-1)`` quoted pairs is the
obvious alternative and the wrong one: it can hold rates that disagree with each
other (EUR->GBP->USD landing somewhere other than EUR->USD), and the arbitrage it
invents is the kind of bug that only shows up in a number nobody double-checked.
One anchor per currency makes an inconsistent triangle unrepresentable.

Replacing this with a real feed means changing :func:`_rate` and the metadata it
reports. The tool signatures, the contract in ``app/agents/contracts.py`` and
everything downstream stay as they are.
"""

from __future__ import annotations

#: The day these indicative rates were frozen. Reported with every conversion:
#: a stale rate that says so is usable, one that stays quiet is not.
RATES_AS_OF = "2026-01-02"

#: Marks where a number came from, so a caller can tell a frozen demo rate from
#: a live quote without inspecting this module.
RATE_SOURCE = "hardcoded"

#: Value of ONE unit of each currency in USD. USD is 1.0 by definition; every
#: other pair is derived (see :func:`_rate`), so the table cannot describe a set
#: of rates that disagree with itself.
_USD_PER_UNIT: dict[str, float] = {
    "USD": 1.0,
    "EUR": 1.09,
    "GBP": 1.27,
    "CHF": 1.12,
    "JPY": 0.0067,
    "CNY": 0.14,
    "CAD": 0.74,
    "AUD": 0.66,
    "NZD": 0.61,
    "SEK": 0.096,
    "NOK": 0.094,
    "DKK": 0.146,
    "PLN": 0.25,
    "CZK": 0.043,
    "SGD": 0.75,
    "HKD": 0.128,
    "INR": 0.012,
    "BRL": 0.18,
    "MXN": 0.055,
    "ZAR": 0.054,
    "AED": 0.272,
    "TRY": 0.029,
}

#: Currencies with no minor unit, where a fractional amount is meaningless.
#: Everything else is quoted to 2 decimals.
_ZERO_DECIMAL = frozenset({"JPY", "KRW", "CLP", "ISK", "VND"})

#: How many decimals a *rate* is quoted to. Deliberately wider than the amount:
#: a USD/JPY rate rounded to 2 decimals would be 0.01 and useless.
_RATE_DECIMALS = 6


def supported_currencies() -> tuple[str, ...]:
    """Return every currency code this agent can convert, sorted.

    Returns:
        The supported ISO-4217 codes.
    """
    return tuple(sorted(_USD_PER_UNIT))


def _normalise(code: str) -> str:
    """Return a currency code in the canonical form used by the table.

    Args:
        code: A currency code as the caller wrote it.

    Returns:
        The code uppercased and stripped.
    """
    return code.strip().upper()


def _rate(source: str, target: str) -> float:
    """Return how many units of ``target`` one unit of ``source`` buys.

    Args:
        source: The currency being converted from (already normalised).
        target: The currency being converted to (already normalised).

    Returns:
        The cross rate, derived from both currencies' USD prices.
    """
    return _USD_PER_UNIT[source] / _USD_PER_UNIT[target]


def _quantize(amount: float, currency: str) -> str:
    """Round an amount to its currency's minor unit and render it as a string.

    A string rather than a float: the caller relays this number through a model
    and then through the A2A text boundary, and ``1234.5600000000001`` surviving
    that trip is a support ticket. Rounding once, here, is also the only place
    that knows JPY has no minor unit.

    Args:
        amount: The converted amount.
        currency: The currency it is now denominated in.

    Returns:
        The amount rendered with the right number of decimals.
    """
    decimals = 0 if currency in _ZERO_DECIMAL else 2
    return f"{amount:.{decimals}f}"


def convert_currency(
    amount: float, from_currency: str, to_currency: str
) -> dict[str, str]:
    """Convert an amount between currencies at a frozen indicative rate.

    The rates are hardcoded (see the module docstring), so the result carries
    ``rate_source`` and ``as_of``. Report both: a converted figure presented as
    a live quote is worse than no conversion at all.

    Args:
        amount: How much to convert, in ``from_currency``.
        from_currency: ISO-4217 code to convert from, e.g. ``"EUR"``.
        to_currency: ISO-4217 code to convert to, e.g. ``"USD"``.

    Returns:
        A mapping with the converted amount and the rate used, or an ``error``
        status naming the unsupported code.
    """
    source = _normalise(from_currency)
    target = _normalise(to_currency)

    unknown = [code for code in (source, target) if code not in _USD_PER_UNIT]
    if unknown:
        return {
            "status": "error",
            "error": f"Unsupported currency code(s): {', '.join(unknown)}.",
            "supported": ", ".join(supported_currencies()),
        }

    rate = _rate(source, target)
    return {
        "status": "ok",
        "amount": _quantize(amount, source),
        "from_currency": source,
        "to_currency": target,
        "rate": f"{rate:.{_RATE_DECIMALS}f}",
        "converted": _quantize(amount * rate, target),
        "rate_source": RATE_SOURCE,
        "as_of": RATES_AS_OF,
    }


def list_supported_currencies() -> dict[str, str]:
    """List the currency codes this agent can convert between.

    Returns:
        A mapping with the supported codes and the date the rates were frozen.
    """
    return {
        "status": "ok",
        "supported": ", ".join(supported_currencies()),
        "count": str(len(_USD_PER_UNIT)),
        "rate_source": RATE_SOURCE,
        "as_of": RATES_AS_OF,
    }
