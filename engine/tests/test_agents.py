"""Agent stages, driven by a scripted model.

No network, no API key. The point of the seam in :mod:`silkscreen.agents.model`
is that the whole prompt-to-PCB pipeline can be exercised deterministically,
including the failure paths that only ever fire against a badly-behaved model.
"""

from __future__ import annotations

import json

import pytest
from silkscreen.agents import (
    ModelError,
    ScriptedModel,
    generate_pcb,
    propose_circuit,
    read_datasheet,
    review_circuit,
)
from silkscreen.agents.datasheet import MAX_INLINE_PDF_BYTES
from silkscreen.agents.grounding import GroundingError
from silkscreen.agents.pipeline import MAX_RESPONSE_TEXT
from silkscreen.agents.propose import ProposalError
from silkscreen.agents.review import (
    ReviewError,
    ReviewReport,
    ReviewStatus,
    Severity,
)
from silkscreen.agents.stages import read_stage, review_stage
from silkscreen.netlist import parse_circuit_spec

# ---------------------------------------------------------------- fixtures


# This file is the SDK driver's suite; the ADK driver's is test_adk.py.
@pytest.fixture(autouse=True)
def _pin_sdk_engine(monkeypatch):
    monkeypatch.setenv("SILKSCREEN_ENGINE", "sdk")


GOOD_CIRCUIT = {
    "devices": {
        "AMS1117-3.3": {"pins": {"GND": "1", "VOUT": "2", "VIN": "3"}},
        "DRV8837": {"pins": {"IN1": "1", "IN2": "2", "VM": "3", "GND": "4",
                             "OUT1": "5", "OUT2": "6", "VCC": "7", "nSLEEP": "8"}},
    },
    "passives": {
        "c_in": {"type": "capacitor", "value": "22uF"},
        "c_out": {"type": "capacitor", "value": "22uF"},
        "c_dec": {"type": "capacitor", "value": "100nF"},
        "r_sleep": {"type": "resistor", "value": "10k"},
    },
    "nets": {
        "VIN": ["AMS1117-3.3.VIN", "c_in.1", "DRV8837.VM"],
        "GND": ["AMS1117-3.3.GND", "DRV8837.GND", "c_in.2", "c_out.2", "c_dec.2"],
        "+3V3": ["AMS1117-3.3.VOUT", "DRV8837.VCC", "c_out.1", "c_dec.1",
                 "r_sleep.1"],
        "SLEEP": ["DRV8837.nSLEEP", "r_sleep.2"],
        "MOT": ["DRV8837.OUT1", "DRV8837.IN1"],
    },
}

DATASHEET_JSON = {
    "part_number": "AMS1117-3.3",
    "package": "SOT-223-3",
    "pin_count": 3,
    "pins": [
        {"number": "1", "name": "GND", "kind": "ground", "page": 1},
        {"number": "2", "name": "VOUT", "kind": "output", "page": 1},
        {"number": "3", "name": "VIN", "kind": "power", "page": 1},
    ],
    "requirements": [
        {"requirement": "Output capacitor must be >= 22uF tantalum", "page": 9}
    ],
    "auxiliaries": [
        {"name": "c_out", "type": "capacitor", "value": "22uF",
         "connects": "VOUT to GND", "why": "loop stability", "page": 9}
    ],
    "notes": "",
}


# ---------------------------------------------------------------- datasheet


def _pdf(url: str, **kwargs: object) -> bytes:
    """Stand in for the download, so these stay tests of read_datasheet.

    A pdf_url is fetched now rather than handed to the provider, so without
    this every case below would resolve a hostname.
    """
    return b"%PDF-1.4 stub datasheet"


def test_read_datasheet_extracts_pins_and_citations():
    model = ScriptedModel(responses=[json.dumps(DATASHEET_JSON)])
    facts = read_datasheet(
        model, "AMS1117-3.3", pdf_url="https://x/ams1117.pdf", fetch=_pdf
    )
    assert facts.pin_count == 3
    assert facts.pin_map() == {"GND": "1", "VOUT": "2", "VIN": "3"}
    assert facts.requirements[0]["page"] == 9


def test_read_datasheet_downloads_the_url_and_sends_bytes():
    """The provider is handed the PDF, never the link to it.

    Gemini does not fetch arbitrary URLs -- its ``file_uri`` takes a Files API
    URI or a YouTube link. A public datasheet URL comes back as a bare 429
    RESOURCE_EXHAUSTED with no quota metric, which reads as an exhausted key and
    survives every failover attempt, so this assertion is the whole fix.
    """
    model = ScriptedModel(responses=[json.dumps(DATASHEET_JSON)])
    read_datasheet(
        model,
        "AMS1117-3.3",
        pdf_url="https://x/ams1117.pdf",
        fetch=lambda url, **kw: b"%PDF-1.4 fake",
    )
    doc = model.calls[0]["documents"][0]
    assert doc.data == b"%PDF-1.4 fake"
    assert doc.url is None


def test_read_datasheet_caps_what_it_will_download():
    """Bigger than one request can carry has to fail here, not at the API."""
    seen: dict = {}

    def fetch(url, **kwargs):
        seen.update(url=url, **kwargs)
        return b"%PDF-1.4 fake"

    read_datasheet(
        ScriptedModel(responses=[json.dumps(DATASHEET_JSON)]),
        "AMS1117-3.3",
        pdf_url="https://x/a.pdf",
        fetch=fetch,
    )
    assert seen == {"url": "https://x/a.pdf", "max_bytes": MAX_INLINE_PDF_BYTES}


def test_read_datasheet_does_not_download_when_given_bytes():
    def explode(url, **kwargs):  # pragma: no cover - must never run
        raise AssertionError(f"fetched {url} despite being handed bytes")

    model = ScriptedModel(responses=[json.dumps(DATASHEET_JSON)])
    read_datasheet(
        model, "AMS1117-3.3", pdf_bytes=b"%PDF-1.4 given", fetch=explode
    )
    assert model.calls[0]["documents"][0].data == b"%PDF-1.4 given"


def test_read_datasheet_refuses_a_url_that_serves_html():
    """Regression: a .pdf URL that answers 200 text/html.

    LCSC's AMS1117 link redirects to a product page and returns 126kB of markup
    from a URL ending in .pdf. Sent onward as application/pdf that is a 400 from
    the API naming neither the part nor the URL, so the check belongs here.
    """
    model = ScriptedModel(responses=[json.dumps(DATASHEET_JSON)])
    with pytest.raises(GroundingError, match="did not return a PDF"):
        read_datasheet(
            model,
            "AMS1117-3.3",
            pdf_url="https://datasheet.lcsc.com/lcsc/x_C6186.pdf",
            fetch=lambda url, **kw: b"<!doctype html><html><head><title>LCSC",
        )
    assert model.calls == [], "a non-PDF must not reach the model"


def test_read_datasheet_tolerates_a_code_fence():
    fenced = "```json\n" + json.dumps(DATASHEET_JSON) + "\n```"
    model = ScriptedModel(responses=[fenced])
    facts = read_datasheet(
        model, "AMS1117-3.3", pdf_url="https://x/a.pdf", fetch=_pdf
    )
    assert facts.pin_count == 3


def test_read_datasheet_refuses_a_part_with_no_pinout():
    """Placing a part whose pinout is unknown is worse than failing."""
    empty = dict(DATASHEET_JSON, pins=[])
    model = ScriptedModel(responses=[json.dumps(empty)])
    with pytest.raises(ModelError, match="No pins extracted"):
        read_datasheet(
            model, "AMS1117-3.3", pdf_url="https://x/a.pdf", fetch=_pdf
        )


def test_pin_count_disagreement_is_flagged_not_hidden():
    mismatched = dict(DATASHEET_JSON, pin_count=8)
    model = ScriptedModel(responses=[json.dumps(mismatched)])
    facts = read_datasheet(
        model, "AMS1117-3.3", pdf_url="https://x/a.pdf", fetch=_pdf
    )
    assert "package choice may be wrong" in facts.notes


def test_read_datasheet_needs_a_document():
    with pytest.raises(ValueError, match="pdf_url or pdf_bytes"):
        read_datasheet(ScriptedModel(), "X")


# ---------------------------------------------------- read_stage (R11)


def _parts_facts_json(part_number: str) -> str:
    """A minimal, valid datasheet response naming ``part_number``.

    Keyed by the part number as the ``ScriptedModel`` marker: the shared
    ``DATASHEET_PROMPT`` text is identical for every part, so the part number
    -- appended once, at the end, by ``read_datasheet`` -- is the only
    substring that safely disambiguates one part's scripted response from
    another's.
    """
    return json.dumps(dict(DATASHEET_JSON, part_number=part_number))


def test_read_stage_reads_several_parts_and_is_order_independent(offline_pdf_fetch):
    """asyncio.gather does not guarantee completion order -- this must not care.

    It asserts the *set* of parts read and their facts, plus the boundary
    events, rather than a fixed interleaving of ``read.part``/``read.fetch``
    across parts: that interleaving is exactly what concurrency makes
    non-deterministic, while the facts a caller actually designs from must
    come back complete and correct regardless.
    """
    parts = ["AMS1117-3.3", "DRV8837", "TPS54331"]
    model = ScriptedModel(by_marker={p: _parts_facts_json(p) for p in parts})
    sheets = {p: f"https://x/{p}.pdf" for p in parts}
    events: list[dict] = []

    facts = read_stage(
        model,
        sheets=sheets,
        preloaded_facts=None,
        emit=events.append,
        enter=lambda stage: None,
    )

    assert {f.part_number for f in facts} == set(parts)
    assert events[0]["event"] == "stage.start"
    assert events[-1] == {
        "event": "stage.done",
        "stage": "read",
        "parts": 3,
        "pins": sum(len(f.pins) for f in facts),
        "requirements": sum(len(f.requirements) for f in facts),
    }
    read_parts = {e["part"] for e in events if e["event"] == "read.part"}
    fetch_parts = {e["part"] for e in events if e["event"] == "read.fetch"}
    assert read_parts == set(parts)
    assert fetch_parts == set(parts)
    # One model call, one fetch per part -- concurrency changes when these
    # interleave, not how many of them there are.
    assert len(model.calls) == 3


