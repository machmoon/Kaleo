"""The app-side Gemini request pacer."""

import pytest

from service.quota import RequestPacer


def test_requests_are_evenly_spaced_at_the_selected_rpm():
    now = [100.0]
    slept = []
    announced = []

    def sleep(delay):
        slept.append(delay)
        now[0] += delay

    pacer = RequestPacer(clock=lambda: now[0], sleep=sleep)

    assert pacer.wait(6, on_wait=announced.append) == 0
    assert pacer.wait(6, on_wait=announced.append) == 10
    assert slept == [10]
    assert announced == [10]


def test_auto_pacing_neither_waits_nor_reserves_a_slot():
    slept = []
    pacer = RequestPacer(clock=lambda: 0, sleep=slept.append)

    assert pacer.wait(None) == 0
    assert pacer.wait(None) == 0
    assert slept == []


@pytest.mark.parametrize("rpm", [0, -1, True])
def test_invalid_direct_pacer_values_are_rejected(rpm):
    with pytest.raises(ValueError, match="positive integer"):
        RequestPacer().wait(rpm)
