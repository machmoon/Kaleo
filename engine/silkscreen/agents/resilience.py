"""Provider failover.

The previous project advertised "multiple backups to ensure zero single points
of failure" and shipped fallbacks that had never been executed: one returned a
streaming iterator to a caller expecting a string, another read a response
field that did not exist. A fallback path that is never exercised is not a
backup, it is a second bug waiting for the first one to happen.

So two rules here. Every provider's output is validated before it is accepted,
because "it returned something" and "it returned usable text" are different
claims. And every fallback path has a test that forces the primary to fail.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .model import Document, Model, ModelError

__all__ = [
    "Attempt",
    "FallbackModel",
    "AllProvidersFailed",
    "Provider",
    "PROVIDER_DOWN_MARKERS",
    "DEFAULT_QUOTA_COOLDOWN_S",
    "MAX_QUOTA_COOLDOWN_S",
    "provider_is_down",
    "retry_delay_s",
]


#: Errors that mean the provider has no capacity left to give *anyone*, as
#: opposed to this one request having gone wrong.
#:
#: The distinction earns its code because the two want opposite handling. A
#: timeout, a transport blip, an empty response or a malformed body is about
#: this request, and asking the same provider again is exactly right -- that is
#: what ``Provider.attempts`` is for. An exhausted quota is not about the
#: request at all: the refusal carries its own ``retryDelay``, and it is
#: measured in tens of seconds, so the sub-second backoff below cannot outlast
#: it. Spending the remaining attempts there buys a guaranteed second refusal
#: and, on a metered key, pays for it.
#:
#: Measured on the desktop overlay's propose step, 2026-09-07, against
#: ``gemini-3.7-flash`` on a free-tier key whose 20-requests-per-day cap for
#: that model had run out: seven in-provider retries after a
#: ``RESOURCE_EXHAUSTED``, seven more refusals, ``retryDelay`` between 40 s and
#: 49 s each time. One overlay propose is three model calls, so at two attempts
#: apiece it spent six of that daily allowance and could serve three runs a day.
#:
#: Deliberately NOT here: ``UNAVAILABLE`` (503, "this model is currently
#: experiencing high demand"). It looks like the same thing and is not. In the
#: same session the retry after a 503 succeeded once in two -- a real coin
#: flip, worth the 0.5 s -- and the alternative is answering from a weaker
#: model. A 503 is transient load; only the quota refusal is a wall.
#:
#: Nothing is hidden by the skip: the failed attempt is still recorded in
#: :attr:`FallbackModel.log`, still reported as a ``model.retry`` event, and
#: still named in :class:`AllProvidersFailed`.
#:
#: Matched on the API's own status word rather than the numeric code. "429"
#: turns up in plenty of unrelated text (a URL, a part number, a nested message
#: body), whereas ``RESOURCE_EXHAUSTED`` is the gRPC status string google-genai
#: puts in the error body it stringifies.
PROVIDER_DOWN_MARKERS = ("RESOURCE_EXHAUSTED",)


def provider_is_down(error: str) -> bool:
    """True when ``error`` says the provider has no quota left to serve."""
    return any(marker in error for marker in PROVIDER_DOWN_MARKERS)


#: How long a quota-refused provider is left alone when its refusal named no
#: delay of its own. Measured against the free tier on 2026-09-08: the refusal
#: for ``gemini-3.7-flash`` carried ``retryDelay: '29s'``, and the docstring
#: above records 40-49 s from an earlier session, so this is the conservative
#: end of what the API itself asks for.
DEFAULT_QUOTA_COOLDOWN_S = 30.0

#: The ceiling on a delay read out of an error body. A refusal is data from the
#: network, and a caller must not be able to park a provider for an hour by
#: returning a large number: past this the cooldown is capped and the provider
#: is probed again. Five minutes is far longer than any observed ``retryDelay``
#: and far shorter than a session.
MAX_QUOTA_COOLDOWN_S = 300.0

#: ``Please retry in 29.82992956s`` and ``'retryDelay': '29s'`` -- the two
#: spellings the Gemini refusal body carries for the same number.
_RETRY_DELAY_PATTERNS = (
    re.compile(r"retry in\s+([0-9]+(?:\.[0-9]+)?)\s*s", re.IGNORECASE),
    re.compile(r"retryDelay['\"]?\s*[:=]\s*['\"]?([0-9]+(?:\.[0-9]+)?)s"),
)


def retry_delay_s(error: str) -> float | None:
    """The delay the provider asked for, in seconds, or ``None``.

    Honouring the server's own number rather than inventing a backoff is the
    same rule urllib3 applies to ``Retry-After``
    (``urllib3/util/retry.py:349`` ``Retry.get_retry_after``, used by
    ``sleep_for_retry`` at :349-365 in urllib3 2.x): the endpoint knows when it
    will serve again and the client does not. The value is *read* here and
    clamped by the caller; it is never slept on, because unlike urllib3's case
    there is another provider to try immediately.
    """
    for pattern in _RETRY_DELAY_PATTERNS:
        found = pattern.search(error)
        if found:
            try:
                value = float(found.group(1))
            except ValueError:  # pragma: no cover - the regex admits only floats
                continue
            if value > 0:
                return value
    return None


class AllProvidersFailed(ModelError):
    """Every provider in the chain failed. Carries all of their errors."""

    def __init__(self, attempts: list[Attempt]):
        self.attempts = attempts
        detail = "; ".join(f"{a.provider}: {a.error}" for a in attempts if a.error)
        super().__init__(f"all {len(attempts)} attempts failed -- {detail}")


@dataclass(frozen=True)
class Provider:
    """A named model, plus how many times it is worth retrying."""

    name: str
    model: Model
    attempts: int = 2

    def __post_init__(self) -> None:
        if self.attempts < 1:
            raise ValueError("attempts must be at least 1")


@dataclass(frozen=True)
class Attempt:
    """One call against one provider, successful or not."""

    provider: str
    ok: bool
    error: str | None = None
    elapsed_s: float = 0.0


def _validate(text: object, provider: str) -> str:
    """Accept only non-empty text.

    This is the check the old code skipped. A provider returning a generator, a
    response object, or an empty string is a failure -- surfacing it here sends
    the chain to the next provider instead of handing a caller something it
    cannot use.
    """
    if isinstance(text, str):
        if text.strip():
            return text
        raise ModelError(f"{provider} returned empty text")
    raise ModelError(
        f"{provider} returned {type(text).__name__}, expected str"
    )


@dataclass
class FallbackModel:
    """Tries each provider in order and returns the first usable response.

    Satisfies the :class:`~silkscreen.agents.model.Model` protocol, so it drops
    in anywhere a single model does.
    """

    providers: list[Provider]
    backoff_s: float = 0.5
    max_backoff_s: float = 8.0
    #: Appended to on every call, successful or not. Read it to see which
    #: provider actually served a request.
    log: list[Attempt] = field(default_factory=list)
    _sleep: Callable[[float], None] = time.sleep
    #: Called immediately before every provider attempt. Services can use this
    #: to coordinate quota pacing across retries without changing providers.
    before_attempt: Callable[[str], None] | None = None
    #: Injected so a test can drive the quota cooldown below without sleeping.
    #: Separate from ``_sleep`` because the cooldown never sleeps: it is read.
    _clock: Callable[[], float] = time.monotonic
    #: ``provider name -> the clock reading before which another attempt is
    #: pointless``. Written when a provider answers ``RESOURCE_EXHAUSTED``,
    #: read at the top of every call. Process-local and never persisted: a
    #: daily quota that resets while this object lives is picked up by the
    #: re-probe that follows the cooldown, and a new process starts clean.
    _cooldown: dict[str, float] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if not self.providers:
            raise ValueError("FallbackModel needs at least one provider")

    def _eligible(self) -> tuple[list[Provider], dict[str, float]]:
        """``(providers to try, {skipped name: seconds left})``.

        A provider that refused for quota is skipped **without a round trip**
        until its cooldown expires. :func:`provider_is_down` already stops the
        *remaining attempts of one call*; this stops the *next call* from
        re-asking a provider that has just said it has nothing left, which on
        a free-tier key is every call for the rest of the day. Measured
        2026-09-08 against the exhausted ``gemini-3.7-flash`` daily cap: the
        refusal costs 0.18 s (median of six), and a board run makes five or six
        model calls, so about a second per run is spent learning the same fact
        over and over.

        **Never returns an empty list.** If every provider is cooling, all of
        them are returned and tried for real: a manufactured failure that
        never asked anybody is worse than a slow answer, and the whole point of
        the last rung is that it is a different family whose quota may be
        fine. That is also what keeps this from turning a transient
        misclassification into a dead model object.
        """
        now = self._clock()
        ready = [p for p in self.providers if self._cooldown.get(p.name, 0.0) <= now]
        if not ready:
            self._cooldown.clear()
            return list(self.providers), {}
        skipped = {
            p.name: self._cooldown[p.name] - now
            for p in self.providers
            if p.name not in {r.name for r in ready}
        }
        return ready, skipped

    @property
    def last_provider(self) -> str | None:
        """Which provider served the most recent successful call."""
        for attempt in reversed(self.log):
            if attempt.ok:
                return attempt.provider
        return None

    @property
    def last_model(self) -> str | None:
        """Concrete model id behind :attr:`last_provider`, when it exposes one."""
        served = self.last_provider
        for provider in self.providers:
            if provider.name == served:
                value = getattr(provider.model, "model", None)
                return str(value) if value else None
        return None

    def for_tier(self, model: str) -> FallbackModel | None:
        """The same failover chain, one tier down where a rung can go there.

        Every rung that offers the tier is swapped to it; a rung that does not
        (an open-weights fallback, say) is kept exactly as it was, because the
        point of the last rung is that it is a *different family* and would
        not share the Gemini tiers' outage or quota pool. Rungs that collapse
        onto the same model id after the swap are folded into the first of
        them: a chain that retries flash-lite and then flash-lite is not
        failover, it is the same request twice, and it would double the
        attempts budget by accident.

        ``None`` when nothing moved, which
        :func:`silkscreen.agents.effort.cheap_sibling` reads as "this model
        offers no cheaper tier" and reports rather than papers over.
        """
        swapped: list[Provider] = []
        moved = False
        for provider in self.providers:
            maker = getattr(provider.model, "for_tier", None)
            sibling = maker(model) if callable(maker) else None
            if sibling is None:
                swapped.append(provider)
                continue
            moved = True
            swapped.append(
                Provider(
                    name=f"{provider.name}-{model}",
                    model=sibling,
                    attempts=provider.attempts,
                )
            )
        if not moved:
            return None
        folded: list[Provider] = []
        seen: set[str] = set()
        for provider in swapped:
            key = str(getattr(provider.model, "model", "")) or provider.name
            if key in seen:
                continue
            seen.add(key)
            folded.append(provider)
        return FallbackModel(
            providers=folded,
            backoff_s=self.backoff_s,
            max_backoff_s=self.max_backoff_s,
            _sleep=self._sleep,
            before_attempt=self.before_attempt,
            # The clock travels, the cooldowns do not: the sibling chain is a
            # different set of model ids with its own quota, so inheriting
            # "flash is out" would park a tier nobody had asked yet.
            _clock=self._clock,
        )

    def generate(
        self,
        prompt: str,
        *,
        documents: list[Document] | None = None,
        system: str | None = None,
        temperature: float = 0.0,
        max_output_tokens: int = 8192,
    ) -> str:
        attempts: list[Attempt] = []
        ready, skipped = self._eligible()
        # Recorded, not swallowed. These ride ``FallbackModel.log`` like every
        # other attempt, so they surface as ``model.retry`` events and are
        # named in ``AllProvidersFailed`` -- a provider that was never asked
        # must not look like one that answered.
        for name, remaining in skipped.items():
            attempt = Attempt(
                provider=name,
                ok=False,
                error=(
                    "skipped: quota refused within the last "
                    f"{DEFAULT_QUOTA_COOLDOWN_S:g}s; {remaining:.1f}s before "
                    "it is asked again"
                ),
                elapsed_s=0.0,
            )
            attempts.append(attempt)
            self.log.append(attempt)
        for provider in ready:
            for try_no in range(provider.attempts):
                if self.before_attempt is not None:
                    self.before_attempt(provider.name)
                started = time.monotonic()
                try:
                    raw = provider.model.generate(
                        prompt,
                        documents=documents,
                        system=system,
                        temperature=temperature,
                        max_output_tokens=max_output_tokens,
                    )
                    text = _validate(raw, provider.name)
                except Exception as exc:
                    attempt = Attempt(
                        provider=provider.name,
                        ok=False,
                        error=f"{type(exc).__name__}: {exc}",
                        elapsed_s=time.monotonic() - started,
                    )
                    attempts.append(attempt)
                    self.log.append(attempt)
                    if provider_is_down(attempt.error or ""):
                        # Its remaining attempts would be spent on a provider
                        # that has just said it has nothing left to give, and
                        # said so with a retry delay this backoff cannot reach.
                        # Move on without sleeping either: the wait was for
                        # this provider's sake and we are done with it.
                        #
                        # Remember it for the *next* call too, for the same
                        # reason and using the delay the refusal itself named
                        # (:func:`retry_delay_s`) rather than a number invented
                        # here. Clamped so a number read off the network cannot
                        # park a provider indefinitely.
                        asked = retry_delay_s(attempt.error or "")
                        self._cooldown[provider.name] = self._clock() + min(
                            DEFAULT_QUOTA_COOLDOWN_S if asked is None else asked,
                            MAX_QUOTA_COOLDOWN_S,
                        )
                        break
                    if try_no + 1 < provider.attempts:
                        self._sleep(
                            min(self.backoff_s * (2**try_no), self.max_backoff_s)
                        )
                    continue

                attempt = Attempt(
                    provider=provider.name,
                    ok=True,
                    elapsed_s=time.monotonic() - started,
                )
                attempts.append(attempt)
                self.log.append(attempt)
                return text

        raise AllProvidersFailed(attempts)
