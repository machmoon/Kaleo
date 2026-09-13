"""``python -m meetings poll`` tests.

The bug class this file guards is **the invisible decline**: a driver that
prints only what it built hides the request it skipped, the meeting it could
not read, and the backlog it stopped at. Every test here asserts on the
declined half of the output as much as on the built half.

Nothing opens a socket, constructs a Gemini client, or runs the solver: the
transport is :class:`~meetings.tests.fakes.FakeTransport`, the model is a
``ScriptedModel``, and ``generate`` is injected.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from silkscreen.agents.model import ScriptedModel

from meetings.__main__ import main
from meetings.tests.fakes import FakeTransport, page

TOKEN = "ya29.test-token"
BASE = "https://meet.googleapis.com/v2"
CONF = "conferenceRecords/abc"

SAID = [
    ("alice", "we need a little 3.3 volt regulator board for the sensor rig"),
    ("bob", "and a small usb-c breakout board to go with it would be handy"),
]
REGULATOR_QUOTE = SAID[0][1]
BREAKOUT_QUOTE = SAID[1][1]


def env(**overrides) -> dict[str, str]:
    values = {"MEET_ACCESS_TOKEN": TOKEN, "MEET_API_BASE": BASE}
    values.update(overrides)
    return values


def recent() -> str:
    # The CLI polls against the wall clock, so the fixture ends "just now".
    return (datetime.now(UTC) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")


def transport(*, conferences=None, said=SAID) -> FakeTransport:
    records = conferences if conferences is not None else [
        {"name": CONF, "space": "spaces/aaa", "endTime": recent()}
    ]
    return FakeTransport(
        {
            "conferenceRecords": page("conferenceRecords", records),
            "/transcripts": page(
                "transcripts", [{"name": f"{CONF}/transcripts/t1", "state": "ENDED"}]
            ),
            "/entries": page(
                "transcriptEntries",
                [
                    {
                        "name": f"{CONF}/transcripts/t1/entries/{i}",
                        "participant": f"{CONF}/participants/{who}",
                        "text": text,
                    }
                    for i, (who, text) in enumerate(said, start=1)
                ],
            ),
        }
    )


def model_saying(*requests: dict) -> ScriptedModel:
    return ScriptedModel(responses=[json.dumps({"requests": list(requests)})])


def asked_for(intent, quote, confidence, speaker="alice") -> dict:
    return {"intent": intent, "quote": quote, "speaker": speaker,
            "confidence": confidence}


class RecordingGenerate:
    def __init__(self):
        self.calls: list[tuple] = []

    def __call__(self, model, intent, **kwargs):
        self.calls.append((intent, kwargs))
        return {"intent": intent}


def test_a_missing_token_exits_2_and_names_the_variable(capsys):
    code = main(["poll"], transport=transport(), model=ScriptedModel(), env={})

    assert code == 2
    assert "MEET_ACCESS_TOKEN" in capsys.readouterr().err


def test_a_missing_api_key_is_a_configuration_error(capsys, monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    code = main(["poll"], transport=transport(), env=env())

    assert code == 2
    assert "GOOGLE_API_KEY" in capsys.readouterr().err


def test_one_poll_prints_the_built_and_the_declined_request(capsys):
    generate = RecordingGenerate()
    code = main(
        ["poll"],
        transport=transport(),
        model=model_saying(
            asked_for("a 3.3V LDO board", REGULATOR_QUOTE, 0.9),
            asked_for("a USB-C breakout", BREAKOUT_QUOTE, 0.3, speaker="bob"),
        ),
        generate=generate,
        env=env(),
    )
    out = capsys.readouterr().out

    assert code == 0
    assert [intent for intent, _ in generate.calls] == ["a 3.3V LDO board"]
    assert f"{CONF}: 2 request(s), 1 built, 1 skipped" in out
    # Both requests appear with their verbatim quotes, and the skipped one
    # says why -- the floor and the confidence -- rather than vanishing.
    assert f'quote: "{REGULATOR_QUOTE}"' in out
    assert f'quote: "{BREAKOUT_QUOTE}"' in out
    assert "built:" in out
    assert "skipped: confidence 0.30 is below the 0.60 floor" in out
    # No --seen-file: the driver says its memory will not survive the process.
    assert "process-local" in out


def test_a_seen_file_stops_a_second_run_from_repeating_the_meeting(tmp_path, capsys):
    seen_file = tmp_path / "seen.json"
    generate = RecordingGenerate()
    args = ["poll", "--seen-file", str(seen_file)]
    model = model_saying(asked_for("a 3.3V LDO board", REGULATOR_QUOTE, 0.9))

    assert main(args, transport=transport(), model=model, generate=generate,
                env=env()) == 0
    assert json.loads(seen_file.read_text()) == [CONF]
    capsys.readouterr()

    second = FakeTransport({"conferenceRecords": page("conferenceRecords", [
        {"name": CONF, "space": "spaces/aaa", "endTime": recent()}
    ])})
    assert main(args, transport=second, model=ScriptedModel(), generate=generate,
                env=env()) == 0

    # The transcript was never fetched and nothing was built a second time --
    # a re-run is a paid pipeline run -- and the empty poll says so.
    assert len(generate.calls) == 1
    assert [u for u in second.urls if "/transcripts" in u] == []
    out = capsys.readouterr().out
    assert "no unhandled conference" in out
    assert "1 already handled" in out


def test_every_keeps_polling_until_interrupted():
    slept: list[float] = []
    fake = transport()

    def sleep(seconds):
        slept.append(seconds)
        if len(slept) == 2:
            raise KeyboardInterrupt

    code = main(
        ["poll", "--every", "30"],
        transport=fake, model=model_saying(), generate=RecordingGenerate(),
        env=env(), sleep=sleep,
    )

    assert code == 0
    assert slept == [30.0, 30.0]
    # Two polls before the interrupt, one conference list each; the meeting
    # itself is read once -- the second tick finds it already seen.
    assert len(fake.calls_matching("conferenceRecords?")) == 2
    # Counted by prefix: every list call now carries an explicit `pageSize`
    # (see meet.PAGE_SIZE), so an exact-URL match would pin the query string
    # rather than the fact being asserted, which is "the transcript was read
    # once".
    assert sum(
        url.startswith(f"{BASE}/{CONF}/transcripts") for url in fake.urls
    ) == 2  # the transcripts list, then that transcript's entries


def test_a_non_positive_interval_is_refused(capsys):
    code = main(["poll", "--every", "0"], transport=transport(),
                model=ScriptedModel(), env=env())

    assert code == 2
    assert "--every" in capsys.readouterr().err


def test_a_meet_api_failure_exits_1_with_the_reason(capsys):
    failing = FakeTransport({
        "conferenceRecords": (403, {"error": {"message": "insufficient scope"}}),
    })

    code = main(["poll"], transport=failing, model=ScriptedModel(),
                generate=RecordingGenerate(), env=env())

    assert code == 1
    err = capsys.readouterr().err
    assert "403" in err
    assert "insufficient scope" in err


def test_output_gives_every_drafted_board_its_own_directory(tmp_path):
    generate = RecordingGenerate()
    main(
        ["poll", "--output", str(tmp_path), "--no-review"],
        transport=transport(),
        model=model_saying(
            asked_for("a 3.3V LDO board", REGULATOR_QUOTE, 0.9),
            asked_for("a USB-C breakout", BREAKOUT_QUOTE, 0.8, speaker="bob"),
        ),
        generate=generate,
        env=env(),
    )

    outputs = [kwargs["output"] for _, kwargs in generate.calls]
    assert outputs == [
        tmp_path / "001-a-3-3v-ldo-board" / "board.kicad_pcb",
        tmp_path / "002-a-usb-c-breakout" / "board.kicad_pcb",
    ]
    assert all(path.parent.is_dir() for path in outputs)
    assert generate.calls[0][1]["review"] is False
    assert generate.calls[0][1]["route"] is True


def test_a_meeting_with_no_request_is_still_reported(capsys):
    code = main(["poll"], transport=transport(), model=model_saying(),
                generate=RecordingGenerate(), env=env())

    assert code == 0
    assert f"{CONF}: no board request in this meeting" in capsys.readouterr().out


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
