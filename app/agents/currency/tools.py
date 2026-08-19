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

from typing import Any

from app.agents.contracts import NEEDS_CONFIRMATION, NEEDS_INPUT

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

#: Full names, so a disambiguation question reads like a question a person would
#: ask rather than a list of three-letter codes.
_NAMES: dict[str, str] = {
    "USD": "United States dollar",
    "EUR": "euro",
    "GBP": "pound sterling",
    "CHF": "Swiss franc",
    "JPY": "Japanese yen",
    "CNY": "Chinese yuan renminbi",
    "CAD": "Canadian dollar",
    "AUD": "Australian dollar",
    "NZD": "New Zealand dollar",
    "SEK": "Swedish krona",
    "NOK": "Norwegian krone",
    "DKK": "Danish krone",
    "PLN": "Polish zloty",
    "CZK": "Czech koruna",
    "SGD": "Singapore dollar",
    "HKD": "Hong Kong dollar",
    "INR": "Indian rupee",
    "BRL": "Brazilian real",
    "MXN": "Mexican peso",
    "ZAR": "South African rand",
    "AED": "UAE dirham",
    "TRY": "Turkish lira",
}

#: Colloquial names and symbols, mapped to every code they could mean *in this
#: table*. A term with more than one candidate is the whole point: "dollars" is
#: six currencies and this agent is the only participant holding the list, so it
#: is the only one entitled to say which. The mapping is scoped to the table on
#: purpose -- "pound" is unambiguous here and would not be against a rate source
#: that also carried EGP and LBP, so widening the table means revisiting this.
_ALIASES: dict[str, tuple[str, ...]] = {
    "dollar": ("USD", "CAD", "AUD", "NZD", "SGD", "HKD"),
    "dollars": ("USD", "CAD", "AUD", "NZD", "SGD", "HKD"),
    "$": ("USD", "CAD", "AUD", "NZD", "SGD", "HKD"),
    "buck": ("USD", "CAD", "AUD"),
    "bucks": ("USD", "CAD", "AUD"),
    "krona": ("SEK", "NOK", "DKK"),
    "kronor": ("SEK", "NOK", "DKK"),
    "krone": ("SEK", "NOK", "DKK"),
    "kroner": ("SEK", "NOK", "DKK"),
    "crown": ("SEK", "NOK", "DKK"),
    "crowns": ("SEK", "NOK", "DKK"),
    "yuan": ("CNY",),
    "renminbi": ("CNY",),
    "yen": ("JPY",),
    # Both the yen and the yuan are written with this symbol.
    "\u00a5": ("JPY", "CNY"),
    "euro": ("EUR",),
    "euros": ("EUR",),
    "\u20ac": ("EUR",),
    "pound": ("GBP",),
    "pounds": ("GBP",),
    "sterling": ("GBP",),
    "quid": ("GBP",),
    "\u00a3": ("GBP",),
    "franc": ("CHF",),
    "francs": ("CHF",),
    "rupee": ("INR",),
    "rupees": ("INR",),
    "real": ("BRL",),
    "reais": ("BRL",),
    "peso": ("MXN",),
    "pesos": ("MXN",),
    "rand": ("ZAR",),
    "dirham": ("AED",),
    "dirhams": ("AED",),
    "lira": ("TRY",),
    "zloty": ("PLN",),
    "koruna": ("CZK",),
}

#: Above this many US dollars, the conversion stops and asks. It is not a limit
#: and not an approval -- converting has no side effect, so the cost of asking
#: is one extra turn and the cost of not asking is a misplaced decimal point
#: reported as a fact. One constant to change if a desk works at a different
#: scale.
LARGE_AMOUNT_USD = 1_000_000.0

#: How many decimals a *rate* is quoted to. Deliberately wider than the amount:
#: a USD/JPY rate rounded to 2 decimals would be 0.01 and useless.
_RATE_DECIMALS = 6


def supported_currencies() -> tuple[str, ...]:
    """Return every currency code this agent can convert, sorted.

    Returns:
        The supported ISO-4217 codes.
    """
    return tuple(sorted(_USD_PER_UNIT))


def _describe(code: str) -> str:
    """Render a code for a human, e.g. ``"USD (United States dollar)"``.

    Args:
        code: An ISO-4217 code present in the rate table.

    Returns:
        The code with its full name, or the bare code if none is known.
    """
    name = _NAMES.get(code)
    return f"{code} ({name})" if name else code


