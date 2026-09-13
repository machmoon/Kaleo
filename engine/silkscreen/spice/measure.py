"""Measurements over a simulation result.

These are the quantities a specification is written in: rise time, ripple, gain,
the peak a node reaches. A :class:`Measurement` is a declarative description of
one -- a kind, a signal, and a window -- rather than a callback, so it survives
a round trip through JSON and an agent can assemble one without writing Python.

Every measurement either returns a number that was actually computed from data,
or raises :class:`~silkscreen.spice.errors.MeasurementError`. Asking for the rise
time of a signal that never rises has no answer, and 0.0 is not one; a caller in
a loop would read that as an infinitely fast edge.

Where a definition has choices, this module makes the deterministic one and says
so in the docstring. Rise time uses the min and max *within the window* as the
rails, not the initial and final values, so a waveform with several edges gives a
number about the edge the caller windowed to rather than a number about the whole
trace.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .errors import MeasurementError
from .result import SimulationResult

__all__ = ["Measurement", "MEASUREMENT_KINDS", "measure"]


@dataclass(frozen=True)
class Measurement:
    """One quantity to extract from a result.

    ``window`` restricts every kind to a span of the independent variable
    (seconds for a transient, hertz for an AC sweep). ``at`` picks a single
    point, interpolated between samples.
    """

    kind: str
    signal: str
    reference: str | None = None
    window: tuple[float, float] | None = None
    at: float | None = None
    low_pct: float = 0.1
    high_pct: float = 0.9
    tolerance: float = 0.02

    def describe(self) -> str:
        text = f"{self.kind}({self.signal}"
        if self.reference:
            text += f"/{self.reference}"
        text += ")"
        if self.at is not None:
            text += f" at {self.at:g}"
        if self.window:
            text += f" over [{self.window[0]:g}, {self.window[1]:g}]"
        return text


def _windowed(
    result: SimulationResult, m: Measurement
) -> tuple[tuple[float, ...], tuple[float, ...]]:
    """Sweep and magnitude arrays for the measurement's signal and window."""
    values = result.magnitude(m.signal)
    sweep = result.sweep
    if len(sweep) != len(values):
        raise MeasurementError(
            f"{m.signal!r} has {len(values)} points but the "
            f"{result.sweep_name} axis has {len(sweep)}"
        )
    if m.window is None:
        if not values:
            raise MeasurementError(f"{m.signal!r} has no data points")
        return sweep, values

    low, high = m.window
    if high <= low:
        raise MeasurementError(f"window [{low:g}, {high:g}] is empty or reversed")
    kept = [(s, v) for s, v in zip(sweep, values, strict=True) if low <= s <= high]
    if not kept:
        raise MeasurementError(
            f"window [{low:g}, {high:g}] contains no simulated points; the "
            f"{result.sweep_name} axis spans "
            f"[{sweep[0]:g}, {sweep[-1]:g}]"
        )
    return tuple(s for s, _ in kept), tuple(v for _, v in kept)


def _interpolate(
    sweep: tuple[float, ...], values: tuple[float, ...], x: float
) -> float:
    if not sweep:
        raise MeasurementError("cannot interpolate an empty signal")
    if x < sweep[0] or x > sweep[-1]:
        raise MeasurementError(
            f"{x:g} is outside the simulated range [{sweep[0]:g}, {sweep[-1]:g}]"
        )
    for index in range(1, len(sweep)):
        if sweep[index] >= x:
            x0, x1 = sweep[index - 1], sweep[index]
            y0, y1 = values[index - 1], values[index]
            if x1 == x0:
                return y1
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return values[-1]


def _crossing(
    sweep: tuple[float, ...],
    values: tuple[float, ...],
    threshold: float,
    *,
    rising: bool,
    start_index: int = 0,
) -> tuple[float, int]:
    """First crossing of ``threshold`` at or after ``start_index``.

    Returns the interpolated sweep position and the index just past it.
    """
    for index in range(max(start_index, 1), len(values)):
        previous, current = values[index - 1], values[index]
        crossed = (
            previous < threshold <= current
            if rising
            else previous > threshold >= current
        )
        if crossed:
            span = current - previous
            fraction = 0.0 if span == 0 else (threshold - previous) / span
            x0, x1 = sweep[index - 1], sweep[index]
            return x0 + (x1 - x0) * fraction, index
    direction = "rising" if rising else "falling"
    raise MeasurementError(
        f"signal never crosses {threshold:g} {direction} within the window "
        f"(it spans [{min(values):g}, {max(values):g}])"
    )


