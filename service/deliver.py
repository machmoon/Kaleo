"""Delivering a step session to Google Workspace (``googleapps/``).

The desktop overlay runs the pipeline as approval-gated steps
(``service/steps.py``). Once the copper is down, the engineer may want the
result where the team already looks -- a Chat space, an inbox with the board
attached, a review on the calendar -- and ``python -m googleapps run`` cannot
do that for a run that already happened. These two routes can:

    GET  /deliver/config          what is configured; never a secret
    POST /deliver/auth/start      consent URL for Ada to open (client opens it)
    POST /deliver/auth            finish Ada sign-in, or all-in-one server open
    POST /steps/<id>/deliver      send the named session to Chat / Gmail / Calendar,
                                  or book a spec review (agenda + Meet link)

This module is an adapter and nothing more. The card, the email and the event
are built by ``googleapps.chat`` / ``gmail`` / ``calendar`` from a
``PipelineResult``-shaped object, and :class:`SessionResult` presents a
:class:`~service.steps.Session` in that shape -- the whole surface the package
depends on, listed in one place -- so nothing here reaches into the pipeline
or reimplements a formatter. The package's rules carry over unchanged: every
address is validated before a byte is sent, the Calendar event is created only
when the review found blockers (and it says so either way), and the routing
story names every unrouted net.

Each destination succeeds or fails on its own. One response reports all of
them, HTTP 200 even when one failed, because "the card posted but Gmail
refused the token" is a result, not an error; only bad input (an address that
would not survive a MIME header, nothing asked for) is a 400, and it happens
before any request leaves.

``googleapps`` is imported lazily: the deployed image and the engine wheel
carry ``service/`` without it, and a service that cannot import at all is
worse than one whose delivery routes say "not installed".
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import steps as _steps

__all__ = [
    "SIGN_OUT_NOTE",
    "AuthJob",
    "SessionResult",
    "auth_status",
    "begin_auth",
    "cancel_auth",
    "config_report",
    "deliver",
    "finish_auth",
    "handle_get",
    "handle_post",
    "sign_out",
    "start_auth",
    "transport_factory",
]

#: The test seam, the ``Handler.model_factory`` convention: ``None`` means the
#: real ``urllib`` transport; tests install a recording one.
transport_factory: Callable[[], Any] | None = None

#: The service does not read ``.env`` (only the two CLIs do), and a hint that
#: names the variable without saying so sends the user to a file the service
#: never opens.
_ENV_NOTE = "in the service's environment (the service does not read .env)"

#: Stage lines for the card and the email, in pipeline order.
_STAGE_ORDER = ("propose", "place", "route", "review", "order", "case")

#: The critic was asked and its answer could not be read. The phrase is
#: ``googleapps.chat.verdict``'s, verbatim, so the summary, the subject line,
#: the card and the two calendar refusals describe one fact in one wording.
#: It is not "no blockers": a failed review established nothing, and the
#: only honest thing to schedule off it is nothing.
REVIEW_FAILED_NOTE = "not reviewed — the review failed"
REVIEW_FAILED_CALENDAR_REASON = (
    f"{REVIEW_FAILED_NOTE}, so whether there are blockers is not known — "
    "nothing scheduled"
)
REVIEW_FAILED_SPEC_REVIEW_REASON = (
    f"{REVIEW_FAILED_NOTE}, so what the meeting would decide is not known — "
    "nothing scheduled"
)


def _googleapps():
    """The package, or ``None`` when it is not beside the service."""
    try:
        import googleapps.auth  # noqa: F401 - probing the import
        import googleapps.calendar  # noqa: F401
        import googleapps.chat  # noqa: F401
        import googleapps.config  # noqa: F401
        import googleapps.gmail  # noqa: F401
        import googleapps.runner  # noqa: F401
    except ImportError:
        return None
    return sys.modules["googleapps"]


def _transport():
    if transport_factory is not None:
        return transport_factory()
    from googleapps.transport import urllib_transport

    return urllib_transport()


# ---------------------------------------------------------------- adapter


@dataclass(frozen=True)
class SessionResult:
    """A step session in the shape ``googleapps`` reads a ``PipelineResult``.

    Every attribute the package touches is here and nothing else is, which
    makes this class the record of how much of the pipeline's surface the
    delivery depends on. It is a snapshot: built under the session's lock,
    then used after the lock is released, so a step running concurrently
    cannot change the board halfway through the email.
    """

    intent: str
    spec: Any
    board: Any
    route: Any
    findings: list[Any]
    board_path: Path
    reviewed: bool
    done: tuple[str, ...]
    #: The run's ``silkscreen.specreview.SpecReview``, when something produced
    #: one for this session. ``None`` is the normal case today and is not a
    #: failure: the spec-review destination then builds a deterministic agenda
    #: from the findings and the routing, with no model call.
    spec_review: Any = None
    #: The critic's :class:`~silkscreen.agents.review.ReviewReport`, when the
    #: review step ran. ``reviewed`` alone is true for a critic whose answer
    #: could not be read, and every honesty rule below -- the verdict, the
    #: email, the calendar -- has to tell that apart from a clean board;
    #: ``googleapps.chat.review_state`` reads this field first and falls back
    #: to the flag only for a session that carries no report.
    review: Any = None

    @property
    def blockers(self) -> list[Any]:
        return [f for f in self.findings if str(f.severity.value) == "blocker"]

    def review_state(self) -> str:
        """``ok`` / ``failed`` / ``skipped`` -- ``googleapps.chat.review_state``,
        so this adapter and the package it feeds cannot disagree."""
        from googleapps.chat import review_state

        return review_state(self)

    def summary(self) -> str:
        """The lines ``PipelineResult.summary()`` prints, from the same fields."""
        w, h = self.board.size_mm
        lines = [
            f"{self.spec.part_count()} parts, {self.spec.net_count()} nets",
            f"board {w:.2f} x {h:.2f} mm  [{self.board.solver_status}]",
        ]
        if self.route is not None:
            lines.append(self.route.summary())
        state = self.review_state()
        if state == "failed":
            lines.append(REVIEW_FAILED_NOTE)
        elif state != "ok":
            lines.append("not reviewed (the review step has not run)")
        elif self.findings:
            lines.append(
                f"{len(self.findings)} finding(s), {len(self.blockers)} blocker(s)"
            )
        else:
            lines.append("no findings")
        return "\n".join(lines)

    def stage_lines(self) -> list[str]:
        return [f"{step}: done" for step in _STAGE_ORDER if step in self.done]


def _snapshot(session: _steps.Session) -> SessionResult:
    """Read the session under its lock; raise if it is not deliverable yet."""
    with session.lock:
        if "routed" not in _steps._reached(session.stage):
            raise _steps.StepOrderError(
                "delivery needs the run to be 'routed' (the board file exists "
                f"only after the route step); it is {session.stage!r}"
            )
        board = session.files.get("board")
        if not board or not Path(board).is_file():
            raise _steps.StepOrderError(
                "the routed board file is missing from disk; run the route "
                "step again before delivering"
            )
        return SessionResult(
            intent=session.intent,
            spec=session.spec,
            board=session.board,
            route=session.route,
            findings=list(session.findings),
            board_path=Path(board),
            reviewed="review" in session.done,
            review=session.review,
            done=tuple(sorted(session.done)),
            # getattr, not an attribute access: the session gains this field
            # only once the engine's spec-review stage is wired in, and a
            # missing agenda is a fallback, not a 500.
            spec_review=getattr(session, "spec_review", None),
        )


# ---------------------------------------------------------------- config


def config_report(config: Any | None = None) -> dict[str, Any]:
    """What the service could deliver to right now. Purely local, no secret.

    ``chat`` / ``gmail`` / ``calendar`` say whether each destination has what
    it needs; ``token`` is the stored OAuth token's state; every gap comes
    with a hint naming the exact fix.

    "What it needs" includes the scope. Google's granular consent makes every
    scope on the sign-in screen an independent checkbox, so a signed-in user
    can have Calendar and not Gmail, and a panel that offers both would then
    fail at send time. ``scopes`` is what the token records as granted and
    ``scopes_recorded`` says whether that record exists at all -- a token
    stored before the record did says nothing about the grant, and is
    reported as usable rather than refused on no evidence.
    """
    pkg = _googleapps()
    if pkg is None:
        return {
            "available": False,
            "chat": False,
            "gmail": False,
            "calendar": False,
            "spec_review": False,
            "oauth_client": False,
            "signed_in": False,
            "token": "missing",
            "scopes": [],
            "scopes_recorded": False,
            "hints": [
                "the googleapps package is not installed beside the service; "
                "run the service from a checkout that has googleapps/"
            ],
        }
    from googleapps import auth, chat
    from googleapps.config import load_config
    from googleapps.transport import GoogleError

    config = config if config is not None else load_config(dotenv=False)
    hints: list[str] = []

    chat_ok = False
    if not config.chat_webhook:
        hints.append(f"Google Chat: set GOOGLEAPPS_CHAT_WEBHOOK {_ENV_NOTE}")
    else:
        try:
            chat.validate_webhook(config.chat_webhook)
            chat_ok = True
        except GoogleError as exc:
            hints.append(f"Google Chat: {exc.detail}")

    oauth = bool(config.client_id and config.client_secret)
    if not oauth:
        missing = [
            name
            for name, value in (
                ("GOOGLEAPPS_CLIENT_ID", config.client_id),
                ("GOOGLEAPPS_CLIENT_SECRET", config.client_secret),
            )
            if not value
        ]
        hints.append(f"Gmail and Calendar: set {' and '.join(missing)} {_ENV_NOTE}")

    status = auth.token_status(config.token_path)
    if status == "missing":
        if oauth:
            hints.append(
                "Gmail and Calendar: sign in with Google from Hardy's Send panel "
                f"(or run `python -m googleapps auth`; token at {config.token_path})"
            )
    elif not auth.token_file_is_private(config.token_path):
        hints.append(
            f"the token file {config.token_path} is not mode 0600; "
            "sign in again from Hardy's Send panel to rewrite it"
        )

    signed_in = oauth and status != "missing"

    # What the sign-in actually granted, per destination. ``destination_state``
    # answers granted / denied / unknown, and only ``denied`` closes a
    # destination: an old token file carries no record, and refusing on that
    # would break a working sign-in to fix a hypothetical one.
    stored: dict[str, Any] | None = None
    if status != "missing":
        try:
            stored = auth.load_token(config.token_path)
        except auth.AuthError:  # pragma: no cover - status already read it
            stored = None
    granted = auth.granted_scopes(stored) if stored is not None else None

    def usable(destination: str) -> bool:
        if not signed_in:
            return False
        return auth.destination_state(stored, destination) != "denied"

    if signed_in and stored is not None:
        denied = auth.denied_scopes(stored, auth.SCOPES)
        if denied:
            hints.append(auth.scope_hint(denied))

    return {
        "available": True,
        "chat": chat_ok,
        "gmail": usable("gmail"),
        "calendar": usable("calendar"),
        # The spec review is a Calendar insert with an agenda in it: the same
        # token, the same scope, no new secret to configure. It is reported
        # separately anyway so the panel can offer or grey out the button
        # without knowing that.
        "spec_review": usable("spec_review"),
        "oauth_client": oauth,
        "signed_in": signed_in,
        "token": status,
        # The granted set, verbatim from the token response Google sent, and
        # whether it was recorded at all -- three states stay three, so a
        # panel can say "not recorded" rather than guessing.
        "scopes": list(granted or ()),
        "scopes_recorded": granted is not None,
        "token_path": str(config.token_path),
        "hints": hints,
    }


def start_auth() -> dict[str, Any]:
    """Open Google's consent page in the browser and persist the token.

    Blocks until the user finishes (or declines / times out). The service is
    threaded, so other requests keep answering while the tab is open. Returns
    the same shape as :func:`config_report` so the panel can refresh in one
    round-trip.

    Prefer :func:`begin_auth` + :func:`finish_auth` from Ada: the overlay
    opens the consent URL itself (Tauri ``openUrl``), which is reliable when
    the service process cannot open a browser from a worker thread.
    """
    if _googleapps() is None:
        raise ValueError(
            "sign-in is unavailable: the googleapps package is not installed "
            "beside the service"
        )
    from googleapps import auth
    from googleapps.config import ConfigError, load_config

    try:
        config = load_config(dotenv=False)
        config.require_oauth()
    except ConfigError as exc:
        raise ValueError(str(exc)) from exc

    try:
        auth.run_auth_flow(config, _transport())
    except auth.AuthError as exc:
        raise ValueError(str(exc)) from exc
    return config_report(config)


#: What ``sign_out`` says, verbatim, because "signed out" alone reads as
#: "revoked": deleting the file here leaves the grant standing at Google.
SIGN_OUT_NOTE = (
    "Signed out here: the refresh token was deleted from this Mac, not "
    "revoked at Google. Revoke it at myaccount.google.com/permissions."
)


@dataclass
class AuthJob:
    """One Ada sign-in: URL first, then the finished config -- or the error.

    Kept after it finishes, until the next :func:`begin_auth`, so a poll that
    arrives after the redirect still sees ``connected`` (or ``failed`` with
    its reason) rather than "no sign-in in progress".
    """

    url_ready: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    auth_url: str | None = None
    report: dict[str, Any] | None = None
    error: BaseException | None = None

    @property
    def state(self) -> str:
        """``waiting`` / ``connected`` / ``failed``."""
        if not self.done.is_set():
            return "waiting"
        if self.error is not None or self.report is None:
            return "failed"
        return "connected"

    def status(self) -> dict[str, Any]:
        """The job for a JSON body: never the report, never a token."""
        error = None
        if self.done.is_set():
            if self.error is not None:
                error = str(self.error)
            elif self.report is None:
                error = "Google sign-in finished with no configuration report"
        return {"state": self.state, "error": error, "auth_url": self.auth_url}


#: Kept under its old name for anything that imported it.
_AuthJob = AuthJob

_auth_lock = threading.Lock()
_auth_job: AuthJob | None = None


def auth_status() -> dict[str, Any]:
    """The current sign-in job, or ``idle``. Non-blocking, never a token."""
    with _auth_lock:
        job = _auth_job
    if job is None:
        return {"state": "idle", "error": None, "auth_url": None}
    return job.status()


def cancel_auth() -> dict[str, Any]:
    """Forget the current job so a new sign-in may begin.

    The worker thread cannot be interrupted while it waits on the loopback
    redirect (it times out on its own, and its port was chosen by the
    kernel), so this drops the reference and nothing else: a late redirect
    on the old port writes the token file exactly as it would have, which is
    the same outcome as finishing.
    """
    global _auth_job
    with _auth_lock:
        had = _auth_job is not None
        _auth_job = None
    return {"cancelled": had}


def sign_out(config: Any | None = None) -> dict[str, Any]:
    """Delete the stored token. Local only; the grant stands at Google."""
    global _auth_job
    if _googleapps() is None:
        raise ValueError(
            "sign-out is unavailable: the googleapps package is not installed "
            "beside the service"
        )
    from googleapps.config import load_config

    config = config if config is not None else load_config(dotenv=False)
    path = Path(config.token_path)
    removed = False
    if path.is_symlink():
        raise ValueError(f"{path} is a symlink; refusing to remove through it")
    try:
        path.unlink()
        removed = True
    except FileNotFoundError:
        removed = False
    with _auth_lock:
        job = _auth_job
        if job is not None and job.done.is_set():
            _auth_job = None
    return {
        "signed_out": True,
        "removed": removed,
        "token_path": str(path),
        "note": SIGN_OUT_NOTE,
        "report": config_report(config),
    }


def begin_auth() -> dict[str, Any]:
    """Start OAuth and return the consent URL for Ada to open.

    Does not open a browser here — the overlay calls Tauri ``openUrl`` on
    ``auth_url``, then :func:`finish_auth` to wait for the redirect.
    """
    global _auth_job
    if _googleapps() is None:
        raise ValueError(
            "sign-in is unavailable: the googleapps package is not installed "
            "beside the service"
        )
    from googleapps import auth
    from googleapps.config import ConfigError, load_config

    try:
        config = load_config(dotenv=False)
        config.require_oauth()
    except ConfigError as exc:
        raise ValueError(str(exc)) from exc

    with _auth_lock:
        if _auth_job is not None and not _auth_job.done.is_set():
            raise ValueError(
                "a Google sign-in is already in progress; finish or cancel it "
                "before starting another"
            )
        job = AuthJob()
        _auth_job = job

    def on_url(url: str) -> None:
        job.auth_url = url
        job.url_ready.set()

    def worker() -> None:
        try:
            # Ada opens the URL; a no-op here avoids a second, failing attempt
            # from the service thread.
            auth.run_auth_flow(
                config,
                _transport(),
                open_browser=lambda _url: None,
                on_url=on_url,
            )
            job.report = config_report(config)
        except BaseException as exc:  # noqa: BLE001 - must surface to finish_auth
            job.error = exc
            if not job.url_ready.is_set():
                job.url_ready.set()
        finally:
            job.done.set()

    threading.Thread(target=worker, daemon=True, name="googleapps-auth").start()
    if not job.url_ready.wait(timeout=15.0):
        raise ValueError("timed out preparing the Google consent URL")
    if job.auth_url is None:
        err = job.error
        if isinstance(err, auth.AuthError):
            raise ValueError(str(err)) from err
        if isinstance(err, Exception):
            raise ValueError(str(err)) from err
        raise ValueError("Google sign-in failed before a consent URL was ready")
    return {"auth_url": job.auth_url}


def finish_auth() -> dict[str, Any]:
    """Wait for the Ada-opened consent redirect and return a fresh config.

    The job is kept afterwards: a second call returns the same report (or
    raises the same error) rather than "no sign-in in progress".
    """
    from googleapps import auth

    with _auth_lock:
        job = _auth_job
    if job is None:
        raise ValueError("no Google sign-in is in progress; press Sign in again")
    # Consent can sit open for minutes; match googleapps' 300 s budget.
    if not job.done.wait(timeout=320.0):
        raise ValueError("timed out waiting for the OAuth redirect")
    if job.error is not None:
        if isinstance(job.error, (auth.AuthError, ValueError)):
            raise ValueError(str(job.error)) from job.error
        raise ValueError(str(job.error)) from job.error
    if job.report is None:
        raise ValueError("Google sign-in finished with no configuration report")
    return job.report


# ---------------------------------------------------------------- delivery


def _addresses(payload: dict[str, Any], key: str) -> list[str]:
    """A list of addresses out of the payload, validated, or a ``ValueError``.

    The field is a JSON list; a comma-separated string is accepted too because
    that is what a text box hands over. Validation is the package's own, so
    the refusal names every bad address at once and happens before any request.
    """
    raw = payload.get(key)
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [part for part in raw.split(",") if part.strip()]
    if not isinstance(raw, list) or any(not isinstance(a, str) for a in raw):
        raise ValueError(f"'{key}' must be a list of email addresses")
    if not raw:
        return []
    from googleapps.addresses import validate_addresses
    from googleapps.transport import GoogleError

    try:
        return validate_addresses(raw, what=f"'{key}'")
    except GoogleError as exc:
        raise ValueError(exc.detail) from None


def _flag(payload: dict[str, Any], key: str) -> bool:
    value = payload.get(key, False)
    if not isinstance(value, bool):
        raise ValueError(f"'{key}' must be a boolean")
    return value


def deliver(
    session_id: str, payload: dict[str, Any], *, config: Any | None = None
) -> dict[str, Any]:
    """Send one routed session wherever the payload asks.

    Raises ``StepNotFound`` for an unknown session, ``StepOrderError`` when
    the run is not routed yet, and ``ValueError`` for bad input -- all before
    anything is sent. Everything after that is reported per destination.
    """
    if _googleapps() is None:
        raise ValueError(
            "delivery is unavailable: the googleapps package is not installed "
            "beside the service"
        )
    from googleapps import auth, calendar, chat, gmail
    from googleapps import specreview as _specreview
    from googleapps.config import ConfigError, load_config
    from googleapps.runner import RunOutcome, email_body
    from googleapps.transport import GoogleError

    want_chat = _flag(payload, "chat")
    email_to = _addresses(payload, "email")
    want_schedule = _flag(payload, "schedule")
    want_spec_review = _flag(payload, "spec_review")
    attendees = _addresses(payload, "attendees")
    wants_event = want_schedule or want_spec_review
    if not (want_chat or email_to or wants_event):
        raise ValueError(
            "nothing to deliver: set 'chat', give 'email' addresses, or set "
            "'schedule' or 'spec_review' with 'attendees'"
        )
    if want_schedule and not attendees:
        raise ValueError("'schedule' needs at least one address in 'attendees'")
    if want_spec_review and not attendees:
        raise ValueError("'spec_review' needs at least one address in 'attendees'")
    if attendees and not wants_event:
        raise ValueError(
            "'attendees' only means something with 'schedule' or 'spec_review'"
        )
    # The start time, parsed and refused here -- before the session is even
    # read, let alone a request sent. A meeting in the past is a typo, and
    # booking it and reporting success would hide the typo.
    when: float | None = None
    if payload.get("when") is not None:
        if not want_spec_review:
            raise ValueError("'when' only means something with 'spec_review'")
        when = _specreview.parse_when(payload["when"])

    result = _snapshot(_steps._get(session_id))
    config = config if config is not None else load_config(dotenv=False)
    transport = _transport()
    board_name = result.board_path.stem
    stage_lines = result.stage_lines()
    report: dict[str, Any] = {"session": session_id}

    if want_chat:
        try:
            config.require_webhook()
            chat.post_run_card(
                config.chat_webhook, result, transport=transport,
                stage_lines=stage_lines,
            )
            report["chat"] = {"ok": True}
        except (ConfigError, GoogleError) as exc:
            report["chat"] = {"ok": False, "error": str(exc)}

    if email_to:
        try:
            token = auth.access_token(
                config, transport, require=(auth.GMAIL_SCOPE,)
            )
            message_id = gmail.send_run_email(
                token,
                to=email_to,
                subject=f"silkscreen: {board_name} — {chat.verdict(result)}",
                body=email_body(RunOutcome(result=result, stage_lines=stage_lines)),
                board_path=result.board_path,
                transport=transport,
            )
            report["email"] = {"ok": True, "to": email_to, "message_id": message_id}
        except (ConfigError, auth.AuthError, GoogleError) as exc:
            report["email"] = {"ok": False, "to": email_to, "error": str(exc)}

    if want_schedule:
        blockers = result.blockers
        state = result.review_state()
        if state == "failed":
            # A critic whose answer could not be read yields no blockers, and
            # "no blockers -- nothing scheduled" over that is a model failure
            # read as a clean board. ``reviewed`` is true here (the step ran),
            # which is why the state is consulted and not the flag.
            report["calendar"] = {
                "ok": False,
                "skipped_reason": REVIEW_FAILED_CALENDAR_REASON,
            }
        elif state != "ok":
            report["calendar"] = {
                "ok": False,
                "skipped_reason": "the review step has not run, so whether "
                "there are blockers is not known — nothing scheduled",
            }
        elif not blockers:
            report["calendar"] = {
                "ok": False,
                "skipped_reason": "no blockers — nothing scheduled",
            }
        else:
            try:
                token = auth.access_token(
                    config, transport, require=(auth.CALENDAR_SCOPE,)
                )
                event = calendar.schedule_review(
                    token,
                    board_name=board_name,
                    blocker_titles=[str(f.title) for f in blockers],
                    attendees=attendees,
                    transport=transport,
                )
                report["calendar"] = {
                    "ok": True,
                    "blockers": len(blockers),
                    "html_link": event.html_link,
                    "meet_uri": event.meet_uri,
                }
            except (ConfigError, auth.AuthError, GoogleError) as exc:
                report["calendar"] = {"ok": False, "error": str(exc)}

    if want_spec_review:
        report["spec_review"] = _book_spec_review(
            result,
            attendees=attendees,
            when=when,
            board_name=board_name,
            config=config,
            transport=transport,
        )

    return report


def _usable_agenda(stored: Any) -> Any | None:
    """The agenda the spec-review stage produced, or ``None`` if it produced none.

    The stage's ``SpecReviewResult`` has three states and only one of them is
    an agenda: ``review`` set is the agenda, ``review`` ``None`` means the
    model never gave a usable answer, and no result at all means the stage
    never ran. The last two must land in the same place here -- on
    :func:`agenda_from_result`, which derives the agenda from the findings --
    because a failed model turned into an agenda with no items would be
    skipped as "nothing needs a meeting" over a board with blockers, which is
    a model outage reported as a clean board.

    Duck-typed, like ``agenda_from_spec_review`` itself: a bare ``SpecReview``
    (or its dict) has no ``review`` member and is used as-is.
    """
    if stored is None:
        return None
    if isinstance(stored, Mapping):
        return None if "review" in stored and stored["review"] is None else stored
    if hasattr(stored, "review") and stored.review is None:
        return None
    return stored


def _book_spec_review(
    result: SessionResult,
    *,
    attendees: list[str],
    when: float | None,
    board_name: str,
    config: Any,
    transport: Any,
) -> dict[str, Any]:
    """The ``spec_review`` destination: agenda in, calendar hold out.

    Three answers, never a fourth: booked, deliberately skipped with the
    reason, or failed with the error. The two skips are the honesty rules the
    old ``--schedule`` path already keeps -- an unreviewed run does not get to
    claim there is nothing to discuss, and an agenda with nothing blocking
    does not get half an hour of four people's day.
    """
    from googleapps import auth, specreview
    from googleapps.config import ConfigError
    from googleapps.transport import GoogleError

    state = result.review_state()
    if state == "failed":
        # Before the agenda is built, because an agenda the review step
        # stored is "reviewed by construction" (``Agenda.reviewed`` defaults
        # true for one made from a real ``SpecReview``) -- and the review
        # step asks for an agenda whether or not the critic answered, since
        # unrouted nets are evidence too. Consulting the agenda alone would
        # book a meeting off a board nobody has argued against.
        return {"ok": False, "skipped_reason": REVIEW_FAILED_SPEC_REVIEW_REASON}
    if state == "ok" and _usable_agenda(result.spec_review) is not None:
        agenda = specreview.agenda_from_spec_review(result.spec_review)
    else:
        # ``agenda_from_result`` reads the review state itself, so an
        # unreviewed run comes back with ``reviewed`` false and is refused
        # below in the package's own words.
        agenda = specreview.agenda_from_result(result, board_name=board_name)
    unrouted = specreview.unrouted_nets(result)
    # The remaining two refusals are the agenda's own judgment
    # (``Agenda.skip_reason``): a run whose review step has not run, and an
    # agenda with nothing blocking. Read from it rather than re-derived here
    # so the wording cannot drift from ``python -m googleapps run``'s.
    if agenda.skip_reason is not None:
        return {"ok": False, "skipped_reason": agenda.skip_reason}
    try:
        token = auth.access_token(
            config, transport, require=(auth.CALENDAR_SCOPE,)
        )
        event = specreview.schedule_spec_review(
            token,
            agenda=agenda,
            attendees=attendees,
            when=when,
            unrouted=unrouted,
            transport=transport,
        )
    except (ConfigError, auth.AuthError, GoogleError) as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "html_link": event.html_link,
        "meet_uri": event.meet_uri,
        "minutes": agenda.total_minutes(),
        "items": len(agenda.items),
        "blocking": len(agenda.blocking),
    }


# ---------------------------------------------------------------- routing


def is_deliver_path(path: str) -> bool:
    """True for ``/steps/<id>/deliver`` (the one POST this module answers)."""
    parts = [p for p in path.split("?")[0].split("/") if p]
    return len(parts) == 3 and parts[0] == "steps" and parts[2] == "deliver"


def handle_post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    route = path.split("?")[0].rstrip("/")
    if route == "/deliver/auth/start":
        return begin_auth()
    if route == "/deliver/auth":
        # Ada finishes with client_opens after /start; bare POST keeps the
        # all-in-one server-side browser open for CLI-style callers.
        if payload.get("client_opens"):
            return finish_auth()
        return start_auth()
    if not is_deliver_path(path):
        raise _steps.StepNotFound(
            "POST /steps/<id>/deliver, /deliver/auth/start, or /deliver/auth"
        )
    session_id = [p for p in path.split("?")[0].split("/") if p][1]
    return deliver(session_id, payload)


def handle_get(path: str) -> dict[str, Any]:
    if path.split("?")[0].rstrip("/") != "/deliver/config":
        raise _steps.StepNotFound("GET /deliver/config")
    return config_report()