def test_read_stage_isolates_one_bad_part_from_the_rest(offline_pdf_fetch):
    """One part's read failing must not sink the parts that read fine.

    Matches the partial-failure convention ``propose_circuit``'s batched
    repair loop and ``review_circuit``'s finding filter both apply: a bad
    unit of work is reported and dropped, not allowed to fail units that had
    nothing wrong with them.
    """
    model = ScriptedModel(by_marker={
        "AMS1117-3.3": _parts_facts_json("AMS1117-3.3"),
        # No pins at all -- read_datasheet raises ModelError for this part.
        "DRV8837": json.dumps(dict(DATASHEET_JSON, part_number="DRV8837", pins=[])),
    })
    sheets = {
        "AMS1117-3.3": "https://x/ams1117.pdf",
        "DRV8837": "https://x/drv8837.pdf",
    }
    events: list[dict] = []

    facts = read_stage(
        model,
        sheets=sheets,
        preloaded_facts=None,
        emit=events.append,
        enter=lambda stage: None,
    )

    assert [f.part_number for f in facts] == ["AMS1117-3.3"]
    failed = [e for e in events if e["event"] == "read.failed"]
    assert len(failed) == 1
    assert failed[0]["part"] == "DRV8837"
    assert "No pins extracted" in failed[0]["error"]
    assert events[-1]["event"] == "stage.done"
    assert events[-1]["parts"] == 1


def test_read_stage_raises_when_every_part_fails(offline_pdf_fetch):
    """A batch of one behaves exactly as it did before concurrency landed."""
    model = ScriptedModel(by_marker={
        "AMS1117-3.3": json.dumps(dict(DATASHEET_JSON, pins=[])),
    })
    with pytest.raises(ModelError, match="No pins extracted"):
        read_stage(
            model,
            sheets={"AMS1117-3.3": "https://x/ams1117.pdf"},
            preloaded_facts=None,
            emit=lambda event: None,
            enter=lambda stage: None,
        )


# ---------------------------------------------------------------- propose


def test_propose_accepts_a_valid_circuit_first_try():
    model = ScriptedModel(responses=[json.dumps(GOOD_CIRCUIT)])
    spec, attempts = propose_circuit(model, "a motor driver")
    assert spec.part_count() == 6
    assert len(attempts) == 1 and attempts[0].accepted


def test_propose_repairs_an_invalid_circuit():
    """The repair loop is the whole point: the model gets its errors back."""
    broken = json.loads(json.dumps(GOOD_CIRCUIT))
    broken["nets"]["GND"] = ["AMS1117-3.3.GND", "DRV8837.GND"]  # caps now floating
    model = ScriptedModel(responses=[json.dumps(broken), json.dumps(GOOD_CIRCUIT)])

    spec, attempts = propose_circuit(model, "a motor driver")
    assert spec.part_count() == 6
    assert len(attempts) == 2
    assert not attempts[0].accepted and attempts[1].accepted
    assert any("floating" in e for e in attempts[0].errors)


def test_repair_prompt_contains_every_error():
    broken = json.loads(json.dumps(GOOD_CIRCUIT))
    broken["nets"]["VIN"] = ["AMS1117-3.3.NOPE", "c_in.1", "DRV8837.VM"]
    model = ScriptedModel(responses=[json.dumps(broken), json.dumps(GOOD_CIRCUIT)])
    propose_circuit(model, "a motor driver")
    repair_prompt = model.calls[1]["prompt"]
    assert "rejected" in repair_prompt
    assert "NOPE" in repair_prompt


def test_propose_gives_up_loudly_after_the_repair_budget():
    broken = json.loads(json.dumps(GOOD_CIRCUIT))
    broken["nets"]["GND"] = ["AMS1117-3.3.GND", "DRV8837.GND"]
    model = ScriptedModel(responses=[json.dumps(broken)] * 3)
    with pytest.raises(ProposalError, match="No valid circuit after"):
        propose_circuit(model, "a motor driver", max_repairs=2)


def test_propose_passes_datasheet_facts_into_the_prompt():
    model = ScriptedModel(responses=[json.dumps(DATASHEET_JSON),
                                     json.dumps(GOOD_CIRCUIT)])
    facts = [
        read_datasheet(
            model, "AMS1117-3.3", pdf_url="https://x/a.pdf", fetch=_pdf
        )
    ]
    propose_circuit(model, "a regulator", facts=facts)
    prompt = model.calls[1]["prompt"]
    assert "22uF tantalum" in prompt and "p.9" in prompt


# ---------------------------------------------------------------- review


def test_review_returns_findings_sorted_by_severity():
    response = json.dumps({"findings": [
        {"severity": "note", "title": "Silkscreen could be clearer", "detail": "",
         "parts": []},
        {"severity": "blocker", "title": "nSLEEP left floating",
         "detail": "The driver will not enable.", "parts": ["r_sleep"],
         "citation": "DRV8837 p.8", "suggested_fix": "Pull to VCC"},
        {"severity": "marginal", "title": "Decoupling is far from the pin",
         "detail": "", "parts": ["c_dec"]},
    ]})
    model = ScriptedModel(responses=[response])
    spec = parse_circuit_spec(GOOD_CIRCUIT)
    findings, _dropped = review_circuit(model, spec)
    assert [f.severity for f in findings] == [
        Severity.BLOCKER, Severity.MARGINAL, Severity.NOTE
    ]
    assert findings[0].citation == "DRV8837 p.8"


def test_review_drops_findings_that_reference_nonexistent_parts():
    """A finding pointing at a part that isn't on the board helps nobody."""
    response = json.dumps({"findings": [
        {"severity": "blocker", "title": "U99 is miswired", "detail": "",
         "parts": ["U99", "c_dec"]},
    ]})
    model = ScriptedModel(responses=[response])
    findings, dropped = review_circuit(model, parse_circuit_spec(GOOD_CIRCUIT))
    assert findings[0].parts == ("c_dec",)
    assert dropped == ()


def test_review_drops_a_finding_whose_every_part_is_invented():
    """A blocker that names nothing real must not reach the result.

    Kept with an empty ``parts`` it was an unlocatable blocker: it books a
    Calendar meeting, reports the board not-orderable and blocks the desktop
    order step, over a part the board does not have. Dropping it silently
    would be the other half of the bug, so the drop is reported.
    """
    response = json.dumps({"findings": [
        {"severity": "blocker", "title": "U7 enable pin floats", "detail": "",
         "parts": ["U7", "R9"]},
    ]})
    model = ScriptedModel(responses=[response])
    findings, dropped = review_circuit(model, parse_circuit_spec(GOOD_CIRCUIT))
    assert findings == []
    assert len(dropped) == 1
    assert "U7" in dropped[0] and "R9" in dropped[0]


def test_review_reports_an_entry_it_could_not_read():
    """Two of three findings malformed must not read as one finding found."""
    response = json.dumps({"findings": [
        {"severity": "note", "title": "Real one", "detail": "", "parts": []},
        {"severity": "blocker", "detail": "no title"},
        "not an object",
    ]})
    model = ScriptedModel(responses=[response])
    findings, dropped = review_circuit(model, parse_circuit_spec(GOOD_CIRCUIT))
    assert [f.title for f in findings] == ["Real one"]
    assert len(dropped) == 2


def test_review_handles_a_clean_result():
    model = ScriptedModel(responses=[json.dumps({"findings": []})])
    assert review_circuit(model, parse_circuit_spec(GOOD_CIRCUIT)) == ([], ())


def test_review_prompt_asks_the_model_to_refute():
    """An agent asked 'is this correct?' says yes."""
    model = ScriptedModel(responses=[json.dumps({"findings": []})])
    review_circuit(model, parse_circuit_spec(GOOD_CIRCUIT))
    prompt = model.calls[0]["prompt"].lower()
    assert "wrong" in prompt and "do not compliment" in prompt


def test_empty_circuit_is_rejected():
    """A model that returns the wrong shape must not yield an empty board."""
    from silkscreen.netlist import ValidationError, parse_circuit_spec

    with pytest.raises(ValidationError, match="no devices and no passives"):
        parse_circuit_spec({"not": "a circuit"})


def test_propose_rejects_a_response_that_is_not_a_circuit():
    model = ScriptedModel(responses=[json.dumps({"part_number": "AMS1117"})] * 3)
    with pytest.raises(ProposalError):
        propose_circuit(model, "x", max_repairs=1)


def test_an_unreadable_critic_answer_raises_rather_than_reading_as_clean():
    """This test previously asserted ``review_circuit(...) == []``.

    That assertion was wrong, and pinning it was the bug. It made this case --
    the critic answered something with no findings list in it -- return the
    identical value to ``test_review_handles_a_clean_result`` two functions
    up, so no consumer downstream could tell "the critic reviewed this board
    and found nothing" from "the critic said something unreadable". What was
    printed for the second fact was the first fact's sentence: a "board ready"
    Gmail subject, a skipped design-review invite (the blockers-only rule sees
    zero blockers), a Slack thread with a green tick. ``audit/report.py``
    already forbids exactly this -- an empty finding list must not read as a
    clean board -- and the engine now agrees with it.
    """
    model = ScriptedModel(responses=['{"findings": "not a list"}'])
    with pytest.raises(ReviewError, match="no 'findings' list"):
        review_circuit(model, parse_circuit_spec(GOOD_CIRCUIT))