# --------------------------------------------------------------------------
# Kinds
# --------------------------------------------------------------------------


def _final(result: SimulationResult, m: Measurement) -> float:
    _, values = _windowed(result, m)
    return values[-1]


def _initial(result: SimulationResult, m: Measurement) -> float:
    _, values = _windowed(result, m)
    return values[0]


def _max(result: SimulationResult, m: Measurement) -> float:
    _, values = _windowed(result, m)
    return max(values)


def _min(result: SimulationResult, m: Measurement) -> float:
    _, values = _windowed(result, m)
    return min(values)


def _peak_to_peak(result: SimulationResult, m: Measurement) -> float:
    _, values = _windowed(result, m)
    return max(values) - min(values)


def _mean(result: SimulationResult, m: Measurement) -> float:
    _, values = _windowed(result, m)
    return sum(values) / len(values)


def _rms(result: SimulationResult, m: Measurement) -> float:
    _, values = _windowed(result, m)
    return math.sqrt(sum(v * v for v in values) / len(values))


def _value_at(result: SimulationResult, m: Measurement) -> float:
    if m.at is None:
        raise MeasurementError("value_at needs Measurement.at")
    sweep, values = _windowed(result, m)
    return _interpolate(sweep, values, m.at)


def _abs_max(result: SimulationResult, m: Measurement) -> float:
    """Largest magnitude reached -- the absolute-maximum-rating check."""
    _, values = _windowed(result, m)
    return max(abs(v) for v in values)


def _rise_time(result: SimulationResult, m: Measurement) -> float:
    sweep, values = _windowed(result, m)
    low_rail, high_rail = min(values), max(values)
    if high_rail == low_rail:
        raise MeasurementError(
            f"{m.signal!r} is flat at {low_rail:g} over the window; it has no "
            f"rising edge to measure"
        )
    span = high_rail - low_rail
    low = low_rail + m.low_pct * span
    high = low_rail + m.high_pct * span
    t_low, index = _crossing(sweep, values, low, rising=True)
    t_high, _ = _crossing(sweep, values, high, rising=True, start_index=index)
    return t_high - t_low


def _fall_time(result: SimulationResult, m: Measurement) -> float:
    sweep, values = _windowed(result, m)
    low_rail, high_rail = min(values), max(values)
    if high_rail == low_rail:
        raise MeasurementError(
            f"{m.signal!r} is flat at {low_rail:g} over the window; it has no "
            f"falling edge to measure"
        )
    span = high_rail - low_rail
    low = low_rail + m.low_pct * span
    high = low_rail + m.high_pct * span
    t_high, index = _crossing(sweep, values, high, rising=False)
    t_low, _ = _crossing(sweep, values, low, rising=False, start_index=index)
    return t_low - t_high


def _settling_time(result: SimulationResult, m: Measurement) -> float:
    """Time from the window start until the signal stays within
    ``tolerance`` (fractional) of its final value for the rest of the window."""
    sweep, values = _windowed(result, m)
    final = values[-1]
    band = abs(final) * m.tolerance
    if band == 0:
        band = m.tolerance
    settled_index = 0
    for index in range(len(values) - 1, -1, -1):
        if abs(values[index] - final) > band:
            settled_index = index + 1
            break
    # Every signal is trivially "within band of its final value" at the final
    # sample, so a settling point at the very last point means the signal was
    # still moving when the window ended. Reporting the window length as a
    # settling time there would be a number with no meaning.
    if settled_index >= len(values) - 1:
        raise MeasurementError(
            f"{m.signal!r} is still outside ±{band:g} of its final value "
            f"{final:g} one sample before the window ends; it has not settled. "
            f"Simulate for longer, or widen the tolerance."
        )
    return sweep[settled_index] - sweep[0]


