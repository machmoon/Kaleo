"""Process-wide pacing for outbound Gemini generation requests."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

__all__ = ["GEMINI_REQUEST_PACER", "RequestPacer"]


class RequestPacer:
    """Space request starts so concurrent runs share one RPM ceiling.

    This is deliberately a pacer rather than a quota counter. Google applies
    RPM, TPM, and daily limits per project; only Google knows the latter two.
    A minimum interval is conservative for RPM and avoids the burst that a
    simple per-minute counter would still allow at a window boundary.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next_at = 0.0

    def wait(
        self,
        rpm: int | None,
        *,
        on_wait: Callable[[float], None] | None = None,
    ) -> float:
        """Wait for a slot and return the number of seconds requested."""
        if rpm is None:
            return 0.0
        if isinstance(rpm, bool) or rpm <= 0:
            raise ValueError("rpm must be a positive integer or None")

        interval = 60.0 / rpm
        with self._lock:
            delay = max(0.0, self._next_at - self._clock())
            if delay:
                if on_wait is not None:
                    on_wait(delay)
                self._sleep(delay)
            self._next_at = self._clock() + interval
        return delay


# Cloud Run can serve multiple requests in one process. A per-handler gate
# would let every browser tab spend the same quota at once, so they share this.
GEMINI_REQUEST_PACER = RequestPacer()
