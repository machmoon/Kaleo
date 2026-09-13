"""Parsing what people actually type at a bot in a channel."""

from __future__ import annotations

import pytest

from slackbot.commands import Command, CommandError, parse_command, strip_slack_markup


def test_strips_mentions_links_and_smart_quotes():
    text = "<@U0BOT> design a <https://x.test/ds.pdf|datasheet> board — “fast”"
    cleaned = strip_slack_markup(text)
    assert "<@U0BOT>" not in cleaned
    assert "https://x.test/ds.pdf" in cleaned
    assert "—" not in cleaned and "“" not in cleaned


def test_bare_intent_is_a_design_run():
    """The most common message has no verb in it at all."""
    command = parse_command("<@U0BOT> a 3.3V buck converter from 12V")
    assert command.verb == "design"
    assert command.intent == "a 3.3V buck converter from 12V"
    assert command.review is True


def test_verbs_and_aliases():
    assert parse_command("build an led blinker").verb == "design"
    assert parse_command("layout an led blinker").verb == "place"
    assert parse_command("check").verb == "review"
    assert parse_command("fab 20").verb == "order"
    assert parse_command("help").verb == "help"


def test_empty_message_is_help():
    assert parse_command("<@U0BOT>").verb == "help"
    assert parse_command("").verb == "help"


def test_place_never_reviews():
    """`place` is the cheap path; a review would defeat the point of asking."""
    assert parse_command("place an stm32 board").review is False


def test_no_review_flag():
    command = parse_command("design an stm32 board --no-review")
    assert command.review is False
    assert command.intent == "an stm32 board"


def test_datasheets_are_collected_and_unwrapped():
    command = parse_command(
        "design a regulator --datasheet TPS62840=<https://ti.test/d.pdf> "
        "-d STM32=https://st.test/s.pdf"
    )
    assert command.datasheets == {
        "TPS62840": "https://ti.test/d.pdf",
        "STM32": "https://st.test/s.pdf",
    }
    assert "--datasheet" not in command.intent
    assert command.intent == "a regulator"


@pytest.mark.parametrize(
    "text", ["design x --datasheet", "design x --datasheet nourl", "design x -d =u"]
)
def test_malformed_datasheet_is_a_command_error(text):
    with pytest.raises(CommandError):
        parse_command(text)


def test_order_takes_a_bare_quantity_or_a_flag():
    assert parse_command("order 25").quantity == 25
    assert parse_command("order --qty 25").quantity == 25
    assert parse_command("order").quantity == 5


@pytest.mark.parametrize("text", ["order 0", "order 99999", "order --qty abc"])
def test_bad_quantity_is_rejected(text):
    with pytest.raises(CommandError):
        parse_command(text)


def test_design_without_an_intent_is_refused():
    with pytest.raises(CommandError):
        parse_command("design")


def test_unbalanced_quote_does_not_raise():
    """shlex raises on this; a chat message is allowed to contain an apostrophe."""
    command = parse_command('design a "3v3 rail')
    assert command.verb == "design"
    assert command.intent


def test_verbs_that_need_a_prior_run():
    assert Command(verb="review").needs_prior_run()
    assert Command(verb="order").needs_prior_run()
    assert not Command(verb="design", intent="x").needs_prior_run()
