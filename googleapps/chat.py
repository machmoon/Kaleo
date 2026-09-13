"""Posting a run summary card to a Google Chat incoming webhook.

No OAuth here: for an incoming webhook the URL itself is the credential,
which is why it is validated before anything is sent to it and never
shown -- not even a tail -- in any error message. Only
``https://chat.googleapis.com/v1/spaces/…`` is accepted -- an exact host
match, checked before the transport is invoked.

The card follows the project's honesty rules. Counts come from the result and
never from a template, and the routing line may only say every net is routed
when ``unrouted`` is actually empty; otherwise the card names each unfinished
net and the router's reason, verbatim. A card that says "routed" over a
ratsnest is the exact failure this codebase exists to prevent.
"""

from __future__ import annotations

import html
import json
import urllib.parse
from typing import Any

from .transport import (
    GoogleError,
    HttpRequest,
    Transport,
    describe_secret,
    ensure_google_url,
    error_detail,
)

__all__ = [
    "NOT_REVIEWED_NOTE",
    "post_run_card",
    "review_note",
    "review_state",
    "routing_lines",
    "run_card",
    "validate_webhook",
    "verdict",
]

CHAT_HOST = "chat.googleapis.com"
#: Past this many, a space is being spammed rather than informed.
MAX_LISTED_FINDINGS = 8
MAX_LISTED_UNROUTED = 12
#: The sentence ``service/deliver.py``'s Calendar rule already uses for a run
#: whose review step has not run. One wording, shared, so the card, the
#: subject and the calendar cannot drift into three descriptions of one fact.
NOT_REVIEWED_NOTE = "not reviewed (the review step has not run)"


def review_state(result: Any) -> str:
    """``"ok"``, ``"failed"`` or ``"skipped"``: what the critic actually did.

    Two producers hand this package a result and they say it differently.
    ``PipelineResult`` carries a ``ReviewReport`` on ``review`` (``ok`` /
    ``failed`` / ``ran``); ``service.deliver.SessionResult`` carries only a
    ``reviewed`` flag, because a step session knows whether the review step
    was pressed and nothing more. Both are read here, and nothing else is: a
    result that says neither is treated as unreviewed, because "nobody said
    the critic ran" is not evidence that it did.
    """
    review = getattr(result, "review", None)
    if review is not None and hasattr(review, "ok"):
        if review.ok:
            return "ok"
        return "failed" if getattr(review, "failed", False) else "skipped"
    reviewed = getattr(result, "reviewed", None)
    return "ok" if reviewed else "skipped"


def review_note(result: Any) -> str:
    """One sentence about the review, for a result whose review is not ok.

    A ``ReviewReport`` speaks for itself (its ``note()`` names the failure);
    a session that only knows the step did not run gets the calendar rule's
    own wording.
    """
    review = getattr(result, "review", None)
    if review is not None and hasattr(review, "note"):
        return review.note()
    return NOT_REVIEWED_NOTE


def validate_webhook(url: str) -> str:
    """The webhook URL, or a refusal that never repeats the secret.

    Not even a tail is shown: an incoming-webhook URL ends in its ``token=``
    parameter, so the last characters are the last characters of the secret.
    """
    shown = describe_secret(url)
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != CHAT_HOST:
        raise GoogleError(
            "bad_webhook",
            f"GOOGLEAPPS_CHAT_WEBHOOK ({shown}) must be an https URL on "
            f"{CHAT_HOST}",
        )
    if not parsed.path.startswith("/v1/spaces/"):
        raise GoogleError(
            "bad_webhook",
            f"GOOGLEAPPS_CHAT_WEBHOOK ({shown}) does not look like an "
            "incoming-webhook URL (expected a /v1/spaces/… path)",
        )
    return ensure_google_url(url)


def routing_lines(route: Any) -> list[str]:
    """The routing story, told the way the CLI tells it.

    ``route`` is ``RouteResult`` or ``None`` (routing turned off). The first
    line is the router's own summary -- counts, copper length -- and every
    unrouted net follows by name with the router's reason.
    """
    if route is None:
        return ["not routed (routing was turned off for this run)"]
    lines = [route.summary()]
    unrouted = sorted(route.unrouted.items())
    for net, reason in unrouted[:MAX_LISTED_UNROUTED]:
        lines.append(f"unrouted {net}: {reason}")
    if len(unrouted) > MAX_LISTED_UNROUTED:
        lines.append(f"…and {len(unrouted) - MAX_LISTED_UNROUTED} more unrouted nets")
    return lines


def verdict(result: Any) -> str:
    """One phrase for the whole run -- the Chat headline and the Gmail
    subject -- ordered by what matters most: blockers first, then a review
    that produced no verdict, then unfinished copper, and "board ready" only
    when none of those apply.

    A review that did not run, or whose answer could not be read, yields no
    blockers, and "board ready" over that is the same untruth as "board ready"
    over a ratsnest: it states a fact nobody established. The unreviewed
    phrase therefore outranks the routing one -- an unrouted net is a known
    defect, and an unread review is an unknown board. "Did not run" is read
    from whichever field the producer has (:func:`review_state`), so a step
    session that never pressed the review step is not verdicted clean either.
    A run with routing turned off is "placed", never "ready": nothing routed
    is not the same as everything routed.
    """
    blockers = len(result.blockers)
    if blockers:
        return f"needs review — {blockers} blocker(s)"
    state = review_state(result)
    if state == "failed":
        return "not reviewed — the review failed"
    if state != "ok":
        return "not reviewed — the review step has not run"
    route = getattr(result, "route", None)
    if route is None:
        # Routing was turned off: there is no copper, and "ready" over a
        # board with no copper claims a stage that never happened.
        return "placed — not routed (routing was turned off)"
    if route.unrouted:
        return f"placed — {len(route.unrouted)} net(s) left unrouted"
    return "board ready"