def test_an_answer_that_is_not_json_at_all_is_the_same_review_failure():
    """One ``except ReviewError`` has to cover every unreadable answer.

    Two ways to be unreadable that need two different handlers is two chances
    to forget one, and the forgotten one is a quiet zero.
    """
    model = ScriptedModel(responses=["I am afraid I cannot help with that."])
    with pytest.raises(ReviewError, match="not JSON"):
        review_circuit(model, parse_circuit_spec(GOOD_CIRCUIT))


def test_a_list_response_is_a_review_failure_not_an_empty_review():
    model = ScriptedModel(responses=["[]"])
    with pytest.raises(ReviewError):
        review_circuit(model, parse_circuit_spec(GOOD_CIRCUIT))


def _review_stage(response: str, *, review: bool = True):
    events: list[dict] = []
    return (
        review_stage(
            ScriptedModel(responses=[response]),
            parse_circuit_spec(GOOD_CIRCUIT),
            facts=[],
            review=review,
            emit=events.append,
            enter=lambda _stage: None,
        ),
        events,
    )


def test_review_stage_reports_a_failed_critic_without_losing_the_run():
    report, events = _review_stage('{"findings": "not a list"}')
    assert report.status is ReviewStatus.FAILED
    assert report.failed and not report.ok and report.ran
    assert report.findings == ()
    assert "no 'findings' list" in report.detail
    # The event stream says it too: a stage.done carrying findings 0 with no
    # status is exactly the shape a client reads as a clean board.
    assert any(e["event"] == "review.failed" for e in events)
    done = next(e for e in events if e["event"] == "stage.done")
    assert done["status"] == "failed"


def test_the_three_review_outcomes_are_three_different_sentences():
    failed, _ = _review_stage('{"findings": "not a list"}')
    clean, _ = _review_stage(json.dumps({"findings": []}))
    skipped, _ = _review_stage(json.dumps({"findings": []}), review=False)
    notes = {failed.note(), clean.note(), skipped.note()}
    assert len(notes) == 3
    assert clean.note() == "the review ran and found nothing"
    for outcome in (failed, skipped):
        assert "nothing is known about this board" in outcome.note()
        assert "found nothing" not in outcome.note()
    assert skipped.status is ReviewStatus.SKIPPED and not skipped.ran


def test_a_clean_review_is_the_only_outcome_that_is_ok():
    clean, events = _review_stage(json.dumps({"findings": []}))
    assert clean.ok and clean.ran and clean.findings == ()
    assert not any(e["event"] == "review.failed" for e in events)
    assert next(e for e in events if e["event"] == "stage.done")["status"] == "ok"


def test_a_review_report_defaults_to_knowing_nothing():
    """The default must be ``skipped``, never ``ok``.

    A ``PipelineResult`` assembled without a review pass has not been argued
    against, and a default of ``ok`` would make every such result claim a
    clean review it never had.
    """
    assert ReviewReport().status is ReviewStatus.SKIPPED
    assert not ReviewReport().ok


# ---------------------------------------------------------------- pipeline


def test_full_pipeline_prompt_to_board(tmp_path, offline_pdf_fetch):
    """intent -> datasheet -> propose -> place -> .kicad_pcb -> review."""
    review_json = json.dumps({"findings": [
        {"severity": "blocker", "title": "Output cap is ceramic, not tantalum",
         "detail": "The AMS1117 loop needs ESR the ceramic does not provide.",
         "parts": ["c_out"], "citation": "AMS1117 p.9",
         "suggested_fix": "Use a 22uF tantalum"},
    ]})
    # Markers must be unique to one stage: "datasheet" alone also appears in
    # the proposal prompt, which silently routed the wrong response.
    model = ScriptedModel(by_marker={
        "reading an electronic component datasheet": json.dumps(DATASHEET_JSON),
        "designing a printed circuit board": json.dumps(GOOD_CIRCUIT),
        "reviewing a circuit someone else designed": review_json,
    })

    out = tmp_path / "board.kicad_pcb"
    result = generate_pcb(
        model,
        "a 3.3V motor driver board",
        datasheets={"AMS1117-3.3": "https://x/ams1117.pdf"},
        output=out,
        time_limit_s=15.0,
    )

    assert result.board_path == out and out.exists()
    assert len(result.board.parts) == 6
    assert len(result.facts) == 1
    assert len(result.blockers) == 1
    assert result.repair_rounds == 0
    assert result.placement is None
    assert "parts" in result.summary()

    from kiutils.board import Board
    assert len(Board.from_file(str(out)).footprints) == 6


def test_integrated_placement_runs_before_artifacts_and_copper(tmp_path):
    """Placement policy calls are visible and finish before later board stages."""
    worker = ScriptedModel(responses=[json.dumps(GOOD_CIRCUIT)])
    placement_model = ScriptedModel(responses=["NOOP"])
    events = []

    result = generate_pcb(
        worker,
        "a motor driver",
        output=tmp_path / "board.kicad_pcb",
        review=False,
        time_limit_s=10.0,
        placement_profile="compact-control",
        placement_policy="gemini",
        placement_feedback={"weights": {"compactness_weight": 1.25}},
        placement_model=placement_model,
        placement_max_turns=1,
        on_event=events.append,
        include_responses=True,
    )

    assert result.placement is not None
    assert result.placement.applied is result.placement.run.completed
    assert result.placement.requested_policy == "gemini"
    assert result.placement.run.profile.compactness_weight == 1.25
    assert result.route is not None
    assert result.placed_board_path is not None

    assert [e["stage"] for e in events if e["event"] == "stage.start"] == [
        "propose",
        "place",
        "placement_repair",
        "schematic",
        "route",
    ]
    requests = [
        e
        for e in events
        if e["event"] == "model.request" and e["stage"] == "placement_repair"
    ]
    responses = [
        e
        for e in events
        if e["event"] == "model.response" and e["stage"] == "placement_repair"
    ]
    assert len(requests) == len(responses) == 1
    assert requests[0]["call_id"].startswith("placement-")
    assert requests[0]["call_id"] == responses[0]["call_id"]
    assert "PCB PLACEMENT REPAIR" in requests[0]["prompt"]
    assert responses[0]["text"] == "NOOP"


def test_pipeline_reports_repair_rounds(tmp_path):
    broken = json.loads(json.dumps(GOOD_CIRCUIT))
    broken["nets"]["GND"] = ["AMS1117-3.3.GND", "DRV8837.GND"]
    model = ScriptedModel(responses=[json.dumps(broken), json.dumps(GOOD_CIRCUIT),
                                     json.dumps({"findings": []})])
    result = generate_pcb(model, "a motor driver", output=tmp_path / "b.kicad_pcb",
                          time_limit_s=10.0)
    assert result.repair_rounds == 1
    assert "1 repair round" in result.summary()


def test_pipeline_can_skip_review(tmp_path):
    model = ScriptedModel(responses=[json.dumps(GOOD_CIRCUIT)])
    result = generate_pcb(model, "x", output=tmp_path / "b.kicad_pcb",
                          review=False, time_limit_s=10.0)
    assert result.findings == []
    assert len(model.calls) == 1


def test_transport_failure_is_not_a_proposal_failure():
    """An upstream outage and a bad proposal are different conditions.

    Wrapping the first in the second loses the distinction, and a caller that
    must choose between "retry later" and "give up" cannot tell them apart --
    an HTTP service deciding 502 versus 500, for instance.
    """
    class Dead:
        def generate(self, *args, **kwargs):
            raise ModelError("upstream 503")

    with pytest.raises(ModelError, match="503"):
        propose_circuit(Dead(), "a motor driver")


# ---------------------------------------------------------------- events


#: A plan the planning stage will accept. Every scripted pipeline model needs
#: one now that planning is on by default in the CLI -- keyed by marker, so
#: adding a stage does not renumber anybody's script.
PLAN_JSON = {   'building': 'the board the request describes',
    'power': {   'source': 'barrel_jack',
                 'input_voltage': '12V',
                 'connector_package': 'Barrel_Jack_5.5x2.1mm',
                 'battery_package': None,
                 'notes': 'wall adapter, centre positive'},
    'rails': [   {   'name': '+3V3',
                     'voltage': '3.3V',
                     'derived_from': 'VIN',
                     'purpose': 'logic'}],
    'blocks': [   {   'name': 'regulation',
                      'purpose': 'step the input down to 3.3V',
                      'rail': '+3V3'}],
    'connectivity': [],
    'assumptions': [   'mains powered, since the request did not say '
                       'otherwise'],
    'open_questions': []}


def _scripted_pipeline_model():
    """The full-pipeline scripted model, reused by the event tests."""
    review_json = json.dumps({"findings": [
        {"severity": "blocker", "title": "Output cap is ceramic, not tantalum",
         "detail": "The AMS1117 loop needs ESR the ceramic does not provide.",
         "parts": ["c_out"], "citation": "AMS1117 p.9",
         "suggested_fix": "Use a 22uF tantalum"},
    ]})
    from silkscreen.agents.plan import PLAN_MARKER

    return ScriptedModel(by_marker={
        PLAN_MARKER: json.dumps(PLAN_JSON),
        "reading an electronic component datasheet": json.dumps(DATASHEET_JSON),
        "designing a printed circuit board": json.dumps(GOOD_CIRCUIT),
        "reviewing a circuit someone else designed": review_json,
    })


