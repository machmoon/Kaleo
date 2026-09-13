"""Money and compute are integers, and the rounding direction is deliberate."""

from __future__ import annotations

import pytest

from billing.units import (
    MKCU_PER_KCU,
    Meter,
    cents_to_display,
    mkcu_to_display,
    price_mkcu,
)


def test_a_minute_of_engine_time_is_one_kcu():
    assert Meter(engine_seconds=60).as_mkcu() == MKCU_PER_KCU


def test_sub_second_usage_still_costs_something():
    """Rounds up. A meter that floors lets a long tail of runs be free."""
    assert Meter(engine_seconds=0.001).as_mkcu() >= 1


def test_price_rounds_up_never_down():
    # 1 mKCU at 225c/KCU is 0.225c, which must bill as 1c, not 0c.
    assert price_mkcu(1, rate_cents_per_kcu=225) == 1
    assert price_mkcu(MKCU_PER_KCU, rate_cents_per_kcu=225) == 225
    assert price_mkcu(0, rate_cents_per_kcu=225) == 0


def test_price_is_exact_at_scale():
    """The float trap this module exists to avoid: 0.1 + 0.2 != 0.3.

    A thousand runs of a third of a KCU must cost exactly the same as the
    arithmetic says, with no accumulated drift.
    """
    total = sum(price_mkcu(333, rate_cents_per_kcu=225) for _ in range(1000))
    assert total == 1000 * 75  # 333 * 225 / 1000 = 74.925 -> 75c each, exactly
    assert isinstance(total, int)


def test_negative_inputs_raise_rather_than_credit():
    with pytest.raises(ValueError):
        price_mkcu(-1, rate_cents_per_kcu=225)
    with pytest.raises(ValueError):
        Meter(engine_seconds=-1).as_mkcu()


def test_display_helpers_are_presentation_only():
    assert mkcu_to_display(1500) == "1.5 KCU"
    assert mkcu_to_display(2000) == "2 KCU"
    assert cents_to_display(2000) == "$20.00"
    assert cents_to_display(5) == "$0.05"