def _gain(result: SimulationResult, m: Measurement) -> float:
    """Linear magnitude ratio of ``signal`` to ``reference`` at ``at``.

    Without a reference this is the signal's own magnitude, which is the gain
    directly when the stimulus is a 1 V AC probe.
    """
    sweep, values = _windowed(result, m)
    numerator = _interpolate(sweep, values, m.at) if m.at is not None else values[-1]
    if m.reference is None:
        return numerator
    ref = Measurement(kind=m.kind, signal=m.reference, window=m.window, at=m.at)
    ref_sweep, ref_values = _windowed(result, ref)
    denominator = (
        _interpolate(ref_sweep, ref_values, m.at)
        if m.at is not None
        else ref_values[-1]
    )
    if denominator == 0:
        raise MeasurementError(
            f"reference {m.reference!r} is zero; gain is undefined there"
        )
    return numerator / denominator


def _gain_db(result: SimulationResult, m: Measurement) -> float:
    ratio = _gain(result, m)
    if ratio <= 0:
        raise MeasurementError(
            f"gain magnitude is {ratio:g}; cannot express as dB"
        )
    return 20.0 * math.log10(ratio)


def _bandwidth_3db(result: SimulationResult, m: Measurement) -> float:
    """Frequency at which the response first falls 3 dB below its peak.

    Searches upward in frequency from the peak, so this is the upper corner of a
    low-pass or band-pass response. Requires an AC result.
    """
    if result.analysis != "ac":
        raise MeasurementError(
            f"bandwidth_3db needs an AC sweep; this is a {result.analysis} result"
        )
    sweep, values = _windowed(result, m)
    if m.reference is not None:
        reference = Measurement(
            kind=m.kind,
            signal=m.reference,
            window=m.window,
        )
        ref_sweep, ref_values = _windowed(result, reference)
        if ref_sweep != sweep:
            raise MeasurementError(
                f"reference {m.reference!r} does not share the same frequency axis"
            )
        for frequency, ref_value in zip(sweep, ref_values, strict=True):
            if ref_value == 0:
                raise MeasurementError(
                    f"reference {m.reference!r} is zero at {frequency:g} Hz; "
                    "bandwidth is undefined there"
                )
        values = tuple(
            value / ref_value
            for value, ref_value in zip(values, ref_values, strict=True)
        )
    peak = max(values)
    if peak <= 0:
        raise MeasurementError("response is zero everywhere; no corner frequency")
    peak_index = values.index(peak)
    threshold = peak / math.sqrt(2.0)
    tail = values[peak_index:]
    if min(tail) > threshold:
        raise MeasurementError(
            f"response never falls to -3 dB within the swept range "
            f"[{sweep[0]:g}, {sweep[-1]:g}] Hz; widen the sweep"
        )
    corner, _ = _crossing(
        sweep[peak_index:], tail, threshold, rising=False
    )
    return corner


def _mid_crossings(
    result: SimulationResult, m: Measurement
) -> tuple[list[float], list[float]]:
    """``(rising, falling)`` times where the signal crosses its own midpoint.

    The midpoint and the hysteresis band come from the extremes *within the
    window*, the same choice :func:`_rise_time` makes, so a caller windows to
    the part of the trace it means. The band is 10% of the swing on each side
    and a crossing only counts once the signal has been all the way past the
    opposite edge, which is what stops ringing on one edge from being counted
    as several cycles.

    A flat signal raises rather than reporting zero crossings and letting the
    caller divide by them: an oscillator that did not start is a result, and
    "period 0" is not the way to say it.
    """
    sweep, values = _windowed(result, m)
    if len(values) < 2:
        raise MeasurementError(
            f"{m.signal!r} has {len(values)} point(s) in the window; a period "
            f"needs a waveform"
        )
    low, high = min(values), max(values)
    swing = high - low
    if swing <= 0:
        raise MeasurementError(
            f"{m.signal!r} is flat at {low:g} over the window; it does not "
            f"oscillate, so it has no period"
        )
    mid = (low + high) / 2.0
    band = 0.1 * swing
    rising: list[float] = []
    falling: list[float] = []
    armed_up = values[0] < mid - band
    armed_down = values[0] > mid + band
    for index in range(1, len(values)):
        previous, current = values[index - 1], values[index]
        if armed_up and previous < mid <= current:
            span = current - previous
            fraction = 0.0 if span == 0 else (mid - previous) / span
            rising.append(
                sweep[index - 1] + (sweep[index] - sweep[index - 1]) * fraction
            )
            armed_up, armed_down = False, False
        elif armed_down and previous > mid >= current:
            span = current - previous
            fraction = 0.0 if span == 0 else (mid - previous) / span
            falling.append(
                sweep[index - 1] + (sweep[index] - sweep[index - 1]) * fraction
            )
            armed_up, armed_down = False, False
        if current < mid - band:
            armed_up = True
        elif current > mid + band:
            armed_down = True
    return rising, falling