def test_events_trace_every_stage_and_model_call(tmp_path, offline_pdf_fetch):
    """The stream is a progress signal: one event per boundary, no payload."""
    model = _scripted_pipeline_model()
    events = []

    generate_pcb(
        model,
        "a 3.3V motor driver board",
        datasheets={"AMS1117-3.3": "https://x/ams1117.pdf"},
        output=tmp_path / "board.kicad_pcb",
        time_limit_s=15.0,
        on_event=events.append,
    )

    # The critic runs on its own thread from the moment the spec validates,
    # so its three frames land somewhere inside place's solve in wall-clock
    # order -- the enclosure and sourcing lanes' rule, now applying to a
    # stage that is on by default. Within each lane the order is frozen;
    # across lanes only the bounds are, so the review lane is split out and
    # the main line asserted whole.
    review = [e for e in events if e.get("stage") == "review"]
    main = [e for e in events if e.get("stage") != "review"]
    assert [e["event"] for e in review] == [
        "stage.start", "model.call", "stage.done",
    ]
    assert [e["event"] for e in main] == [
        # First frame of every run: which effort level produced what follows.
        # A stream captured mid-run still carries the level at its head, and
        # a client can label its progress rail before a stage has started.
        "effort.selected",
        "stage.start", "read.part", "read.fetch", "model.call", "stage.done",
        "stage.start", "model.call", "stage.done",
        "stage.start", "stage.done",
        "stage.start", "stage.done",
        "stage.start", "stage.done",
    ]
    # Schematic and route are stages like any other: each opens and closes, so
    # a client ticking a stage list never sees a close it has no open for.
    assert [e["stage"] for e in main if e["event"].startswith("stage.")] == [
        "read", "read", "propose", "propose", "place", "place",
        "schematic", "schematic", "route", "route",
    ]
    # The critic is joined before schematic is drawn, which is the whole
    # point: its latency now sits inside the solver budget instead of on the
    # tail of the run.
    assert events.index(review[-1]) < next(
        i for i, e in enumerate(events)
        if e["event"] == "stage.start" and e.get("stage") == "schematic"
    )
    # And the reason the critic is allowed on a thread at all: its call still
    # comes out in the one frozen worker order, so a ScriptedModel answers
    # this run the same way twice.
    assert_worker_call_order(events)
    assert all(isinstance(e["t_s"], (int, float)) for e in events)
    assert len([e for e in events if e["event"] == "model.call"]) == len(model.calls)

    # No event may carry board text, model output or datasheet text.
    assert [e for e in events if e["event"] == "model.response"] == [], (
        "raw model output is opt-in: a default stream carries none of it"
    )
    for event in events:
        assert "kicad_pcb" not in event
        for value in event.values():
            assert not (isinstance(value, str) and len(value) > 500)


def test_the_event_name_set_is_frozen(tmp_path, offline_pdf_fetch):
    """Every event name a client can be sent, in one assertion.

    ``frontend/src/lib/stream.js`` switches on these strings to turn a frame
    into a sentence, so a renamed or added event is a silent regression over
    there rather than a failure here -- unless this set is what has to change.
    """
    broken = json.loads(json.dumps(GOOD_CIRCUIT))
    broken["nets"]["GND"] = ["AMS1117-3.3.GND", "DRV8837.GND"]
    # One datasheet and one repair round, so every unconditional event fires.
    model = ScriptedModel(responses=[
        json.dumps(DATASHEET_JSON), json.dumps(broken), json.dumps(GOOD_CIRCUIT),
        json.dumps({"findings": []}),
    ])
    events = []
    generate_pcb(model, "a 3.3V motor driver board",
                 datasheets={"AMS1117-3.3": "https://x/ams1117.pdf"},
                 output=tmp_path / "b.kicad_pcb",
                 time_limit_s=10.0, on_event=events.append)

    assert {e["event"] for e in events} == {
        "effort.selected",
        "stage.start", "stage.done", "read.part", "read.fetch",
        "propose.round", "model.call",
    }
    # The two conditional names have their own tests here: model.response fires
    # only under include_responses, model.retry only behind a failover model.


def test_response_events_carry_each_answer_verbatim(tmp_path, offline_pdf_fetch):
    """The debug stream: what the model actually said, attributed to its stage.

    A response follows its own call immediately, so a client reading the feed
    in order never has to guess which round-trip an answer belongs to.
    """
    model = _scripted_pipeline_model()
    events = []

    generate_pcb(
        model,
        "a 3.3V motor driver board",
        datasheets={"AMS1117-3.3": "https://x/ams1117.pdf"},
        output=tmp_path / "board.kicad_pcb",
        time_limit_s=15.0,
        on_event=events.append,
        include_responses=True,
    )

    names = [e["event"] for e in events]
    calls = [i for i, name in enumerate(names) if name == "model.call"]
    assert calls, "the pipeline made no model calls to report"
    assert all(names[i + 1] == "model.response" for i in calls)

    # What the scripted model returned for each prompt it was actually given,
    # so the assertion is about the wrapper rather than about this test's copy.
    answers = [
        next(r for marker, r in model.by_marker.items() if marker in call["prompt"])
        for call in model.calls
    ]
    responses = [e for e in events if e["event"] == "model.response"]
    requests = [e for e in events if e["event"] == "model.request"]
    assert [e["stage"] for e in requests] == ["read", "propose", "review"]
    assert [e["prompt"] for e in requests] == [call["prompt"] for call in model.calls]
    assert [e["call_id"] for e in requests] == [e["call_id"] for e in responses]
    assert [e["stage"] for e in responses] == ["read", "propose", "review"]
    assert [e["text"] for e in responses] == answers
    assert [e["chars"] for e in responses] == [len(a) for a in answers]
    assert all(e["truncated"] is False for e in responses)


def test_a_long_response_is_clipped_and_says_how_long_it_really_was(tmp_path):
    """A model that answers with a wall of text cannot flood the stream.

    The count is the untruncated one: a client that only ever sees the clipped
    text still learns that there was more of it.
    """
    circuit = json.dumps(GOOD_CIRCUIT)
    # Trailing whitespace: still the same JSON, so the pipeline runs as usual.
    padded = circuit + " " * (MAX_RESPONSE_TEXT + 500 - len(circuit))
    model = ScriptedModel(responses=[padded])
    events = []

    generate_pcb(model, "x", output=tmp_path / "b.kicad_pcb", review=False,
                 time_limit_s=10.0, on_event=events.append, include_responses=True)

    responses = [e for e in events if e["event"] == "model.response"]
    assert len(responses) == 1
    assert responses[0]["truncated"] is True
    assert responses[0]["chars"] == MAX_RESPONSE_TEXT + 500
    assert len(responses[0]["text"]) == MAX_RESPONSE_TEXT
    assert responses[0]["text"] == padded[:MAX_RESPONSE_TEXT]


def test_events_report_a_repair_round(tmp_path):
    broken = json.loads(json.dumps(GOOD_CIRCUIT))
    broken["nets"]["GND"] = ["AMS1117-3.3.GND", "DRV8837.GND"]
    model = ScriptedModel(responses=[json.dumps(broken), json.dumps(GOOD_CIRCUIT),
                                     json.dumps({"findings": []})])
    events = []
    generate_pcb(model, "a motor driver", output=tmp_path / "b.kicad_pcb",
                 time_limit_s=10.0, on_event=events.append)

    rounds = [e for e in events if e["event"] == "propose.round"]
    assert len(rounds) == 1
    assert rounds[0]["round"] == 1 and rounds[0]["errors"] > 0
    assert isinstance(rounds[0]["first_error"], str) and rounds[0]["first_error"]

    done = [e for e in events
            if e["event"] == "stage.done" and e["stage"] == "propose"]
    assert done[0]["repair_rounds"] == 1


def test_events_omit_stages_that_did_not_run(tmp_path):
    """A skipped stage emits nothing at all, rather than an empty pair."""
    model = ScriptedModel(responses=[json.dumps(GOOD_CIRCUIT)])
    events = []
    generate_pcb(model, "x", output=tmp_path / "b.kicad_pcb", review=False,
                 time_limit_s=10.0, on_event=events.append)

    assert [e for e in events if e.get("stage") == "review"] == []
    assert [e for e in events if e.get("stage") == "read"] == []
    assert len(model.calls) == 1


def test_pipeline_without_a_callback_behaves_identically(tmp_path):
    model = ScriptedModel(responses=[json.dumps(GOOD_CIRCUIT)])
    result = generate_pcb(model, "x", output=tmp_path / "b.kicad_pcb",
                          review=False, time_limit_s=10.0, on_event=None)
    assert result.findings == []
    assert len(model.calls) == 1
    assert len(result.board.parts) == 6


def test_a_raising_callback_aborts_the_run(tmp_path):
    """A service aborts a run whose client disconnected by raising here."""
    model = _scripted_pipeline_model()

    def hang_up(event):
        raise RuntimeError("client gone")

    with pytest.raises(RuntimeError, match="client gone"):
        generate_pcb(model, "x", output=tmp_path / "b.kicad_pcb",
                     time_limit_s=10.0, on_event=hang_up)


def test_events_surface_a_provider_failover(tmp_path):
    """A failed provider is visible even though the call itself succeeded."""
    from silkscreen.agents.resilience import FallbackModel, Provider

    class Dead:
        def generate(self, *args, **kwargs):
            raise ModelError("upstream 503")

    model = FallbackModel(providers=[
        Provider(name="primary", model=Dead(), attempts=1),
        Provider(name="backup",
                 model=ScriptedModel(responses=[json.dumps(GOOD_CIRCUIT)])),
    ])
    events = []
    generate_pcb(model, "x", output=tmp_path / "b.kicad_pcb", review=False,
                 time_limit_s=10.0, on_event=events.append)

    retries = [e for e in events if e["event"] == "model.retry"]
    assert len(retries) == 1
    assert retries[0]["provider"] == "primary" and retries[0]["stage"] == "propose"
    assert "ModelError" in retries[0]["error"]

    calls = [e for e in events if e["event"] == "model.call"]
    assert len(calls) == 1
    assert calls[0]["ok"] and calls[0]["provider"] == "backup"