def resolve_currency(term: str) -> tuple[str, tuple[str, ...]]:
    """Turn what the caller wrote into a code, or into the choices it could be.

    Args:
        term: An ISO-4217 code, a colloquial name, or a symbol.

    Returns:
        ``(code, ())`` when the term resolves to exactly one currency;
        ``("", candidates)`` when it is ambiguous, candidates in table order;
        ``("", ())`` when nothing here matches it.
    """
    cleaned = term.strip()
    if cleaned.upper() in _USD_PER_UNIT:
        return cleaned.upper(), ()
    candidates = _ALIASES.get(cleaned.lower())
    if not candidates:
        return "", ()
    known = tuple(code for code in candidates if code in _USD_PER_UNIT)
    if len(known) == 1:
        return known[0], ()
    return "", known


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


def _ambiguous(field: str, term: str, candidates: tuple[str, ...]) -> dict[str, Any]:
    """Build the reply that asks the user which currency they meant.

    Args:
        field: Which argument was ambiguous (``from_currency``/``to_currency``).
        term: The word the user actually used.
        candidates: The codes it could mean.

    Returns:
        A ``needs_input`` reply. Nothing has been converted.
    """
    options = ", ".join(_describe(code) for code in candidates)
    return {
        "status": NEEDS_INPUT,
        "reason": "ambiguous_currency",
        "field": field,
        "term": term,
        "candidates": list(candidates),
        "question": (
            f"Which currency does {term!r} mean here? It could be: {options}."
        ),
    }


def convert_currency(
    amount: float, from_currency: str, to_currency: str, confirmed: bool = False
) -> dict[str, Any]:
    """Convert an amount between currencies at a frozen indicative rate.

    Refuses, rather than guesses, in two cases. An ambiguous term ("dollars" is
    six currencies here) returns ``needs_input``; an amount over
    :data:`LARGE_AMOUNT_USD` returns ``needs_confirmation`` unless the user has
    already said to go ahead. Both come back with a ``question`` for the user
    and no conversion performed.

    Neither is an approval. There is no side effect to gate — the point is that
    a wrong currency or a misplaced decimal point becomes a confident number
    that travels onward into arithmetic, a published figure and a report, and
    the only participant able to notice is the person who typed it.

    The rates are hardcoded (see the module docstring), so a successful result
    carries ``rate_source`` and ``as_of``. Report both: a converted figure
    presented as a live quote is worse than no conversion at all.

    Args:
        amount: How much to convert, in ``from_currency``.
        from_currency: ISO-4217 code, or the user's own word, to convert from.
        to_currency: ISO-4217 code, or the user's own word, to convert to.
        confirmed: Whether the user has already confirmed a large conversion.
            Never suppresses the ambiguity question -- a currency nobody has
            named cannot be confirmed into existence.

    Returns:
        A mapping with the converted amount and the rate used, or a
        ``needs_input`` / ``needs_confirmation`` / ``error`` status.
    """
    source, source_options = resolve_currency(from_currency)
    if source_options:
        return _ambiguous("from_currency", from_currency.strip(), source_options)

    target, target_options = resolve_currency(to_currency)
    if target_options:
        return _ambiguous("to_currency", to_currency.strip(), target_options)

    unknown = [
        term.strip()
        for term, code in ((from_currency, source), (to_currency, target))
        if not code
    ]
    if unknown:
        return {
            "status": "error",
            "error": f"Unsupported currency: {', '.join(repr(u) for u in unknown)}.",
            "supported": ", ".join(supported_currencies()),
        }

    usd_value = abs(amount) * _USD_PER_UNIT[source]
    if usd_value > LARGE_AMOUNT_USD and not confirmed:
        return {
            "status": NEEDS_CONFIRMATION,
            "reason": "large_amount",
            "amount": _quantize(amount, source),
            "from_currency": source,
            "to_currency": target,
            "usd_equivalent": f"{usd_value:,.2f}",
            "threshold_usd": f"{LARGE_AMOUNT_USD:,.2f}",
            "question": (
                f"That is {_quantize(amount, source)} {source}, about "
                f"${usd_value:,.2f} -- over the ${LARGE_AMOUNT_USD:,.0f} "
                f"threshold this desk double-checks. Confirm the amount is "
                f"right and I should convert it to {target}?"
            ),
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
        "supported": ", ".join(_describe(code) for code in supported_currencies()),
        "count": str(len(_USD_PER_UNIT)),
        "rate_source": RATE_SOURCE,
        "as_of": RATES_AS_OF,
        "threshold_usd": f"{LARGE_AMOUNT_USD:,.2f}",
    }
