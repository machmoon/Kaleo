"""The model half of the review: the findings a rule cannot express.

Everything here produces :data:`Origin.SUGGESTED` findings, and it is
structurally unable to produce anything else -- the constructor is fixed at the
bottom of :func:`_parse_findings`. The model is never shown a chance to edit,
downgrade or delete a proven finding; it receives them as context so it does
not waste its pass re-deriving geometry it cannot measure.

The layering rule of this repo is that the engine makes no network calls, and
that holds: nothing here imports the agents package or an SDK. It takes
anything with a ``generate`` method, which is the same seam
:class:`silkscreen.agents.model.ScriptedModel` fills in the tests.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any, Protocol

from ..units import NM_PER_MM
from .effort import EffortProfile
from .findings import Finding, Origin, Severity
from .geometry import AuditBoard, Rect
from .rules import is_ground, is_supply

__all__ = ["JudgeModel", "board_digest", "judge", "JUDGE_PROMPT", "REFUTE_PROMPT"]

#: How many parts a per-part pass will look at. Beyond this the passes stop
#: paying for themselves, and the report says how many were skipped rather
#: than implying the whole board was covered.
MAX_FOCUS_PARTS = 6


class JudgeModel(Protocol):
    def generate(self, prompt: str, **kwargs: Any) -> str: ...


def _strip_fence(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _loads(raw: str) -> dict:
    try:
        data = json.loads(_strip_fence(raw))
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.S)
        if not match:
            return {}
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return {}
    return data if isinstance(data, dict) else {}


def board_digest(board: AuditBoard, proven: list[Finding]) -> str:
    """Everything the model gets to see: geometry rendered as text.

    A model cannot measure a board, so it is given measurements. Positions are
    in millimetres in KiCad's own frame, which is the frame the report and the
    render use, so a coordinate quoted back is a coordinate that can be found.
    """

    def mm(value: int) -> str:
        return f"{value / NM_PER_MM:.2f}"

    lines: list[str] = []
    if board.outline is not None:
        o = board.outline
        lines.append(
            f"Board outline: {mm(o.width_nm)} x {mm(o.height_nm)} mm, "
            f"origin {mm(o.x0)},{mm(o.y0)}"
        )
    else:
        lines.append("Board outline: none drawn")

    lines.append(f"Parts ({len(board.parts)}):")
    for part in board.parts:
        e = part.extent
        lines.append(
            f"  {part.ref} {part.value or '-'} [{part.lib_id or 'unknown'}] "
            f"{len(part.pads)} pads, side {part.side}, at {mm(e.centre[0])},"
            f"{mm(e.centre[1])}, {mm(e.width_nm)}x{mm(e.height_nm)} mm"
        )
        pins = [f"{p.number}:{p.net}" for p in part.pads if p.net]
        if pins:
            lines.append("     pins " + ", ".join(pins[:24]))

    by_net = board.pads_by_net()
    lines.append(f"Nets ({len(by_net)}):")
    for net, pads in sorted(by_net.items()):
        kind = "supply" if is_supply(net) else "ground" if is_ground(net) else "signal"
        lines.append(
            f"  {net} [{kind}] {len(pads)} pads: "
            + ", ".join(p.name for p in pads[:10])
        )

    lines.append(
        f"Copper: {len(board.tracks)} track segments, {len(board.vias)} vias"
    )

    if proven:
        lines.append("Already PROVEN by the deterministic checker (do not repeat):")
        for finding in proven:
            lines.append(f"  - [{finding.severity.value}] {finding.title}")
    else:
        lines.append("The deterministic checker found nothing.")
    return "\n".join(lines)


JUDGE_PROMPT = """\
You are reviewing a PCB someone else laid out. A deterministic checker has
already measured the geometry: overlaps, clearances, connectivity, decoupling
distance, track widths. Do not repeat what it found and do not attempt to
re-measure anything -- you cannot, and a wrong measurement is worse than no
finding at all.

Your job is the half a checker cannot express. Look for:
- A component whose VALUE is wrong for its role: regulator input/output caps,
  crystal load caps, pull-up strength, a bulk cap where a ceramic belongs.
- A pin tied to the wrong kind of net: an output on a supply, a mode/boot pin
  left floating, an enable pin with no defined level.