def test_the_project_files_sit_beside_a_dotted_board_name(tmp_path, offline_pdf_fetch):
    """The .kicad_pro must name the board that is actually there.

    Stripping every suffix turned "revision.2.kicad_pcb" into the stem
    "revision", so the project and the schematic landed next to a board file
    they did not describe -- and opening the advertised project found no board.
    """
    out = tmp_path / "revision.2.kicad_pcb"
    result = generate_pcb(
        _scripted_pipeline_model(),
        "a 3.3V motor driver board",
        datasheets={"AMS1117-3.3": "https://x/ams1117.pdf"},
        output=out,
        time_limit_s=15.0,
    )

    assert [p.name for p in result.artifacts] == [
        "revision.2.kicad_pro",
        "revision.2.kicad_sch",
        "revision.2.placed.kicad_pcb",
        "revision.2.kicad_pcb",
    ]
    assert all(p.exists() for p in result.artifacts)


def test_routing_can_be_turned_off_without_losing_the_board(
    tmp_path, offline_pdf_fetch
):
    """--no-route leaves a placed board with pads on nets and empty copper."""
    out = tmp_path / "board.kicad_pcb"
    result = generate_pcb(
        _scripted_pipeline_model(),
        "a 3.3V motor driver board",
        datasheets={"AMS1117-3.3": "https://x/ams1117.pdf"},
        output=out,
        time_limit_s=15.0,
        route=False,
    )
    assert result.route is None
    assert result.board.tracks == []
    assert out.exists()


def test_board_only_writes_the_board_and_nothing_else(tmp_path, offline_pdf_fetch):
    out = tmp_path / "board.kicad_pcb"
    result = generate_pcb(
        _scripted_pipeline_model(),
        "a 3.3V motor driver board",
        datasheets={"AMS1117-3.3": "https://x/ams1117.pdf"},
        output=out,
        time_limit_s=15.0,
        emit_stages=False,
    )
    assert result.artifacts == [out]
    assert list(tmp_path.iterdir()) == [out]


# ---------------------------------------------------------------- packages


def test_propose_prompt_names_the_packages_the_builder_supports():
    """The model is told the footprint rule up front, from the rule's own text."""
    from silkscreen.board import supported_packages_text

    model = ScriptedModel(responses=[json.dumps(GOOD_CIRCUIT)])
    propose_circuit(model, "a toy car")
    prompt = model.calls[0]["prompt"]
    assert supported_packages_text() in prompt
    assert "{packages}" not in prompt


def test_propose_repairs_an_unsupported_package():
    """A 7-pin device is the board stage's refusal; it must reach the model
    as a repair item while the loop can still do something about it."""
    seven = json.loads(json.dumps(GOOD_CIRCUIT))
    seven["devices"]["DRV8837"]["pins"] = {
        "IN1": "1", "IN2": "2", "VM": "3", "GND": "4",
        "OUT1": "5", "OUT2": "6", "VCC": "7",
    }
    seven["nets"]["SLEEP"] = ["DRV8837.IN2", "r_sleep.2"]
    model = ScriptedModel(responses=[json.dumps(seven), json.dumps(GOOD_CIRCUIT)])

    spec, attempts = propose_circuit(model, "a toy car")
    assert len(attempts) == 2
    assert not attempts[0].accepted and attempts[1].accepted
    assert any("7 pins" in e for e in attempts[0].errors)
    assert "No package rule" in model.calls[1]["prompt"]
    assert all(len(d.pins) != 7 for d in spec.devices)


# ---------------------------------------------------------------- enclosure (v2)
#
# The kernel path of ``enclosure_stage`` (docs/ai-cad-plan.md v2). The kernel
# itself (build123d) is A's and B's; here it is stood in for by a fake
# ``EnclosureProposal`` carrying a fake model and a tiny real ``KernelReport``,
# with ``stages.export_model`` / ``stages.render_packet`` monkeypatched at the
# module attributes the stage looks them up through.

from pathlib import Path  # noqa: E402

from silkscreen.agents import stages  # noqa: E402
from silkscreen.agents.enclosure import (  # noqa: E402
    ENCLOSURE_PROMPT,
    EnclosureProposal,
)
from silkscreen.enclosure.cad import ExportPaths  # noqa: E402
from silkscreen.enclosure.ir import parse_enclosure_spec  # noqa: E402
from silkscreen.enclosure.kernel import Clause, KernelReport  # noqa: E402

ENCLOSURE_JSON = {"wall_mm": 2.0, "lid": "lip", "cutouts": []}
ENCLOSURE_INTENT = "a 3.3V motor driver board"
ENCLOSURE_SHEETS = {"AMS1117-3.3": "https://x/ams1117.pdf"}

DONE_KEYS = {
    "event", "stage", "cutouts", "lid", "wall_mm", "repair_rounds",
    "kernel_passed", "kernel_failed", "exports",
}


#: What the fake exporter writes as the STEP assembly. ISO 10303-21 is ASCII,
#: which is what lets the one-shot route ship the whole case in JSON.
STEP_TEXT = "ISO-10303-21;\nHEADER;\nENDSEC;\nEND-ISO-10303-21;\n"


def _kernel_report(*, failing: tuple[str, ...] = ()) -> KernelReport:
    return KernelReport(
        clauses=(
            Clause("board_clash", "board_clash" not in failing, 1_000_000,
                   "cavity clears the board by 1.000 mm"),
            Clause("headroom", "headroom" not in failing, -250_000,
                   "lid underside 0.250 mm below the tallest part"),
        ),
        warnings=("snapshot critic: nothing to report",),
    )


class _FakeModel:
    """Stands in for an EnclosureModel: the stage never looks inside it."""


def _install_kernel_fakes(monkeypatch, *, kernel=None, brief="the brief",
                          render=None, ask_model=False):
    """Make propose_enclosure answer with a kernel-built proposal and give the
    stage fake exporters. Returns the call log."""
    calls: dict[str, list] = {"export": [], "render": []}
    fake_model = _FakeModel()

    def fake_propose(agent_model, envelope, *, style_hint="", rigorous=False,
                     on_event=None, **kw):
        # ``ask_model``: the fake stands in for OCCT, not for the model. The
        # real ``propose_enclosure`` asks the model once before it builds
        # anything, and that round trip is what carries the case's stage
        # attribution onto the worker thread it runs on. A test pinning that
        # attribution needs the call to happen; the rest do not, and their
        # ScriptedModels carry no answer for this prompt.
        if ask_model:
            agent_model.generate(ENCLOSURE_PROMPT)
        spec = parse_enclosure_spec(json.dumps(ENCLOSURE_JSON))
        return EnclosureProposal(
            spec=spec,
            repair_rounds=1,
            model=fake_model,
            kernel=_kernel_report() if kernel is None else kernel,
            brief=brief,
        )

    def fake_export(model, directory, stem="enclosure"):
        assert model is fake_model
        directory = Path(directory)
        calls["export"].append((directory, stem))
        paths = ExportPaths(
            step=directory / f"{stem}.step",
            base_stl=directory / f"{stem}-base.stl",
            lid_stl=directory / f"{stem}-lid.stl",
        )
        paths.step.write_text(STEP_TEXT)
        paths.base_stl.write_text("solid base\nendsolid base\n")
        paths.lid_stl.write_text("solid lid\nendsolid lid\n")
        return paths

    def fake_render(model, out_dir, **kw):
        assert model is fake_model
        calls["render"].append(Path(out_dir))
        out = []
        for view in ("iso", "top"):
            png = Path(out_dir) / f"enclosure-{view}.png"
            png.write_bytes(b"\x89PNG\r\n")
            out.append(png)
        return tuple(out)

    monkeypatch.setattr(stages, "propose_enclosure", fake_propose)
    monkeypatch.setattr(stages, "export_model", fake_export)
    monkeypatch.setattr(stages, "render_packet", render or fake_render)
    return calls


def _enclosure_run(model, *, on_event=None, **kw):
    return generate_pcb(
        model,
        ENCLOSURE_INTENT,
        datasheets=ENCLOSURE_SHEETS,
        time_limit_s=15.0,
        on_event=on_event,
        enclosure=True,
        enclosure_style="rounded corners",
        **kw,
    )


def _enclosure_done(events):
    return next(
        e for e in events
        if e["event"] == "stage.done" and e.get("stage") == "enclosure"
    )


def test_kernel_path_stage_done_carries_engine_and_kernel_fields(
    tmp_path, offline_pdf_fetch, monkeypatch
):
    calls = _install_kernel_fakes(monkeypatch)
    events = []
    result = _enclosure_run(
        _scripted_pipeline_model(),
        output=tmp_path / "board.kicad_pcb",
        on_event=events.append,
    )

    done = _enclosure_done(events)
    assert {k: v for k, v in done.items() if k != "t_s"} == {
        "event": "stage.done",
        "stage": "enclosure",
        "cutouts": 0,
        "lid": "lip",
        "wall_mm": 2.0,
        "repair_rounds": 1,
        "kernel_passed": True,
        "kernel_failed": [],
        "exports": [
            "enclosure.step", "enclosure-base.stl", "enclosure-lid.stl",
        ],
    }
    # The default stem is "enclosure", beside the board (decision 19).
    assert calls["export"] == [(tmp_path, "enclosure")]
    assert calls["render"] == [tmp_path]

    case = result.enclosure
    assert case.kernel.passed is True
    assert case.brief == "the brief"
    assert case.exports.step == tmp_path / "enclosure.step"
    # The STEP text is what the exporter wrote, verbatim.
    assert case.step_text == case.exports.step.read_text(encoding="utf-8")
    assert case.step_text == STEP_TEXT
    assert case.snapshots == (
        tmp_path / "enclosure-iso.png", tmp_path / "enclosure-top.png"
    )
    # Every written file is an artifact, STEP first, before the headline board.
    for path in (case.exports.step, case.exports.base_stl, case.exports.lid_stl,
                 *case.snapshots):
        assert path in result.artifacts
        assert result.artifacts.index(path) < result.artifacts.index(
            result.board_path
        )
    assert result.artifacts.index(case.exports.step) < result.artifacts.index(
        case.exports.base_stl
    )
    # The brief never rides the event stream.
    for event in events:
        assert "the brief" not in json.dumps(event)


