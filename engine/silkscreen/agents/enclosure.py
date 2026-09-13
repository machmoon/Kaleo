"""Propose an enclosure, then make the model fix its own mistakes.

The :mod:`silkscreen.agents.propose` loop, applied to 3D (docs/ai-cad-plan.md
v2, decision 18): the model's output goes through
:func:`silkscreen.enclosure.ir.parse_enclosure_spec`, then through
:func:`silkscreen.enclosure.cad.build_enclosure` and
:func:`silkscreen.enclosure.kernel.verify_model`, whose signed-margin clauses
are the acceptance gate. A :class:`~silkscreen.enclosure.errors.KernelError`
goes back to the model as a repair item naming its failure class; so does a
failing clause.

The kernel is the **only** path (docs/ai-cad-plan.md v3, 2026-09-08). Without
build123d there is no case: :func:`propose_enclosure` raises
:class:`~silkscreen.enclosure.errors.KernelUnavailable` naming the ``cad``
extra before it spends a model call. The v1 OpenSCAD emitter used to stand in
here and it built a measurably different box behind a fit receipt that could
not fail, which is a worse outcome than a refusal in words.

How hard the loop pushes back is the ``rigorous`` flag's call. The **fast
default** (``rigorous=False``, demo speed) repairs spec-validation failures
and a build that raises, allows one repair round, and ships the first spec
that *builds* -- the kernel report rides along even when clauses fail, the
honest receipt. **Rigorous mode** (``rigorous=True``) is the strict loop:
every failure -- JSON shape, unprintable wall, a cutout naming a part the
board does not have, a kernel clause -- is batched back as a single repair
prompt over three repair rounds, and a failing report blocks. The loop is
bounded and gives up loudly with :class:`EnclosureProposalError`.

An optional **critic** (decision 17) looks at a rendered snapshot grid of the
built model and names at most a few findings; they are filtered to parts the
board has or cutouts the spec names (the :mod:`~silkscreen.agents.review`
rule), appended to the kernel report as warnings, and are never a gate.

The model never receives or invents a raw dimension to transcribe (plan
decision 3): the prompt carries the *measured* board facts -- outline size,
part rectangles, heights, mounting holes, connector plugs, edge-adjacent refs
with their faces -- purely so the model can choose style within bounds, and
the deterministic builder injects every millimetre from the envelope and
:mod:`silkscreen.enclosure.rules`, not from the model's answer.

Intended live tier: :data:`~silkscreen.agents.model.CHEAP_MODEL` -- choosing a
lid style is a mechanical pass, not a reasoning one. The tier is the caller's
to construct; this module only ever sees the :class:`Model` protocol.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, NamedTuple

from ..enclosure.board_shape import BoardEnvelope, PartExtent
from ..enclosure.cad import (
    KERNEL_MISSING,
    EnclosureModel,
    build_enclosure,
    kernel_available,
)
from ..enclosure.errors import (
    CutoutError,
    EnclosureError,
    EnclosureValidationError,
    KernelError,
    KernelUnavailable,
)
from ..enclosure.ir import EnclosureSpec, parse_enclosure_spec
from ..enclosure.kernel import KernelReport, verify_model
from ..enclosure.rules import PLUG_ENVELOPES
from ..enclosure.snapshot import render_grid
from ..packing import Layer
from ..units import mm, to_mm
from .model import Document, Model, ModelError, parse_json

__all__ = [
    "ENCLOSURE_PROMPT",
    "CRITIC_PROMPT",
    "EnclosureProposal",
    "EnclosureProposalError",
    "propose_enclosure",
]

#: A part whose courtyard sits within this of a board edge is offered to the
#: model as a cutout candidate on that face.
_EDGE_NEAR_NM: int = mm(3.0)

#: Repair budgets per mode, applied when the caller does not pass
#: ``max_repairs`` explicitly: fast mode gets one round (speed over rigor,
#: by default), rigorous mode keeps the strict loop's three.
_FAST_MAX_REPAIRS: int = 1
_RIGOROUS_MAX_REPAIRS: int = 3

#: The critic is bounded (decision 17): at most this many model calls, and
#: at most this many findings kept from each.
_CRITIC_ROUNDS: int = 2
_CRITIC_MAX_FINDINGS: int = 5


class EnclosureProposalError(EnclosureError):
    """The model could not produce a valid enclosure within the repair budget.

    ``attempts`` counts every round made, so the caller can report honestly
    how hard the model tried before the run degraded (plan decision 5).
    """

    def __init__(self, message: str, attempts: int):
        self.attempts = attempts
        super().__init__(message)


class EnclosureProposal(NamedTuple):
    """What :func:`propose_enclosure` accepted, with every receipt attached.

    ``spec`` is the accepted :class:`~silkscreen.enclosure.ir.EnclosureSpec`;
    ``repair_rounds`` how many corrections the model needed -- a genuinely
    useful quality signal, mirrored into the ``stage.done`` event.

    ``model`` is the built :class:`~silkscreen.enclosure.cad.EnclosureModel`
    and ``kernel`` its :class:`~silkscreen.enclosure.kernel.KernelReport`
    (critic findings appended to its ``warnings``) -- the kernel report is
    the only receipt and the only repair signal, since the kernel is the only
    path. ``brief`` is the text the model was told -- the measured facts
    block plus the style hint -- kept so a caller can show what the proposal
    was made against.
    """

    spec: EnclosureSpec
    repair_rounds: int
    model: EnclosureModel | None = None
    kernel: KernelReport | None = None
    brief: str = ""


#: The marker ``"ENCLOSURE-SPEC v2"`` is the v2 contract (docs/ai-cad-plan.md
#: decision 14). The literal ``"ENCLOSURE-SPEC v1"`` is kept in the same line
#: on purpose: every workstream's ``ScriptedModel.by_marker`` keys on it, and
#: a substring match is how those tests reach this prompt.
ENCLOSURE_PROMPT = """\
You are choosing the style of a 3D-printed enclosure for a finished PCB
(ENCLOSURE-SPEC v2; supersedes ENCLOSURE-SPEC v1). Respond with ONE JSON
object -- no prose, no code fence.