def _period(result: SimulationResult, m: Measurement) -> float:
    """Mean time between successive midpoint crossings in the same direction.

    Averaged over every whole cycle *in the window* rather than taken from the
    first pair of crossings, so a few samples of jitter do not become the
    answer. The averaging does not rescue a badly chosen window: an RC-timed
    oscillator's first cycle starts from a different initial condition than
    every cycle after it -- a 555 astable's first charge runs from 0 V rather
    than from one third of the supply -- so a caller measuring a startup
    transient gets a number that is wrong by a fixed, plausible-looking amount.
    Window past the startup; that is what ``window`` is for.
    """
    rising, _ = _mid_crossings(result, m)
    if len(rising) < 2:
        raise MeasurementError(
            f"{m.signal!r} crosses its midpoint upward {len(rising)} time(s) "
            f"in the window; a period needs at least two, so either it does "
            f"not oscillate or the window is shorter than one cycle"
        )
    return (rising[-1] - rising[0]) / (len(rising) - 1)


def _frequency(result: SimulationResult, m: Measurement) -> float:
    """One over :func:`_period`. Raises where the period raises."""
    return 1.0 / _period(result, m)


def _duty_cycle(result: SimulationResult, m: Measurement) -> float:
    """Fraction of each cycle spent above the midpoint, 0 to 1.

    Measured from the first rising crossing to the falling crossing after it,
    averaged over the whole cycles in the window. Returned as a fraction, not
    a percentage, so a clause reads ``op="within", value=2/3`` -- which is the
    number closed-form theory gives, without a factor of a hundred in the way.
    """
    rising, falling = _mid_crossings(result, m)
    if len(rising) < 2:
        raise MeasurementError(
            f"{m.signal!r} crosses its midpoint upward {len(rising)} time(s) "
            f"in the window; a duty cycle needs at least two whole cycles"
        )
    highs: list[float] = []
    for index in range(len(rising) - 1):
        start, end = rising[index], rising[index + 1]
        drop = next((f for f in falling if start < f < end), None)
        if drop is None:
            raise MeasurementError(
                f"{m.signal!r} rises through its midpoint at {start:g} and "
                f"again at {end:g} without falling through it in between; the "
                f"waveform is not a clean two-state cycle"
            )
        highs.append((drop - start) / (end - start))
    return sum(highs) / len(highs)


#: Every measurement kind, by name. An agent can enumerate this.
MEASUREMENT_KINDS: dict[str, object] = {
    "final": _final,
    "initial": _initial,
    "max": _max,
    "min": _min,
    "peak_to_peak": _peak_to_peak,
    "mean": _mean,
    "rms": _rms,
    "value_at": _value_at,
    "abs_max": _abs_max,
    "rise_time": _rise_time,
    "fall_time": _fall_time,
    "settling_time": _settling_time,
    "period": _period,
    "frequency": _frequency,
    "duty_cycle": _duty_cycle,
    "gain": _gain,
    "gain_db": _gain_db,
    "bandwidth_3db": _bandwidth_3db,
}


def measure(result: SimulationResult, m: Measurement) -> float:
    """Compute one measurement. Raises :class:`MeasurementError` if it has no
    answer -- never returns a placeholder."""
    func = MEASUREMENT_KINDS.get(m.kind)
    if func is None:
        raise MeasurementError(
            f"unknown measurement kind {m.kind!r}; "
            f"known: {sorted(MEASUREMENT_KINDS)}"
        )
    return func(result, m)  # type: ignore[operator]