- A missing part: no reset pull-up, no bulk capacitance, no series resistor
  where one is required.
- A topology error: feedback taken from the wrong node, two supplies joined,
  a part with both terminals on one net.
- A layout judgment the checker cannot make: a noisy net routed under a
  crystal, a switching node with a large loop, a connector facing inward.

Return ONE JSON object, no prose, no code fence:

{
  "findings": [
    {"severity": "blocker|marginal|note",
     "title": "<one line naming the defect and the part>",
     "detail": "<2-3 sentences: what is wrong and what physically happens>",
     "refs": ["<reference designators involved>"],
     "nets": ["<net names involved>"],
     "fix": "<one concrete change>"}
  ]
}

Rules:
- Name at least one real reference designator or net in every finding. A
  finding that points at nothing cannot be shown on the board and will be
  discarded.
- "blocker" means the board will not work. Use it only when you are certain.
- If you genuinely find nothing, return {"findings": []}. Do not pad the list.
"""

FOCUS_PROMPT = """\
Now look only at {ref} ({value}, {pads} pads) and the nets it touches. Ignore
the rest of the board. One part examined properly finds what a whole-board
pass skims over: check every pin of {ref} against what a part like this
requires, and check the parts immediately around it.

Same JSON shape as before. Findings about anything other than {ref} and its
nets will be discarded.
"""

REFUTE_PROMPT = """\
A reviewer made this claim about the board. Your job is to REFUTE it.

Claim: {title}
Reasoning given: {detail}
Parts named: {refs}
Nets named: {nets}

The board, as measured:
{digest}

Decide whether the claim survives. Refute it if the evidence does not support
it, if it names a part or net that does not exist, if it contradicts the
measured geometry, or if it is a generic remark that would be true of any
board. Default to refuting when you are unsure -- a review that reports a
non-problem costs more trust than one that misses a small one.

Return ONE JSON object, no prose:
{{"refuted": true|false, "reason": "<one sentence>"}}
"""


def _parse_findings(
    raw: str, board: AuditBoard, source: str, *, limit_refs: set[str] | None = None
) -> list[Finding]:
    data = _loads(raw)
    entries = data.get("findings")
    if not isinstance(entries, list):
        return []

    known_refs = {p.ref for p in board.parts}
    known_nets = set(board.pads_by_net())
    out: list[Finding] = []
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("title"):
            continue
        # Drop names the board does not contain, the same way the circuit
        # reviewer does: a finding pointing at an invented part is noise that
        # reads exactly like signal.
        refs = tuple(
            r
            for r in (entry.get("refs") or [])
            if isinstance(r, str) and r in known_refs
        )
        nets = tuple(
            n
            for n in (entry.get("nets") or [])
            if isinstance(n, str) and n in known_nets
        )
        if limit_refs is not None and not (set(refs) & limit_refs):
            continue
        # A suggested finding that names nothing the board contains cannot be
        # located, cannot be checked, and reads exactly like one that can. The
        # prompt says such findings are discarded; this is where that happens.
        if not refs and not nets:
            continue
        try:
            severity = Severity(str(entry.get("severity", "note")).lower())
        except ValueError:
            severity = Severity.NOTE
        out.append(
            Finding(
                rule="judgment",
                severity=severity,
                # Fixed here, not taken from the model: nothing a model says
                # is allowed to enter the report as proven.
                origin=Origin.SUGGESTED,
                title=str(entry["title"]),
                detail=str(entry.get("detail", "")),
                refs=refs,
                nets=nets,
                extent=_locate(board, refs, nets),
                evidence="",
                fix=str(entry.get("fix", "")),
                source=source,
            )
        )
    return out


def _locate(
    board: AuditBoard, refs: tuple[str, ...], nets: tuple[str, ...]
) -> Rect | None:
    """Put a model's finding on the board, or leave it honestly unplaced."""
    boxes: list[Rect] = []
    for ref in refs:
        part = board.part_by_ref(ref)
        if part is not None:
            boxes.append(part.extent)
    if not boxes:
        by_net = board.pads_by_net()
        for net in nets:
            for pad in by_net.get(net, []):
                boxes.append(pad.rect)
    if not boxes:
        return None
    out = boxes[0]
    for box in boxes[1:]:
        out = out.union(box)
    return out