{
  "wall_mm": <number, >= 1.2>,
  "clearance_mm": <number, board-to-cavity gap; 1.0 is typical, below 0.5 binds>,
  "corner_radius_mm": <number, 0 for square corners>,
  "lid": "lip" | "screw" | "snap" | "none",
  "mount": "holes" | "pins" | "corners" | "none",
  "insert": "M2" | "M2.5" | "M3" | "M4" | "self_tap",
  "material": "PLA" | "PETG" | "ABS",
  "cutouts": [
    {"id": "<unique identifier>", "ref": "<board ref, e.g. J1>",
     "face": "left" | "right" | "front" | "back" | "top",
     "margin_mm": <number, extra opening margin around the plug>}
  ],
  "vents": true | false,
  "label": "<short text embossed on the lid>" | null
}

What each "mount" means:
  "holes":   standoffs with insert bores placed at the board's own mounting
             holes (only valid when the board facts list mounting holes);
  "pins":    standoffs with locating pins through the mounting holes, the lid
             presses the board down (also needs mounting holes);
  "corners": four plain corner standoffs under the board edge (no holes needed);
  "none":    the board rests on the floor with no standoffs.
"insert" is the screw size bored into the standoffs ("self_tap" is an
undersize hole for a self-tapping screw); it also sizes a "screw" lid's
screws. "lip" is a ring lip that registers in the cavity; "snap" adds a
bead-and-groove detent to it ("friction" is accepted as "lip").

Hard rules -- a proposal breaking any of these is rejected automatically:

1. You choose STYLE only. Every dimension is measured -- from the PCB file
   and from the design-rule tables (wall fits, insert bores, plug sizes) --
   and injected by the builder; never invent or repeat a board measurement.
2. A cutout's "ref" must name a part listed in the board facts below. For a
   known connector the opening is sized from the mating PLUG (a USB-C plug
   overmold, not the receptacle); for any other part from its real
   courtyard. You only pick the part, the face, and the margin.
3. Only put a side cutout on a face the part actually sits near (the board
   facts name each part's nearby faces); a part far from a wall gets a tunnel
   to nowhere and is rejected.
4. "mount": "holes" or "pins" needs mounting holes on the board; when the
   facts say there are none, choose "corners" or "none".
5. Walls below 1.2 mm do not print. Clearance below 0.5 mm binds.
6. Omit "cutouts" entries you are not sure about -- a solid case that fits is
   better than an opening onto the wrong part.
"""

#: The critic's brief (decision 17). Findings are advisory; the marker lets a
#: ``ScriptedModel`` answer it separately from the spec prompt.
CRITIC_PROMPT = """\
You are reviewing a rendered 3D-printed enclosure for a PCB (ENCLOSURE-CRITIC).
The image is a 2x2 grid: isometric, opposite isometric, top view, and a
vertical section through the tallest part. Geometry has already been checked
by a CAD kernel; look only for what a picture shows and numbers do not --
an opening on the wrong side, a lid that cannot come off, a boss in the way
of a plug, a part poking through a wall.

Respond with ONE JSON array of at most 5 findings -- no prose, no code fence:
[{"ref": "<board ref or cutout id the finding is about>", "finding": "<one sentence>"}]
Return [] when you see nothing wrong. A finding must name a part or cutout
listed below; findings about anything else are discarded.
"""


def _facts_block(envelope: BoardEnvelope) -> str:
    """The measured board, rendered for the prompt. Deterministic text.

    Everything here is derived from the envelope so two runs over the same
    board produce byte-identical prompts. Faces use the package frame map:
    ``front`` is the board edge at maximum KiCad Y, ``back`` at minimum, and
    a mounting hole's position is given in that same frame ("from left",
    "from front").
    """
    size_x = to_mm(envelope.x_max_nm - envelope.x_min_nm)
    size_y = to_mm(envelope.y_max_nm - envelope.y_min_nm)
    lines = [
        f"Board outline: {size_x:.2f} x {size_y:.2f} mm, "
        f"substrate {to_mm(envelope.thickness_nm):.2f} mm thick.",
        f"Tallest part: {to_mm(envelope.max_height_nm):.2f} mm above the board.",
    ]
    if envelope.max_height_bottom_nm > 0:
        lines.append(
            f"Tallest bottom-side part: {to_mm(envelope.max_height_bottom_nm):.2f} "
            "mm below the board (it must fit the standoff gap)."
        )
    if envelope.mounting_holes:
        lines.append(
            "Mounting holes (standoffs go here for mount 'holes' or 'pins'):"
        )
        for hole in envelope.mounting_holes:
            from_left = to_mm(hole.x_nm - envelope.x_min_nm)
            # front = the KiCad max-Y edge (the package frame map).
            from_front = to_mm(envelope.y_max_nm - hole.y_nm)
            lines.append(
                f"  {hole.ref}: {to_mm(hole.drill_nm):.2f} mm drill, "
                f"{from_left:.2f} mm from left, {from_front:.2f} mm from front"
            )
    else:
        lines.append(
            "No mounting holes: choose mount corners, pins or none, never holes."
        )
    lines.append("Parts (sizes measured from the board file; you never restate them):")
    for part in envelope.parts:
        width = to_mm(part.x_max_nm - part.x_min_nm)
        depth = to_mm(part.y_max_nm - part.y_min_nm)
        faces = _near_faces(part, envelope)
        near = f"; near faces: {', '.join(faces)}" if faces else ""
        side = "; bottom side" if part.side is Layer.BOTTOM else ""
        plug = ""
        if part.connector is not None and part.connector in PLUG_ENVELOPES:
            plug = (
                f"; connector {part.connector} (opening sized from the "
                f"{PLUG_ENVELOPES[part.connector].name})"
            )
        lines.append(
            f"  {part.ref}: {width:.2f} x {depth:.2f} mm footprint, "
            f"{to_mm(part.height_nm):.2f} mm tall{side}{plug}{near}"
        )
    return "\n".join(lines)


def _near_faces(part: PartExtent, envelope: BoardEnvelope) -> list[str]:
    """Which enclosure faces this part sits close enough to for a cutout."""
    faces: list[str] = []
    if part.x_min_nm - envelope.x_min_nm <= _EDGE_NEAR_NM:
        faces.append("left")
    if envelope.x_max_nm - part.x_max_nm <= _EDGE_NEAR_NM:
        faces.append("right")
    # front = the KiCad max-Y edge; back = min-Y (the package frame map).
    if envelope.y_max_nm - part.y_max_nm <= _EDGE_NEAR_NM:
        faces.append("front")
    if part.y_min_nm - envelope.y_min_nm <= _EDGE_NEAR_NM:
        faces.append("back")
    return faces


def _known_names(spec: EnclosureSpec, envelope: BoardEnvelope) -> set[str]:
    """What a critic finding may be about: board refs and cutout ids."""
    return {p.ref for p in envelope.parts} | {c.id for c in spec.cutouts}


def _parse_findings(raw: str, known: set[str]) -> tuple[list[str], int]:
    """Critic output -> ``(kept findings, dropped count)``.

    The :mod:`~silkscreen.agents.review` rule: a finding naming nothing the
    board or the spec has is dropped, not reported unlocatable. Malformed
    output raises :class:`ModelError` (from :func:`parse_json`); the caller
    turns that into a warning, because the critic is never a gate.
    """
    data = parse_json(raw)
    if isinstance(data, dict):
        data = data.get("findings", [])
    if not isinstance(data, list):
        raise ModelError("critic did not return a JSON array of findings")
    kept: list[str] = []
    dropped = 0
    for entry in data[:_CRITIC_MAX_FINDINGS]:
        if not isinstance(entry, dict):
            dropped += 1
            continue
        ref = str(entry.get("ref", "")).strip()
        finding = str(entry.get("finding", "")).strip()
        if ref in known and finding:
            kept.append(f"{ref}: {finding}")
        else:
            dropped += 1
    return kept, dropped


def _critique(
    model: Model,
    built: EnclosureModel,
    spec: EnclosureSpec,
    envelope: BoardEnvelope,
    on_event: Callable[[dict[str, Any]], None] | None,
) -> tuple[str, ...]:
    """Show the model a snapshot grid and collect its findings, bounded.

    Round one asks for findings; round two, only when round one found
    something, shows the same image again with those findings and asks which
    survive -- the refutation pass of ``audit/effort.py``, so a critic that
    saw a shadow as a hole gets one chance to take it back. Never more than
    :data:`_CRITIC_ROUNDS` calls. Every failure path -- no renderer, an
    unreadable answer, an unreachable model -- becomes one ``critic:``
    warning rather than an exception: the built case is not lost to advice.
    """
    known = _known_names(spec, envelope)
    try:
        png = render_grid(built)
    except Exception as exc:  # renderer is advisory; never the gate
        return (f"critic: snapshot unavailable ({type(exc).__name__}: {exc})",)
    image = Document(data=png, mime_type="image/png")
    names = ", ".join(sorted(known)) or "(none)"
    prompt = f"{CRITIC_PROMPT}\nParts and cutouts on this board: {names}\n"

    findings: list[str] = []
    dropped = 0
    for round_no in range(_CRITIC_ROUNDS):
        if round_no > 0:
            if not findings:
                break
            listed = "\n".join(f"  - {f}" for f in findings)
            prompt = (
                f"{CRITIC_PROMPT}\nParts and cutouts on this board: {names}\n\n"
                f"You previously reported these findings. Look again and "
                f"return ONLY the ones you still stand by, in the same JSON "
                f"array shape:\n{listed}\n"
            )
        try:
            raw = model.generate(
                prompt, documents=[image], temperature=0.0, max_output_tokens=1024
            )
            kept, dropped = _parse_findings(raw, known)
        except ModelError as exc:
            if round_no == 0:
                findings = [f"unavailable ({exc})"]
            # A failed refutation round keeps round one's findings.
            break
        findings = kept
        if on_event is not None:
            on_event(
                {
                    "event": "enclosure.critic",
                    "round": round_no + 1,
                    "findings": len(kept),
                    "dropped": dropped,
                }
            )
    return tuple(f"critic: {f}" for f in findings)


def _with_warnings(report: KernelReport, extra: tuple[str, ...]) -> KernelReport:
    if not extra:
        return report
    return KernelReport(clauses=report.clauses, warnings=report.warnings + extra)


def propose_enclosure(
    model: Model,
    envelope: BoardEnvelope,
    *,
    style_hint: str = "",
    rigorous: bool = False,
    max_repairs: int | None = None,
    critic: bool = False,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> EnclosureProposal:
    """Ask for an enclosure spec and repair it until it validates.

    Returns an :class:`EnclosureProposal` whose ``model`` and ``kernel``
    carry the built geometry and its clause report, and ``brief`` what the
    model was told. The kernel report is the only receipt.

    ``rigorous`` selects how much the loop pushes back; **fast is the
    default** because a demo wants its case now:

    * ``rigorous=False`` (fast): :func:`parse_enclosure_spec` failures and a
      build that raises (:class:`~silkscreen.enclosure.errors.KernelError`,
      :class:`~silkscreen.enclosure.errors.CutoutError`) feed the repair
      loop, with a budget of one repair round unless ``max_repairs`` says
      otherwise. The first spec that *builds* ships: the kernel report is
      attached even when clauses fail, the honest receipt.
    * ``rigorous=True``: the strict loop, three repair rounds unless
      ``max_repairs`` says otherwise. Every failing clause of the
      :class:`~silkscreen.enclosure.kernel.KernelReport` goes back as a
      repair item and only a passing report is accepted.

    ``critic=True`` runs the bounded snapshot critic after acceptance; its
    findings are ``critic:`` warnings on ``kernel`` and never a gate.

    ``on_event`` receives one ``enclosure.round`` event per rejected round,
    one ``enclosure.kernel`` event (``passed``, ``failed`` clause names) per
    kernel verification, and one ``enclosure.critic`` event per critic call.

    Raises:
        KernelUnavailable: build123d is not installed. There is no case
            without it and there is no second-best case to fall back to
            (docs/ai-cad-plan.md v3), so this is raised **before** the first
            model call rather than degraded silently.
        EnclosureProposalError: the model answered, but never with a spec that
            validated (and built, and in rigorous mode passed) within the
            budget. Carries ``attempts``.
        ModelError: the model could not be reached at all. Deliberately not
            wrapped -- an upstream outage is a different condition from a bad
            proposal, and callers route them differently (the
            :func:`~silkscreen.agents.propose.propose_circuit` convention).
    """
    # No kernel, no case: refuse in words before a model call is spent.
    if not kernel_available():
        raise KernelUnavailable(KERNEL_MISSING)
    if max_repairs is None:
        max_repairs = _RIGOROUS_MAX_REPAIRS if rigorous else _FAST_MAX_REPAIRS
    facts = _facts_block(envelope)
    hint = f"\nStyle the user asked for:\n{style_hint}\n" if style_hint else ""
    prompt = f"{ENCLOSURE_PROMPT}\n{hint}\nBoard facts:\n{facts}\n"
    brief = facts
    if style_hint:
        brief = f"{facts}\n\nStyle the user asked for:\n{style_hint}"

    last_errors: list[str] = []
    for round_no in range(max_repairs + 1):
        # A transport failure is NOT wrapped: ModelError propagates so a
        # FallbackModel's failover -- and the service's 502 -- stay intact.
        raw = model.generate(prompt, temperature=0.0, max_output_tokens=4096)

        errors: list[str] = []
        try:
            spec = parse_enclosure_spec(raw)
        except EnclosureValidationError as exc:
            errors = list(exc.errors)
        else:
            built, report, errors = _kernel_round(
                spec, envelope, rigorous, round_no, on_event
            )
            if not errors:
                if critic:
                    report = _with_warnings(
                        report, _critique(model, built, spec, envelope, on_event)
                    )
                return EnclosureProposal(
                    spec=spec,
                    repair_rounds=round_no,
                    model=built,
                    kernel=report,
                    brief=brief,
                )

        last_errors = errors
        if on_event is not None:
            # Engine-generated messages, not model text: safe on the wire,
            # truncated all the same.
            on_event(
                {
                    "event": "enclosure.round",
                    "round": round_no + 1,
                    "errors": len(errors),
                    "first_error": str(errors[0])[:160] if errors else "",
                }
            )
        if round_no == max_repairs:
            break
        # Feed every problem back at once so one round can fix all of them.
        problems = "\n".join(f"  - {e}" for e in errors)
        prompt = (
            f"{ENCLOSURE_PROMPT}\n{hint}\nBoard facts:\n{facts}\n\n"
            f"Your previous proposal was rejected. Fix ALL of these and "
            f"return the corrected JSON object:\n{problems}\n\n"
            f"Your previous proposal was:\n{raw}\n"
        )

    detail = "\n".join(f"  - {e}" for e in last_errors)
    raise EnclosureProposalError(
        f"No valid enclosure after {max_repairs + 1} attempts. "
        f"Final errors:\n{detail}",
        attempts=max_repairs + 1,
    )


def _kernel_round(
    spec: EnclosureSpec,
    envelope: BoardEnvelope,
    rigorous: bool,
    round_no: int,
    on_event: Callable[[dict[str, Any]], None] | None,
) -> tuple[EnclosureModel | None, KernelReport | None, list[str]]:
    """Build and verify one parsed spec on the kernel.

    Returns ``(model, report, errors)``. A build that raises -- a
    :class:`KernelError` naming its failure class, a :class:`CutoutError` --
    is a repair item in **both** modes, since nothing was built to ship. A
    report that fails is a repair item only in rigorous mode; fast mode
    accepts it and the caller attaches it, so the failure rides the receipt
    rather than vanishing.
    """
    try:
        built = build_enclosure(spec, envelope)
    except (KernelError, CutoutError) as exc:
        # str(KernelError) is already "<failure_class>: <detail>".
        return None, None, [str(exc)]
    # Everything the builder had to change to make the spec buildable -- a
    # widened clearance, a raised cavity, a mount that fell back to corner
    # pucks -- is on the model and nowhere else. It rides out on the report
    # so the stage, the service and the CLI all show it: a change made on
    # the user's behalf and never mentioned is the quiet zero this layer
    # exists to prevent.
    report = _with_warnings(
        verify_model(built, spec, envelope),
        tuple(f"build: {w}" for w in built.warnings),
    )
    if on_event is not None:
        on_event(
            {
                "event": "enclosure.kernel",
                "round": round_no + 1,
                "passed": report.passed,
                "failed": list(report.failed),
            }
        )
    errors: list[str] = []
    if rigorous and not report.passed:
        errors = [
            line for line in report.text().splitlines() if line.startswith("FAIL")
        ]
    return built, report, errors
