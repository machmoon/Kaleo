"""Parsing what someone typed at the bot into something the runner can execute.

Slack message text is not plain text. Mentions arrive as ``<@U123>``, links as
``<http://x|x>``, and the client helpfully turns quotes and dashes into
typographic ones. All of that is undone here, in one place, so the rest of the
package only ever sees what the person meant to type.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field

__all__ = ["Command", "CommandError", "parse_command", "strip_slack_markup", "HELP"]


class CommandError(ValueError):
    """The message did not name a command this bot can run."""


#: ``<@U123>``/``<#C123|name>``: a mention Slack encoded for us.
_MENTION = re.compile(r"<[@#][A-Z0-9]+(\|[^>]*)?>")
#: ``<http://x|label>`` or ``<http://x>``: a link Slack auto-formatted.
_LINK = re.compile(r"<(https?://[^|>]+)(\|[^>]*)?>")
#: Slack's mailto encoding, which shows up in ``PART=someone@example.com``.
_MAILTO = re.compile(r"<mailto:([^|>]+)(\|[^>]*)?>")
#: What the composer substitutes for quotes and dashes as you type.
_SMART = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"',
                        "–": "-", "—": "-", " ": " "})

VERBS = ("design", "place", "review", "order", "help")

#: Aliases exist because people will type the word that fits the sentence they
#: were already writing, not the word in the docs.
_ALIASES = {
    "build": "design",
    "make": "design",
    "generate": "design",
    "layout": "place",
    "pack": "place",
    "check": "review",
    "critique": "review",
    "quote": "order",
    "fab": "order",
    "?": "help",
    "usage": "help",
}

HELP = """\
*silkscreen* — PCB design in this channel. Mention me with:

• `@silkscreen design <what you want>` — propose a circuit, validate it, place \
it, and review it. Posts the board image and the `.kicad_pcb`.
• `@silkscreen place <what you want>` — the same run without the adversarial \
review pass. Faster and cheaper.
• `@silkscreen review` — re-run the critic on the last run *in this thread*.
• `@silkscreen order [qty]` — prepare a fabrication order from the last run in \
this thread. Prepares only: nothing is ever submitted and no payment is made.
• `@silkscreen help` — this message.

Add datasheets with `--datasheet PART=URL` (repeatable); anything cited in the \
review comes from those. Run in a channel, and I answer in a thread under your \
message so the whole team can see it.

Open the `.kicad_pcb` in KiCad — that is the supported path. The board file is \
the deliverable; the image is a preview."""


@dataclass(frozen=True)
class Command:
    """One parsed instruction."""

    verb: str
    intent: str = ""
    datasheets: dict[str, str] = field(default_factory=dict)
    #: ``order`` only: how many boards the draft is for.
    quantity: int = 5
    #: ``design``/``place``: whether the review pass runs.
    review: bool = True

    def needs_prior_run(self) -> bool:
        """True for verbs that act on a run already in this thread."""
        return self.verb in ("review", "order")


def strip_slack_markup(text: str) -> str:
    """Turn a Slack message body back into what the person typed."""
    text = _LINK.sub(lambda m: m.group(1), text)
    text = _MAILTO.sub(lambda m: m.group(1), text)
    text = _MENTION.sub(" ", text)
    return text.translate(_SMART).strip()


def _split(text: str) -> list[str]:
    """Tokenise, tolerating an unbalanced quote rather than failing on it."""
    try:
        return shlex.split(text)
    except ValueError:
        return text.split()


def parse_command(text: str) -> Command:
    """Parse a mention body into a :class:`Command`.

    Raises:
        CommandError: with a message that is safe to post back into the
            channel -- it quotes only what the user typed.
    """
    cleaned = strip_slack_markup(text or "")
    if not cleaned:
        return Command(verb="help")

    tokens = _split(cleaned)
    head = tokens[0].lower().strip(":,")
    verb = _ALIASES.get(head, head)
    if verb not in VERBS:
        # A bare "@silkscreen a 3v3 buck converter" is what people actually
        # type. Treat a missing verb as `design` rather than a syntax lesson.
        verb, rest = "design", tokens
    else:
        rest = tokens[1:]

    datasheets: dict[str, str] = {}
    quantity = 5
    review = True
    words: list[str] = []

    index = 0
    while index < len(rest):
        token = rest[index]
        lowered = token.lower()
        if lowered in ("--datasheet", "-d", "--ds"):
            index += 1
            if index >= len(rest):
                raise CommandError("`--datasheet` needs `PART=URL` after it.")
            pair = rest[index]
            part, sep, url = pair.partition("=")
            if not sep or not part.strip() or not url.strip():
                raise CommandError(
                    f"`--datasheet` needs `PART=URL`, got `{pair}`."
                )
            datasheets[part.strip()] = url.strip()
        elif lowered in ("--no-review", "--skip-review"):
            review = False
        elif lowered in ("--qty", "--quantity", "-q"):
            index += 1
            if index >= len(rest):
                raise CommandError("`--qty` needs a number after it.")
            quantity = _quantity(rest[index])
        elif verb == "order" and lowered.isdigit():
            quantity = _quantity(token)
        else:
            words.append(token)
        index += 1

    intent = " ".join(words).strip()
    if verb in ("design", "place") and not intent:
        raise CommandError(
            "Tell me what to build, e.g. `@silkscreen design a 3.3V buck "
            "converter from 12V`."
        )
    if verb == "place":
        review = False
    return Command(
        verb=verb,
        intent=intent,
        datasheets=datasheets,
        quantity=quantity,
        review=review,
    )


def _quantity(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise CommandError(f"Quantity must be a whole number, got `{raw}`.") from exc
    if not 1 <= value <= 10_000:
        raise CommandError("Quantity must be between 1 and 10000.")
    return value