def test_kernel_failed_clauses_are_named_on_the_event(
    tmp_path, offline_pdf_fetch, monkeypatch
):
    _install_kernel_fakes(monkeypatch, kernel=_kernel_report(failing=("headroom",)))
    events = []
    result = _enclosure_run(
        _scripted_pipeline_model(),
        output=tmp_path / "board.kicad_pcb",
        on_event=events.append,
    )
    done = _enclosure_done(events)
    assert done["kernel_passed"] is False
    assert done["kernel_failed"] == ["headroom"]
    # A failing kernel report is a receipt, not a failed stage: the files
    # and the result still ship, the report says what is wrong.
    assert result.enclosure is not None
    assert result.enclosure.kernel.failed == ["headroom"]


def test_without_a_directory_the_step_text_still_ships(
    offline_pdf_fetch, monkeypatch, tmp_path
):
    """The one-shot /generate path: no output, so nothing durable is written
    -- but the response must still carry a complete case, so the stage
    exports into a scratch directory and reads the STEP back."""
    calls = _install_kernel_fakes(monkeypatch)
    events = []
    result = _enclosure_run(_scripted_pipeline_model(), on_event=events.append)

    case = result.enclosure
    assert case.kernel is not None
    assert case.exports is None
    assert case.snapshots == ()
    assert case.step_text == STEP_TEXT
    # The export ran, into a scratch directory that is not the caller's, and
    # nothing was rendered.
    assert len(calls["export"]) == 1
    scratch, stem = calls["export"][0]
    assert stem == "enclosure"
    assert not scratch.exists()  # the TemporaryDirectory is gone by now
    assert calls["render"] == []
    done = _enclosure_done(events)
    assert done["exports"] == []
    assert list(tmp_path.iterdir()) == []


def test_board_only_never_exports_the_kernel_files(
    tmp_path, offline_pdf_fetch, monkeypatch
):
    """``emit_stages`` off promises only the routed board (the schematic rule).

    What ``--board-only`` declines is the *files*. By the time this runs the
    model call and the kernel build have already been spent, so the case
    itself still rides the result -- discarding it would throw away work
    already paid for rather than save any. The export therefore happens, into
    a scratch directory that is not the caller's, exactly as the one-shot
    ``/generate`` route does it
    (``test_without_a_directory_the_step_text_still_ships``).
    Nothing is rendered, because a snapshot has nowhere to go.
    """
    calls = _install_kernel_fakes(monkeypatch)
    result = _enclosure_run(
        _scripted_pipeline_model(),
        output=tmp_path / "board.kicad_pcb",
        emit_stages=False,
    )
    assert calls["render"] == []
    assert len(calls["export"]) == 1
    scratch, _stem = calls["export"][0]
    assert tmp_path not in scratch.parents and scratch != tmp_path
    assert result.enclosure.exports is None
    assert result.enclosure.step_text == STEP_TEXT
    assert sorted(p.name for p in tmp_path.iterdir()) == ["board.kicad_pcb"]


def test_snapshot_failure_is_a_warning_never_a_failed_stage(
    tmp_path, offline_pdf_fetch, monkeypatch
):
    """Decision 17: snapshots are advisory. A renderer that blows up -- the
    NotImplementedError of a stub included -- costs the run a warning event
    and nothing else."""

    def broken(model, out_dir, **kw):
        raise NotImplementedError("no renderer here")

    _install_kernel_fakes(monkeypatch, render=broken)
    events = []
    result = _enclosure_run(
        _scripted_pipeline_model(),
        output=tmp_path / "board.kicad_pcb",
        on_event=events.append,
    )
    warnings = [e for e in events if e["event"] == "enclosure.warning"]
    assert len(warnings) == 1
    assert "snapshots not rendered" in warnings[0]["warning"]
    assert "no renderer here" in warnings[0]["warning"]
    assert not any(e["event"] == "enclosure.failed" for e in events)
    assert set(_enclosure_done(events)) - {"t_s"} == DONE_KEYS
    assert result.enclosure.snapshots == ()
    assert result.enclosure.exports is not None


def test_export_failure_degrades_to_enclosure_failed(
    tmp_path, offline_pdf_fetch, monkeypatch
):
    """An OSError writing the exports is the honest degradation the stage has
    always had: enclosure.failed, None, board still delivered."""
    _install_kernel_fakes(monkeypatch)

    def cannot_write(model, directory, stem="enclosure"):
        raise OSError("disk full")

    monkeypatch.setattr(stages, "export_model", cannot_write)
    events = []
    result = _enclosure_run(
        _scripted_pipeline_model(),
        output=tmp_path / "board.kicad_pcb",
        on_event=events.append,
    )
    failed = [e for e in events if e["event"] == "enclosure.failed"]
    assert len(failed) == 1 and "disk full" in failed[0]["error"]
    assert result.enclosure is None
    assert (tmp_path / "board.kicad_pcb").exists()


def test_no_kernel_refuses_in_words_and_still_delivers_the_board(
    tmp_path, offline_pdf_fetch, monkeypatch
):
    """The one unacceptable outcome is a silent degrade to a worse case
    (docs/ai-cad-plan.md v3, 2026-09-08). With ``kernel_available()`` False
    the real proposal loop refuses: an ``enclosure.failed`` event naming the
    ``cad`` extra, ``result.enclosure`` None, and the board still written.

    The message is truncated to 160 characters on the wire, so this asserts
    the install hint actually survives that truncation rather than only that
    a failure happened.
    """
    import silkscreen.agents.enclosure as agent_enclosure

    monkeypatch.setattr(agent_enclosure, "kernel_available", lambda: False)

    def never(*a, **kw):  # pragma: no cover - only fires on regression
        raise AssertionError("a refused case reached a kernel exporter")

    monkeypatch.setattr(stages, "export_model", never)
    monkeypatch.setattr(stages, "render_packet", never)
    model = _scripted_pipeline_model()
    model.by_marker["ENCLOSURE-SPEC"] = json.dumps(ENCLOSURE_JSON)
    events = []
    result = _enclosure_run(
        model, output=tmp_path / "board.kicad_pcb", on_event=events.append
    )

    failed = [e for e in events if e["event"] == "enclosure.failed"]
    assert len(failed) == 1
    message = failed[0]["error"]
    assert len(message) <= 160
    assert "build123d" in message
    assert 'pip install -e ".[cad]"' in message
    # No stage.done, no case, and nothing invented in its place.
    assert not any(
        e["event"] == "stage.done" and e.get("stage") == "enclosure"
        for e in events
    )
    assert result.enclosure is None
    # The board is still the product, and no case file was written.
    assert result.board_path.exists()
    assert not list(tmp_path.glob("enclosure*"))


def test_proposal_fields_reads_the_proposal_by_name():
    spec = object()
    proposal = EnclosureProposal(spec=spec, repair_rounds=0, brief="b")
    assert stages.proposal_fields(proposal) == (spec, 0, None, None, "b")


# ---------------------------------------------------------------- sourcing


#: The refs GOOD_CIRCUIT resolves to, in board order, and one answer per ref.
#: Two URLs so both probe verdicts appear, two nulls so ``unresolved`` is
#: non-zero: the counts on the receipt are only meaningful if every status
#: the vocabulary allows is exercised by the same run.
SOURCING_PDF_URL = "https://vendor.example/ds/ams1117.pdf"
SOURCING_HTML_URL = "https://vendor.example/ds/drv8837"
SOURCING_ANSWER = {
    "parts": [
        {"ref": "U1", "manufacturer": "Advanced Monolithic Systems",
         "mpn": "AMS1117-3.3", "datasheet_url": SOURCING_PDF_URL, "note": None},
        {"ref": "U2", "manufacturer": "Texas Instruments", "mpn": "DRV8837DSGR",
         "datasheet_url": SOURCING_HTML_URL,
         "note": "WSON, not SOIC -- check the package"},
        {"ref": "C1", "manufacturer": "Samsung", "mpn": "CL31A226KAHNNNE",
         "datasheet_url": None, "note": None},
        {"ref": "C2", "manufacturer": "Samsung", "mpn": "CL31A226KAHNNNE",
         "datasheet_url": None, "note": None},
        {"ref": "C3", "manufacturer": None, "mpn": None,
         "datasheet_url": None, "note": None},
        {"ref": "R1", "manufacturer": None, "mpn": None,
         "datasheet_url": None, "note": None},
    ]
}


def _sourcing_probe(url: str) -> str:
    """The offline datasheet probe: one URL is a PDF, everything else is not."""
    return "verified" if url == SOURCING_PDF_URL else "not_pdf"


def _sourcing_pipeline_model(answer=None):
    """The full-pipeline model with a sourcing answer scripted on its marker."""
    from silkscreen.agents.sourcing import SOURCING_MARKER

    model = _scripted_pipeline_model()
    model.by_marker[SOURCING_MARKER] = (
        json.dumps(SOURCING_ANSWER) if answer is None else answer
    )
    return model


def _sourcing_lanes(events):
    """Split a stream into the main thread's events and the sourcing thread's.

    The parts are sourced on a worker thread from the moment placement lands,
    so its events interleave with schematic and route in wall-clock order.
    Within each lane the order is frozen; across lanes only the bounds are
    (started after place, and every sourcing event closed before the stream
    ends -- nothing escapes the join).

    ``main`` is the *driver* thread's line, so the critic's frames come out of
    it too: review is likewise a worker lane now, running from the spec inside
    place's solve, and leaving it in ``main`` would make this helper assert a
    wall-clock race.
    """
    lane = [
        e for e in events
        if e.get("stage") == "sourcing" or e["event"].startswith("sourcing")
    ]
    main = [
        e for e in events
        if e not in lane and e.get("stage") != "review"
    ]
    return main, lane