def _paragraphs(lines: list[str]) -> list[dict[str, Any]]:
    """``textParagraph`` renders a subset of HTML, so a net named ``<3V3``
    or a finding title with an ampersand has to be escaped or it is either
    swallowed as a tag or rejected by the webhook.

    **CONFIRMED 2026-09-08** against Google's "Format text in Google Chat
    messages": card text is formatted with "a small subset of HTML tags" --
    ``<b> <i> <u> <s> <font> <a> <time> <br> <code> <pre> <ul> <li> <ol>`` --
    explicitly *unlike* message ``text``, which uses Chat's own markdown. Two
    consequences this code relies on: ``&``, ``<`` and ``>`` must be escaped
    (``quote=False`` is right -- quotes are not markup in this subset), and a
    newline is **not** a line break in card text, which is why every line gets
    its own widget here rather than being joined with ``\\n``.
    """
    return [
        {"textParagraph": {"text": html.escape(line, quote=False)}} for line in lines
    ]


def run_card(
    result: Any,
    *,
    stage_lines: list[str] | None = None,
    duration_s: float | None = None,
) -> dict[str, Any]:
    """The webhook payload: a cardsV2 card plus a plain-text fallback."""
    w, h = result.board.size_mm
    # Severity is a StrEnum, so the comparison needs no engine import here.
    blockers = [f for f in result.findings if f.severity == "blocker"]
    notes = [f for f in result.findings if f.severity != "blocker"]

    if review_state(result) != "ok":
        # Not "no findings": nothing was found because nothing was read (or
        # the step never ran), and a card that says otherwise is what makes
        # an outage look like a pass.
        review_lines = [review_note(result)]
    else:
        review_lines = [
            f"{len(result.findings)} finding(s): {len(blockers)} blocker(s), "
            f"{len(notes)} other(s)"
            if result.findings
            else "no findings"
        ]
    for finding in blockers[:MAX_LISTED_FINDINGS]:
        review_lines.append(f"BLOCKER: {finding.title}")
    for finding in notes[: max(0, MAX_LISTED_FINDINGS - len(blockers))]:
        review_lines.append(f"{finding.severity.value}: {finding.title}")
    listed = min(len(blockers), MAX_LISTED_FINDINGS) + max(
        0, min(len(notes), MAX_LISTED_FINDINGS - len(blockers))
    )
    if len(result.findings) > listed:
        review_lines.append(f"…and {len(result.findings) - listed} more finding(s)")

    board_lines = [
        f"{result.spec.part_count()} parts, {result.spec.net_count()} nets",
        f"board {w:.2f} x {h:.2f} mm [{result.board.solver_status}]",
    ]
    if duration_s is not None:
        board_lines.append(f"finished in {duration_s:.1f} s")

    sections = [
        {"header": "Board", "widgets": _paragraphs(board_lines)},
        {"header": "Routing", "widgets": _paragraphs(routing_lines(result.route))},
        {"header": "Review", "widgets": _paragraphs(review_lines)},
    ]
    if stage_lines:
        sections.insert(1, {"header": "Stages", "widgets": _paragraphs(stage_lines)})

    # Chat shows ``text`` *and* the card, and uses ``text`` for the
    # notification preview -- so it is one line, not a second copy of the
    # card. **CONFIRMED**: "To define the content of the message, you can
    # include rich text (``text``), one or more card interfaces (``cardsV2``),
    # or both", and the incoming-webhook payload is the same ``Message``
    # resource (``{"text": ..., "cardsV2": [{"cardId": ..., "card": {...}}]}``
    # POSTed as ``application/json`` to
    # ``https://chat.googleapis.com/v1/spaces/SPACE/messages?key=…&token=…``).
    #
    # **Deliberately not HTML-escaped**, unlike the card widgets: ``text`` is
    # Chat's markdown, not the card's HTML subset, so escaping it would put
    # literal ``&amp;`` in front of a reader. The cost is that an intent
    # containing ``*`` or ``_`` renders as markup in the preview line; the
    # card, which is what anyone actually reads, is escaped correctly. Chat
    # publishes no escape syntax for ``text``, so there is no third option.
    headline = f"silkscreen: {verdict(result)} — {str(result.intent)[:120]}"
    return {
        "text": headline,
        "cardsV2": [
            {
                "cardId": "silkscreen-run",
                "card": {
                    "header": {
                        "title": f"silkscreen: {verdict(result)}",
                        "subtitle": str(result.intent)[:120],
                    },
                    "sections": sections,
                },
            }
        ],
    }


def post_run_card(
    webhook_url: str,
    result: Any,
    *,
    transport: Transport,
    stage_lines: list[str] | None = None,
    duration_s: float | None = None,
) -> None:
    """Build the card and POST it. Raises :class:`GoogleError` on refusal."""
    url = validate_webhook(webhook_url)
    payload = run_card(result, stage_lines=stage_lines, duration_s=duration_s)
    response = transport(
        HttpRequest(
            "POST",
            url,
            {"Content-Type": "application/json; charset=utf-8"},
            json.dumps(payload).encode("utf-8"),
        )
    )
    if response.status >= 300:
        raise GoogleError(
            f"http_{response.status}",
            f"the Chat webhook ({describe_secret(webhook_url)}) rejected the card: "
            + error_detail(response, "no error message in the response"),
        )