def judge(
    model: JudgeModel,
    board: AuditBoard,
    proven: list[Finding],
    profile: EffortProfile,
    *,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[list[Finding], list[str]]:
    """Run the model passes the profile calls for.

    Returns the surviving suggested findings and a list of pass descriptions,
    so the report can say exactly what thinking was done rather than only what
    level was asked for.
    """

    def emit(**evt: Any) -> None:
        if on_event is not None:
            on_event(evt)

    passes: list[str] = []
    digest = board_digest(board, proven)
    suggested: list[Finding] = []

    for round_index in range(profile.judgment_passes):
        label = f"whole-board pass {round_index + 1}"
        emit(event="judge.start", pass_=label)
        prompt = f"{JUDGE_PROMPT}\n\nThe board under review:\n{digest}\n"
        if round_index:
            already = "\n".join(f"- {f.title}" for f in suggested) or "(nothing yet)"
            prompt += (
                "\nA previous pass already reported these. Find something "
                f"different; do not restate them:\n{already}\n"
            )
        found = _parse_findings(
            _generate(model, prompt), board, f"model:{label}"
        )
        suggested.extend(found)
        passes.append(f"{label}: {len(found)} finding(s)")
        emit(event="judge.done", pass_=label, findings=len(found))

    if profile.per_part_focus:
        focus = [p for p in board.parts if p.is_ic][:MAX_FOCUS_PARTS]
        skipped = len([p for p in board.parts if p.is_ic]) - len(focus)
        for part in focus:
            label = f"focus on {part.ref}"
            emit(event="judge.start", pass_=label)
            prompt = (
                f"{JUDGE_PROMPT}\n\nThe board under review:\n{digest}\n\n"
                + FOCUS_PROMPT.format(
                    ref=part.ref, value=part.value or "unknown", pads=len(part.pads)
                )
            )
            found = _parse_findings(
                _generate(model, prompt),
                board,
                f"model:{label}",
                limit_refs={part.ref},
            )
            suggested.extend(found)
            passes.append(f"{label}: {len(found)} finding(s)")
            emit(event="judge.done", pass_=label, findings=len(found))
        if skipped > 0:
            passes.append(
                f"{skipped} further multi-pin part(s) not examined individually"
            )

    suggested = _dedupe(suggested)

    for round_index in range(profile.refute_rounds):
        kept: list[Finding] = []
        for finding in suggested:
            emit(event="refute.start", title=finding.title)
            prompt = REFUTE_PROMPT.format(
                title=finding.title,
                detail=finding.detail,
                refs=", ".join(finding.refs) or "(none)",
                nets=", ".join(finding.nets) or "(none)",
                digest=digest,
            )
            verdict = _loads(_generate(model, prompt))
            raw_refuted = verdict.get("refuted")
            valid_verdict = isinstance(raw_refuted, bool)
            # Deep review is an allow-list: a claim survives only when the
            # refuter returns an explicit JSON boolean false. Malformed output
            # must not silently promote an unverified claim into the report.
            refuted = raw_refuted if valid_verdict else True
            reason = str(verdict.get("reason", "")).strip()
            if not valid_verdict:
                reason = "invalid refutation response; claim was not verified"
            finding.checks.append(
                ("refuted: " if refuted else "survived refutation: ")
                + (reason or "no reason given")
            )
            emit(event="refute.done", title=finding.title, refuted=refuted)
            if not refuted:
                kept.append(finding)
        passes.append(
            f"refutation round {round_index + 1}: "
            f"{len(kept)} of {len(suggested)} survived"
        )
        suggested = kept

    return suggested, passes


def _generate(model: JudgeModel, prompt: str) -> str:
    try:
        return model.generate(prompt, temperature=0.0, max_output_tokens=8192)
    except Exception:
        # A failed model call loses the judgment half of the review; it must
        # not lose the proven half. The caller reports the loss.
        raise


def _dedupe(findings: list[Finding]) -> list[Finding]:
    """Collapse the same claim arriving from two passes."""
    seen: dict[tuple, Finding] = {}
    for finding in findings:
        key = (finding.title.strip().lower(), finding.refs, finding.nets)
        if key not in seen:
            seen[key] = finding
    return list(seen.values())