#: The order the worker stages settle in, and the whole reason any of this
#: ordering is pinned. Three stages run on threads (enclosure, sourcing,
#: simulation) and a fourth -- review -- now runs on one too, from the moment
#: the spec validates. The offline suite stays deterministic only because
#: **exactly one worker model call is ever in flight**, so a ``ScriptedModel``
#: is asked for the stages in one fixed sequence and answers a run the same
#: way twice. This list *is* that sequence.
#:
#: read and plan are the driver's own calls. propose follows, and may repeat
#: (a repair round is another call at the same stage). review comes next: the
#: critic needs only the spec, so it is started before place and joined before
#: the background lanes are started -- which is what keeps it from racing them
#: for the model.
#:
#: The three background lanes then share ONE rank, and that is a statement of
#: fact rather than a convenience. enclosure, sourcing and simulation are
#: started together on three threads and their model calls genuinely race;
#: only the *joins* are ordered (enclosure, then sourcing, then simulation),
#: which is what fixes the order results are assembled in, not the order the
#: model is asked. Measured 2026-09-07: one run of
#: ``test_sourcing_runs_beside_the_enclosure`` completed
#: ``[read, propose, review, sourcing, enclosure]`` -- sourcing's call landing
#: first. What keeps that deterministic offline is ``ScriptedModel.by_marker``
#: keying on each lane's own marker, NOT the call order, so pinning an order
#: between those three would be asserting something the code does not promise
#: and would flake under load.
#:
#: Everything up to and including review *is* strictly sequential, and that is
#: the part worth pinning: one worker call in flight at a time, in one order.
WORKER_CALL_ORDER = (
    "read", "plan", "propose", "review",
    # One shared rank -- concurrent by design, see above.
    "enclosure", "sourcing", "simulation",
)

#: The rank of each stage in :data:`WORKER_CALL_ORDER`, with the three
#: concurrent lanes collapsed onto one.
_CONCURRENT_LANES = frozenset({"enclosure", "sourcing", "simulation"})


def assert_worker_call_order(events):
    """Every worker model call came out in :data:`WORKER_CALL_ORDER`.

    This is the determinism contract stated directly, rather than as an index
    comparison whose meaning a reader has to reconstruct. It holds however
    many stages a particular run switched on and however many repair rounds
    propose needed, because it asserts the sequence never goes *backwards*
    through the order rather than asserting one exact list.

    It fails loudly in the two ways that matter. Move a stage's call across
    another lane's -- review back behind the background lanes, say -- and the
    sequence goes backwards here. Put a second worker call in flight beside
    one of the sequential stages and the order they complete in stops being
    stable, so this stops holding run to run, which is precisely the
    guarantee that would have been lost.

    It deliberately does *not* order enclosure, sourcing and simulation
    against each other: those three run concurrently and are kept
    deterministic by marker keying instead. See :data:`WORKER_CALL_ORDER`.
    """
    rank = {
        stage: (len(WORKER_CALL_ORDER) if stage in _CONCURRENT_LANES else i)
        for i, stage in enumerate(WORKER_CALL_ORDER)
    }
    calls = [
        e["stage"] for e in events
        if e["event"] == "model.call" and e.get("stage") in rank
    ]
    ranks = [rank[stage] for stage in calls]
    assert ranks == sorted(ranks), (
        f"worker model calls came out as {calls}. The sequential stages must "
        f"come out in the order "
        f"{[s for s in WORKER_CALL_ORDER if s not in _CONCURRENT_LANES]} and "
        f"every one of {sorted(_CONCURRENT_LANES)} must come after them. "
        f"Either a second worker call was in flight beside a sequential "
        f"stage, or a stage moved across a lane boundary."
    )


def _assert_sourcing_bounds(events):
    def at(event, stage):
        return next(
            i for i, e in enumerate(events)
            if e["event"] == event and e.get("stage") == stage
        )

    assert at("stage.start", "sourcing") > at("stage.done", "place")
    end = [
        i for i, e in enumerate(events)
        if (e["event"] == "stage.done" and e.get("stage") == "sourcing")
        or e["event"] == "sourcing.failed"
    ]
    # The lane closes -- a sourcing thread that emitted no terminal event is
    # one nobody joined. The upper bound used to be "before review starts";
    # the critic now starts at the validated spec and is joined *before*
    # sourcing is even started, so review is upstream of this lane, not
    # downstream, and cannot bound it. The guarantee that bound was there to
    # protect is unchanged and is asserted directly instead: the critic has
    # closed before the lane opens, so the two never race for the model, and
    # every worker call across the stream came out in the frozen order.
    assert end
    assert at("stage.done", "review") < at("stage.start", "sourcing")
    assert_worker_call_order(events)


def test_default_run_has_no_sourcing_and_no_sourcing_events(
    tmp_path, offline_pdf_fetch
):
    """Opt-in, the enclosure's rule: off means silent, not an empty pass."""
    events = []
    result = generate_pcb(
        _sourcing_pipeline_model(),
        "a 3.3V motor driver board",
        datasheets={"AMS1117-3.3": "https://x/ams1117.pdf"},
        output=tmp_path / "board.kicad_pcb",
        time_limit_s=15.0,
        on_event=events.append,
    )
    assert result.sourcing is None
    assert result.bom_path is None
    assert not any(e.get("stage") == "sourcing" for e in events)
    assert not any(e["event"].startswith("sourcing") for e in events)
    assert not (tmp_path / "bom.csv").exists()


def test_sourcing_run_emits_the_frozen_events_and_writes_the_bom(
    tmp_path, offline_pdf_fetch
):
    from silkscreen.sourcing import bom_csv, grouped_bom_csv

    events = []
    result = generate_pcb(
        _sourcing_pipeline_model(),
        "a 3.3V motor driver board",
        datasheets={"AMS1117-3.3": "https://x/ams1117.pdf"},
        output=tmp_path / "board.kicad_pcb",
        time_limit_s=15.0,
        on_event=events.append,
        sourcing=True,
        sourcing_probe=_sourcing_probe,
    )

    main, lane = _sourcing_lanes(events)
    # The frozen ordering, per lane: the main thread's stages are the ones
    # every run has, and sourcing runs beside schematic and route.
    assert [e["stage"] for e in main if e["event"].startswith("stage.")] == [
        "read", "read", "propose", "propose", "place", "place",
        "schematic", "schematic", "route", "route",
    ]
    assert [e["event"] for e in lane] == [
        "stage.start", "model.call",
        *["sourcing.part"] * 6,
        "stage.done",
    ]
    # Started after placement; the critic is joined before it starts.
    _assert_sourcing_bounds(events)
    # The thread's model call is attributed to its own stage, whatever the
    # main thread had entered by the time the call was made.
    assert [e["stage"] for e in lane if e["event"] == "model.call"] == ["sourcing"]
    assert [e["stage"] for e in main if e["event"] == "model.call"] == [
        "read", "propose",
    ]
    # Per-part events carry the two statuses and nothing the model said.
    assert [(e["ref"], e["mpn_status"], e["datasheet_status"])
            for e in lane if e["event"] == "sourcing.part"] == [
        ("U1", "proposed", "verified"),
        ("U2", "proposed", "not_pdf"),
        ("C1", "proposed", "none"),
        ("C2", "proposed", "none"),
        ("C3", "none", "none"),
        ("R1", "none", "none"),
    ]
    done = lane[-1]
    assert done["parts"] == 6
    assert done["verified"] == 1
    assert done["proposed"] == 4
    assert done["unresolved"] == 2

    assert result.sourcing is not None
    assert (result.sourcing.verified, result.sourcing.proposed,
            result.sourcing.unresolved) == (1, 4, 2)
    assert result.sourcing.warnings == []
    assert [p.ref for p in result.sourcing.parts] == [
        "U1", "U2", "C1", "C2", "C3", "R1",
    ]
    assert result.sourcing.parts[0].mpn == "AMS1117-3.3"
    assert result.sourcing.parts[0].datasheet_url == SOURCING_PDF_URL
    # The 3D model comes from the deterministic table, not from the model.
    assert result.sourcing.parts[0].model3d is not None
    assert result.sourcing.parts[0].model3d.endswith("SOT-223.step")

    bom_path = tmp_path / "bom.csv"
    assert result.bom_path == bom_path
    assert bom_path.read_text(encoding="utf-8") == bom_csv(result.sourcing)
    # The assembler's grouped sheet lands beside it, from the same rows.
    grouped_path = tmp_path / "bom-grouped.csv"
    assert result.grouped_bom_path == grouped_path
    assert grouped_path.read_text(encoding="utf-8") == grouped_bom_csv(result.sourcing)
    assert grouped_path in result.artifacts
    assert result.artifacts.index(bom_path) < result.artifacts.index(grouped_path)
    # The written BOM is a first-class artifact, reported like the rest, and
    # sits before the board the same way the case files do.
    assert bom_path in result.artifacts
    assert result.artifacts.index(bom_path) < result.artifacts.index(
        result.board_path
    )
    assert (tmp_path / "board.kicad_pcb").exists()


def test_sourcing_without_output_returns_the_bom_but_writes_nothing(
    offline_pdf_fetch
):
    result = generate_pcb(
        _sourcing_pipeline_model(),
        "a 3.3V motor driver board",
        datasheets={"AMS1117-3.3": "https://x/ams1117.pdf"},
        time_limit_s=15.0,
        sourcing=True,
        sourcing_probe=_sourcing_probe,
    )
    assert result.sourcing is not None
    assert result.sourcing.proposed == 4
    assert result.bom_path is None
    assert result.artifacts == []


