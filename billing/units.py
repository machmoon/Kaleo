"""Compute units and money, both as integers, for the same reason.

``engine/silkscreen/units.py`` makes every dimension an integer nanometre
because float millimetres destroy boards. The identical argument applies here:
0.1 + 0.2 != 0.3, and a balance that drifts by a thousandth of a cent per
transaction is a reconciliation bug that surfaces months later as "we cannot
explain this number".

So:

* money is **integer cents**, never a float and never a Decimal string;
* compute is **integer milli-KCU** (mKCU), 1 KCU = 1000 mKCU.

One KCU is one minute of engine wall-clock attributable to a run -- the same
shape as Devin's ACU (~15 minutes of agent work), scaled to the fact that a
Kaleo run is minutes rather than a quarter hour. It is deliberately *time*,
not "one model call": a call that reads four datasheets and a call that
answers "no" cost the same under a per-call meter, and users notice.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "MKCU_PER_KCU",
    "Meter",
    "cents_to_display",
    "kcu",
    "mkcu_to_display",
    "price_mkcu",
]

#: One KCU in milli-KCU. All arithmetic happens in mKCU.
MKCU_PER_KCU = 1000

#: Seconds of attributable engine wall-clock per KCU.
SECONDS_PER_KCU = 60


def kcu(whole: int | float) -> int:
    """A KCU count as mKCU. Accepts a float only at this boundary."""
    return round(whole * MKCU_PER_KCU)


def price_mkcu(amount_mkcu: int, *, rate_cents_per_kcu: int) -> int:
    """Cost in whole cents of ``amount_mkcu``, rounded up.

    Rounds **up**, always. Rounding to nearest lets a long tail of sub-cent
    runs be delivered free, and rounding down does it faster. Up by at most
    one cent per run is the only direction that cannot be farmed.
    """
    if amount_mkcu < 0:
        raise ValueError("amount_mkcu must not be negative")
    if rate_cents_per_kcu < 0:
        raise ValueError("rate_cents_per_kcu must not be negative")
    numerator = amount_mkcu * rate_cents_per_kcu
    return -(-numerator // MKCU_PER_KCU)  # ceil division on ints


def mkcu_to_display(amount_mkcu: int) -> str:
    """``1500`` -> ``'1.5 KCU'``. Presentation only; never parsed back."""
    whole, frac = divmod(abs(amount_mkcu), MKCU_PER_KCU)
    sign = "-" if amount_mkcu < 0 else ""
    if frac == 0:
        return f"{sign}{whole} KCU"
    return f"{sign}{whole}.{frac:03d}".rstrip("0") + " KCU"


def cents_to_display(cents: int, *, currency: str = "usd") -> str:
    """``2000`` -> ``'$20.00'``. Presentation only; never parsed back."""
    sign = "-" if cents < 0 else ""
    whole, rem = divmod(abs(cents), 100)
    symbol = {"usd": "$", "eur": "€", "gbp": "£"}.get(currency.lower(), "")
    return f"{sign}{symbol}{whole}.{rem:02d}"


@dataclass(frozen=True)
class Meter:
    """What one run actually consumed, before it becomes a ledger entry.

    The components are kept rather than folded into a single number so an
    invoice line can say *why* a run cost what it did. `engine_seconds` is
    wall-clock attributable to this run; `model_calls` and `solver_seconds`
    are recorded for explanation and are not independently billed today --
    stating that here so nobody later assumes they were.
    """

    engine_seconds: float
    model_calls: int = 0
    solver_seconds: float = 0.0

    def as_mkcu(self) -> int:
        """Billable mKCU, rounded up to the nearest milli-KCU."""
        if self.engine_seconds < 0:
            raise ValueError("engine_seconds must not be negative")
        numerator = round(self.engine_seconds * MKCU_PER_KCU)
        return -(-numerator // SECONDS_PER_KCU)
