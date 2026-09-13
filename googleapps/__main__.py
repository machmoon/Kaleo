"""``python -m googleapps`` -- auth, check, run.

``auth``   one command, one browser click-through: opens Google's consent
           page, catches the loopback redirect, stores the token (0o600).
``check``  reports what is configured and whether the token is usable.
           Purely local: it reads the environment and the token file and
           makes no network call of any kind.
``run``    runs the real pipeline and then, on request, posts the Chat card
           (``--chat``), emails the results with the board attached
           (``--email``), and schedules a design review (``--schedule``) --
           but only when the adversarial review found blockers, and it says
           which way that went either way.

Everything ``run`` can check before the pipeline spends a model call, it
checks first: the API key, the webhook's shape, every address, and -- when
Gmail or Calendar is asked for -- that the stored token can be refreshed
(refresh token present, OAuth client configured), refreshing it now if it has
expired, and that the *scope* that destination needs was actually granted.
That last one is not theoretical: Google's consent screen is a checkbox per
scope, so a sign-in can succeed with Gmail declined, and without the check
the refusal arrives at the Gmail call -- after the board, and the model calls
that produced it, have already been paid for.

Exit codes follow the CLI's convention: 0 success, 1 a run or API failure,
2 a configuration problem.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import auth, calendar, chat, gmail, specreview
from .addresses import validate_addresses
from .config import Config, ConfigError, load_config
from .runner import ModelError, email_body, run_pipeline
from .transport import GoogleError, Transport, urllib_transport


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="googleapps",
        description="Silkscreen results into Google Chat, Gmail, and Calendar.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("auth", help="sign in: browser consent, loopback redirect")
    sub.add_parser("check", help="report configuration and token state (no network)")

    run = sub.add_parser("run", help="generate a board, then deliver the results")
    run.add_argument("intent", help="what to build, in plain language")
    run.add_argument("-o", "--output", default="board.kicad_pcb",
                     help="where to write the .kicad_pcb (default: %(default)s)")
    # The pipeline flags, named exactly as `python -m silkscreen` names them.
    run.add_argument("-d", "--datasheet", action="append", default=[],
                     metavar="PART=URL",
                     help="a datasheet to read first; repeatable")
    run.add_argument("--model", default=None,
                     help="worker model (default: SILKSCREEN_MODEL or the "
                          "engine default)")
    run.add_argument("--time-limit", type=float, default=20.0,
                     help="placement solver budget in seconds")
    run.add_argument("--repairs", type=int, default=3,
                     help="how many times to send a bad proposal back")
    run.add_argument("--no-review", action="store_true",
                     help="skip the adversarial review pass")
    run.add_argument("--no-route", action="store_true",
                     help="stop after placement, leaving the copper empty")
    # The delivery flags.
    run.add_argument("--chat", action="store_true",
                     help="post the run card to the configured Chat webhook")
    run.add_argument("--email", action="append", default=[], metavar="ADDR",
                     help="email the results (with the board attached); repeatable")
    run.add_argument("--schedule", action="store_true",
                     help="schedule a design-review Meet, only if the review "
                          "found blockers")
    run.add_argument("--spec-review", action="store_true",
                     help="book a spec review: the open questions as an agenda, "
                          "with a Meet link — only if something is blocking")
    run.add_argument("--attendee", action="append", default=[], metavar="ADDR",
                     help="attendee for the review or spec-review event; repeatable")
    return parser


def _cmd_auth(config: Config, transport: Transport) -> int:
    path = auth.run_auth_flow(config, transport)
    print(f"Signed in. Token stored at {path} (mode 0600).")
    # Google's consent screen is a checkbox per scope, so a redirect that
    # said "granted" can still have left one unticked. Say which, here,
    # rather than letting the first --email discover it.
    _print_scope_lines(auth.load_token(path))
    return 0


def _print_scope_lines(token: dict | None) -> None:
    """What each destination can actually do, from the recorded grant."""
    for destination, label in (
        ("gmail", "Gmail (--email)"),
        ("calendar", "Calendar (--schedule, --spec-review)"),
    ):
        state = auth.destination_state(token, destination)
        note = {
            "granted": "granted",
            "denied": "NOT GRANTED — the consent checkbox was left unticked",
            "unknown": "not recorded (sign in again to record it)",
        }[state]
        print(f"  {label:36} {note}")
    if token is not None:
        denied = auth.denied_scopes(token, auth.SCOPES)
        if denied:
            print(auth.scope_hint(denied))


def _cmd_check(config: Config) -> int:
    """Purely local. No request leaves this function."""
    view = config.redacted()
    print("googleapps configuration:")
    for key in ("client_id", "client_secret", "chat_webhook", "google_api_key",
                "model", "token_path"):
        print(f"  {key:15} {view[key]}")
    if config.chat_webhook:
        # Shape only -- the same refusal `run --chat` would make, surfaced
        # here so it is found by `check` and not by the first real run.
        try:
            chat.validate_webhook(config.chat_webhook)
            print(f"  {'webhook shape':15} valid")
        except GoogleError as exc:
            print(f"  {'webhook shape':15} INVALID: {exc.detail}")
    status = auth.token_status(config.token_path)
    private = auth.token_file_is_private(config.token_path)
    print(f"  {'token':15} {status}"
          + ("" if status == "missing" or private else "  (WARNING: not mode 0600)"))
    if status == "missing":
        print("Run `python -m googleapps auth` to sign in for Gmail and Calendar.")
    else:
        _print_scope_lines(auth.load_token(config.token_path))
    return 0


def _parse_datasheets(entries: list[str]) -> dict[str, str]:
    datasheets: dict[str, str] = {}
    for entry in entries:
        part, sep, url = entry.partition("=")
        if not sep or not part.strip() or not url.strip():
            raise ConfigError(f"--datasheet needs PART=URL, got {entry!r}")
        datasheets[part.strip()] = url.strip()
    return datasheets


def _preflight(args: argparse.Namespace, config: Config, transport: Transport) -> None:
    """Every refusal that can happen before the pipeline runs, in one place.

    Raises :class:`ConfigError`, :class:`AuthError` or :class:`GoogleError`;
    all three map to a non-zero exit before a model call is made.
    """
    config.require_api_key()
    _parse_datasheets(args.datasheet)
    if args.chat:
        config.require_webhook()
        chat.validate_webhook(config.chat_webhook)
    if args.email:
        validate_addresses(args.email, what="--email")
    if args.schedule and not args.attendee:
        raise ConfigError("--schedule needs at least one --attendee")
    if args.spec_review and not args.attendee:
        raise ConfigError("--spec-review needs at least one --attendee")
    if args.attendee and not (args.schedule or args.spec_review):
        raise ConfigError(
            "--attendee only means something with --schedule or --spec-review"
        )
    if args.attendee:
        validate_addresses(args.attendee, what="--attendee")
    if args.email or args.schedule or args.spec_review:
        # A token that will still be usable *after* the run, not merely now:
        # the pipeline can outlast an access token's remaining minutes, so
        # the file must exist, carry a refresh token, and the OAuth client
        # that refresh needs must be configured -- otherwise a refresh in
        # the delivery step fails after the board exists. The refresh itself
        # is the one pre-run step that may touch the network.
        token = auth.load_token(config.token_path)
        if not token.get("refresh_token"):
            raise auth.AuthError(
                f"the stored token cannot be refreshed; {auth.RERUN_HINT}"
            )
        config.require_oauth()
        # And the scopes those destinations need. Under granular consent a
        # sign-in that "succeeded" can be missing exactly one of them, and
        # discovering that at the Gmail call means the run already paid for
        # a board. Every gap is named at once, the ConfigError convention.
        needed: list[str] = []
        if args.email:
            needed.append(auth.GMAIL_SCOPE)
        if args.schedule or args.spec_review:
            needed.append(auth.CALENDAR_SCOPE)
        auth.require_scopes(token, tuple(needed))
        auth.access_token(config, transport, require=tuple(needed))


def _cmd_run(args: argparse.Namespace, config: Config, transport: Transport) -> int:
    _preflight(args, config, transport)

    outcome = run_pipeline(
        config,
        args.intent,
        args.output,
        datasheets=_parse_datasheets(args.datasheet),
        review=not args.no_review,
        route=not args.no_route,
        max_repairs=args.repairs,
        time_limit_s=args.time_limit,
        model_name=args.model,
    )
    result = outcome.result
    print(result.summary())
    for path in result.artifacts:
        print(f"wrote {path}")

    board_path = result.board_path
    board_name = Path(args.output).stem
    failures = 0

    if args.chat:
        try:
            chat.post_run_card(
                config.chat_webhook,
                result,
                transport=transport,
                stage_lines=outcome.stage_lines,
                duration_s=outcome.duration_s,
            )
            print("posted the run card to Google Chat")
        except GoogleError as exc:
            failures += 1
            print(f"chat: {exc}", file=sys.stderr)

    if args.email:
        try:
            token = auth.access_token(
                config, transport, require=(auth.GMAIL_SCOPE,)
            )
            message_id = gmail.send_run_email(
                token,
                to=args.email,
                subject=f"silkscreen: {board_name} — {chat.verdict(result)}",
                body=email_body(outcome),
                board_path=board_path,
                transport=transport,
            )
            print(f"emailed {', '.join(args.email)} (message {message_id})")
        except (auth.AuthError, GoogleError) as exc:
            failures += 1
            print(f"gmail: {exc}", file=sys.stderr)

    if args.schedule:
        blockers = list(result.blockers)
        review = getattr(result, "review", None)
        if args.no_review:
            # No review ran, so "no blockers" is not a fact we hold.
            print("review was skipped (--no-review) — no review event was created")
        elif review is not None and review.failed:
            # The same reasoning as --no-review, reached a different way: the
            # critic was asked and said nothing readable, so an empty blocker
            # list is not evidence and must not silently skip the meeting.
            print(
                f"review failed to produce a readable answer ({review.detail}) "
                "— no review event was created, and nothing is known about "
                "this board"
            )
        elif not blockers:
            print("review found no blockers — no review event was created")
        else:
            try:
                token = auth.access_token(
                    config, transport, require=(auth.CALENDAR_SCOPE,)
                )
                event = calendar.schedule_review(
                    token,
                    board_name=board_name,
                    blocker_titles=[f.title for f in blockers],
                    attendees=args.attendee,
                    transport=transport,
                )
                print(f"review found {len(blockers)} blocker(s) — scheduled "
                      f"a design review: {event.html_link}")
                # A booked event with no Meet link is not silence: the
                # attendees have already been mailed (sendUpdates=all), so
                # whichever of pending/failure/absent it was gets a line.
                print(
                    f"meet: {event.meet_uri}" if event.meet_uri
                    else f"meet: {event.meet_note}"
                )
            except (auth.AuthError, GoogleError) as exc:
                failures += 1
                print(f"calendar: {exc}", file=sys.stderr)

    if args.spec_review:
        # The same judgment the --schedule path makes, against a richer
        # agenda: without a review there is nothing to hold a meeting about
        # that we actually know, and with nothing blocking there is nothing
        # to decide. Both are said out loud rather than booked anyway.
        agenda = specreview.agenda_from_spec_review(
            getattr(result, "spec_review", None)
        ) if getattr(result, "spec_review", None) is not None else (
            specreview.agenda_from_result(result, board_name=board_name)
        )
        review = getattr(result, "review", None)
        if args.no_review:
            print("review was skipped (--no-review) — no spec review was booked")
        elif review is not None and review.failed:
            # The --schedule rule again: a critic that answered nothing
            # readable leaves an empty blocker list that is not evidence.
            print(
                f"review failed to produce a readable answer ({review.detail}) "
                "— no spec review was booked, and nothing is known about "
                "this board"
            )
        elif not agenda.blocking:
            print("nothing on the agenda is blocking — no spec review was booked")
        else:
            try:
                token = auth.access_token(
                    config, transport, require=(auth.CALENDAR_SCOPE,)
                )
                event = specreview.schedule_spec_review(
                    token,
                    agenda=agenda,
                    attendees=args.attendee,
                    unrouted=specreview.unrouted_nets(result),
                    transport=transport,
                )
                print(f"booked a {agenda.total_minutes()}-minute spec review "
                      f"({len(agenda.blocking)} blocking item(s)): {event.html_link}")
                # A booked event with no Meet link is not silence: the
                # attendees have already been mailed (sendUpdates=all), so
                # whichever of pending/failure/absent it was gets a line.
                print(
                    f"meet: {event.meet_uri}" if event.meet_uri
                    else f"meet: {event.meet_note}"
                )
            except (auth.AuthError, GoogleError) as exc:
                failures += 1
                print(f"spec review: {exc}", file=sys.stderr)

    return 1 if failures else 0


def main(argv: list[str] | None = None, *, transport: Transport | None = None) -> int:
    args = _parser().parse_args(argv)
    transport = transport or urllib_transport()
    try:
        config = load_config()
        if args.command == "auth":
            return _cmd_auth(config, transport)
        if args.command == "check":
            return _cmd_check(config)
        return _cmd_run(args, config, transport)
    except (ConfigError, ModelError) as exc:
        # ModelError here is a bad model name or a missing key -- the CLI's
        # exit 2 case, not a run failure.
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except auth.AuthError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except GoogleError as exc:
        # A refusal made before any request (bad host, bad address) is the
        # user's input to fix; a failed call is the run's failure.
        print(f"error: {exc}", file=sys.stderr)
        return 2 if exc.code in ("bad_host", "bad_webhook", "bad_address") else 1
    except Exception as exc:  # noqa: BLE001 - the pipeline's own failures
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