def test_board_only_sources_the_parts_but_skips_the_bom_file(
    tmp_path, offline_pdf_fetch
):
    """``--board-only`` promises only the routed board (the enclosure.step rule)."""
    result = generate_pcb(
        _sourcing_pipeline_model(),
        "a 3.3V motor driver board",
        datasheets={"AMS1117-3.3": "https://x/ams1117.pdf"},
        output=tmp_path / "board.kicad_pcb",
        time_limit_s=15.0,
        emit_stages=False,
        sourcing=True,
        sourcing_probe=_sourcing_probe,
    )
    assert result.sourcing is not None
    assert result.bom_path is None
    assert result.grouped_bom_path is None
    assert not (tmp_path / "bom.csv").exists()
    assert not (tmp_path / "bom-grouped.csv").exists()
    assert [p.name for p in result.artifacts] == ["board.kicad_pcb"]


def test_sourcing_runs_beside_the_enclosure(tmp_path, offline_pdf_fetch, monkeypatch):
    """Two worker threads, one join order: the case first, then the BOM.

    Both lanes are frozen on their own, both are bounded by place and review,
    and neither steals the other's stage attribution -- the per-thread stage
    slot is what this run leans on hardest, with three threads calling one
    model. The kernel is the only case engine, so it is faked here (the
    exporters as well) rather than pinned absent -- absent now means no case
    at all.
    """
    from test_enclosure_agent import GOOD_ENCLOSURE

    _install_kernel_fakes(monkeypatch, ask_model=True)
    model = _sourcing_pipeline_model()
    model.by_marker["ENCLOSURE-SPEC v1"] = json.dumps(GOOD_ENCLOSURE)
    events = []
    result = generate_pcb(
        model,
        "a 3.3V motor driver board",
        datasheets={"AMS1117-3.3": "https://x/ams1117.pdf"},
        output=tmp_path / "board.kicad_pcb",
        time_limit_s=15.0,
        on_event=events.append,
        enclosure=True,
        sourcing=True,
        sourcing_probe=_sourcing_probe,
    )
    assert result.enclosure is not None
    assert result.sourcing is not None
    _assert_sourcing_bounds(events)
    case = [e for e in events if e.get("stage") == "enclosure"]
    assert [e["event"] for e in case] == ["stage.start", "model.call", "stage.done"]
    _, lane = _sourcing_lanes(events)
    assert [e["stage"] for e in lane if e["event"] == "model.call"] == ["sourcing"]
    assert [p.name for p in result.artifacts] == [
        "board.kicad_pro", "board.kicad_sch", "board.placed.kicad_pcb",
        "enclosure.step", "enclosure-base.stl", "enclosure-lid.stl",
        "enclosure-iso.png", "enclosure-top.png",
        "bom.csv", "bom-grouped.csv", "board.kicad_pcb",
    ]


def test_an_unusable_sourcing_answer_degrades_to_the_unsourced_rows(
    tmp_path, offline_pdf_fetch
):
    """The repair budget spent, the BOM is still a BOM: every row, no MPNs,
    one warning that says so. The run does not fail and the board ships."""
    result = generate_pcb(
        _sourcing_pipeline_model(answer="not json at all"),
        "a 3.3V motor driver board",
        datasheets={"AMS1117-3.3": "https://x/ams1117.pdf"},
        output=tmp_path / "board.kicad_pcb",
        time_limit_s=15.0,
        sourcing=True,
        sourcing_probe=_sourcing_probe,
    )
    assert result.sourcing is not None
    assert result.sourcing.unresolved == 6
    assert result.sourcing.proposed == 0
    assert len(result.sourcing.warnings) == 1
    assert "not sourced" in result.sourcing.warnings[0]
    # Honest on disk too: the rows are written, statuses "none" and all.
    assert result.bom_path is not None
    assert result.bom_path.read_text(encoding="utf-8").count("\n") == 7


def test_a_model_failure_on_the_sourcing_thread_is_raised_at_the_join(
    tmp_path, offline_pdf_fetch
):
    """An outage on the worker thread carries the same meaning at the join
    as it would in line: ModelError, out of ``generate_pcb`` and not a
    half-built result.

    The critic *has* already run by then -- it now starts at the spec and is
    joined before sourcing is even started -- so its answer is paid for and
    then discarded when the run raises. That is the accepted cost of taking
    review off the tail; what must not happen is the critic being left
    running on a thread nobody joins, so its lane is asserted closed.
    """
    from silkscreen.agents.sourcing import SOURCING_MARKER

    model = _sourcing_pipeline_model()
    # No scripted answer for the sourcing prompt: the model "ran out".
    del model.by_marker[SOURCING_MARKER]
    events = []
    with pytest.raises(ModelError):
        generate_pcb(
            model,
            "a 3.3V motor driver board",
            datasheets={"AMS1117-3.3": "https://x/ams1117.pdf"},
            output=tmp_path / "board.kicad_pcb",
            time_limit_s=15.0,
            on_event=events.append,
            sourcing=True,
            sourcing_probe=_sourcing_probe,
        )
    review = [e["event"] for e in events if e.get("stage") == "review"]
    assert review and review[-1] == "stage.done"
    assert not (tmp_path / "bom.csv").exists()


def test_an_unreadable_datasheet_is_reported_on_the_result_not_only_as_an_event(
    tmp_path,
):
    """A board designed without a part's facts must say so somewhere a
    non-streaming caller can see.

    A failed read was surfaced only as a ``read.failed`` event, and ``emit``
    is a no-op whenever ``on_event`` is None -- which is every non-streaming
    caller, the CLI included. So the part was dropped from ``facts``, the
    board was designed and reviewed from the model's general knowledge,
    ``review.ok`` was true, and nothing said a datasheet had been missed.
    ``generate_pcb``'s own docstring is explicit that this "designs and
    reviews the board as though the part were undocumented".
    """
    import silkscreen.agents.grounding as grounding
    from silkscreen.agents import generate_pcb

    def one_of_two(url, **kwargs):
        # Every read failing already raises, and should: a run that grounded
        # nothing it was asked to ground is a failed run. The silent case is
        # the *partial* one, where the board is still produced.
        if "drv8837" in url:
            raise RuntimeError("503 from the distributor")
        return b"%PDF-1.4 stub datasheet"

    original = grounding.fetch_pdf
    grounding.fetch_pdf = one_of_two
    try:
        model = ScriptedModel(
            responses=[
                json.dumps(DATASHEET_JSON),
                GOOD_CIRCUIT,
                json.dumps({"findings": []}),
            ]
        )
        result = generate_pcb(
            model,
            "a 3.3V regulator",
            datasheets={
                "AMS1117": "https://example.test/ams1117.pdf",
                "DRV8837": "https://example.test/drv8837.pdf",
            },
            route=False,
        )
    finally:
        grounding.fetch_pdf = original
    assert result.unread_datasheets, "the failed read left no trace on the result"
    assert "DRV8837" in result.unread_datasheets[0]
    assert "without its facts" in result.unread_datasheets[0]


# --- a cut-off answer is not an answer ------------------------------------
#
# ``resp.text`` carries whatever the model produced before generation
# stopped, and nothing about the string says it is a fragment. Every caller
# in this package parses it as JSON, so a truncated answer used to surface as
# "the critic's answer was not JSON" -- a message that sends a person to the
# prompt when the fix is the token budget. Measured 2026-09-07 against
# gemini-3.5-flash-lite: a 200-token budget returned 524 characters of a
# half-written JSON array with finish_reason MAX_TOKENS.


class _FakeCandidate:
    def __init__(self, finish_reason):
        self.finish_reason = finish_reason


class _FakeResponse:
    def __init__(self, text, finish_reason):
        self.text = text
        self.candidates = [_FakeCandidate(finish_reason)]


def _gemini_offline(monkeypatch):
    """A GeminiModel built without a network call, for response handling."""
    from silkscreen.agents.model import GeminiModel

    monkeypatch.setenv("GOOGLE_API_KEY", "test-key-not-used")
    return GeminiModel("gemini-3.5-flash-lite")


class _FinishReasonEnum:
    """Stands in for the SDK's FinishReason enum, which has a ``.name``."""

    def __init__(self, name):
        self.name = name


@pytest.mark.parametrize(
    "reason",
    [_FinishReasonEnum("MAX_TOKENS"), "MAX_TOKENS", "FinishReason.MAX_TOKENS"],
)
def test_a_truncated_answer_raises_instead_of_being_parsed(monkeypatch, reason):
    """Whatever shape the SDK reports it in, MAX_TOKENS is not an answer."""
    from silkscreen.agents.model import ModelError

    model = _gemini_offline(monkeypatch)
    with pytest.raises(ModelError) as exc:
        model._reject_truncation(_FakeResponse('{"findings": [', reason), 8192, 14)
    # The message has to name the budget, or it sends a reader to the prompt.
    assert "8192" in str(exc.value)
    assert "truncated" in str(exc.value)
    assert "max_output_tokens" in str(exc.value)


def test_a_complete_answer_is_returned_whatever_the_finish_reason_shape(
    monkeypatch,
):
    """Only MAX_TOKENS is truncation; STOP and an absent reason are answers."""
    model = _gemini_offline(monkeypatch)
    for reason in (_FinishReasonEnum("STOP"), "STOP", None):
        model._reject_truncation(_FakeResponse('{"findings": []}', reason), 8192, 16)


def test_a_response_with_no_candidates_is_not_treated_as_truncated(monkeypatch):
    """A shape this code does not recognise must not invent a failure.

    The empty-text guard above already rejects the case that matters; a
    response whose candidate list this SDK version does not populate is not
    evidence of truncation, and raising on it would take down every call.
    """
    model = _gemini_offline(monkeypatch)

    class _NoCandidates:
        text = "fine"
        candidates = []

    model._reject_truncation(_NoCandidates(), 8192, 4)
