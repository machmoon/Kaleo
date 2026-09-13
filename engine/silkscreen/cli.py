"""Command line: ``python -m silkscreen "an stm32 stepper driver"``."""

from __future__ import annotations

import argparse
import contextlib
import os
import sys
import time
from pathlib import Path

from .agents import ModelError, generate_pcb
from .agents.effort import DEFAULT_EFFORT, UNSET, Effort, slider
from .agents.model import DEFAULT_MODEL, GeminiModel
from .agents.review import Severity

_SEVERITY_MARK = {
    Severity.BLOCKER: "!!",
    Severity.MARGINAL: " !",
    Severity.NOTE: "  ",
}


def _load_dotenv(path: Path) -> None:
    """Minimal .env reader, so a key never has to be exported by hand."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _case_main(argv: list[str]) -> int:
    """``silkscreen case board.kicad_pcb`` -- retrofit a case onto a board.

    ``--no-model`` builds the deterministic default-spec case with no API
    call at all: default :func:`parse_enclosure_spec` dict + measured board
    envelope + the build123d kernel, fully offline.

    The kernel is the only engine (docs/ai-cad-plan.md v3). Without the
    ``cad`` extra the command refuses, naming what to install, and exits
    non-zero rather than writing a lesser case.
    """
    parser = argparse.ArgumentParser(
        prog="silkscreen case",
        description="Generate a 3D-printable case (STEP + STLs) for an "
        "existing KiCad board. Needs the CAD kernel: pip install -e '.[cad]'",
    )
    parser.add_argument("board", help="the .kicad_pcb to fit a case around")
    parser.add_argument("-o", "--output", default="enclosure.step",
                        help="where to write the STEP assembly (default: "
                             "%(default)s); the two STLs and the snapshot "
                             "PNGs land beside it under the same stem")
    parser.add_argument("--intent", default="",
                        metavar="TEXT",
                        help="natural-language case intent, e.g. "
                             "'rounded corners, USB cutout left'")
    parser.add_argument("--no-model", action="store_true",
                        help="build the deterministic default case with no "
                             "model call (fully offline)")
    parser.add_argument("--rigorous", action="store_true",
                        help="run the full strict verify-and-repair loop "
                             "(slower); default is demo-fast, where a failing "
                             "kernel clause rides the receipt as a note")
    parser.add_argument("--assemble", action="store_true",
                        help="also write <stem>-assembly.step: the real board "
                             "(from `kicad-cli pcb export step`, with every "
                             "part's 3D model) seated on the standoffs inside "
                             "the case, and the assembly clause report "
                             "measured there. Needs KiCad on the machine")
    args = parser.parse_args(argv)

    from .enclosure.board_shape import board_envelope
    from .enclosure.cad import KERNEL_MISSING, build_enclosure, kernel_available
    from .enclosure.errors import EnclosureError
    from .enclosure.ir import parse_enclosure_spec
    from .enclosure.kernel import verify_model

    # One engine, and it refuses in words rather than degrading: the v1
    # OpenSCAD emitter built a different box behind a receipt that could not
    # fail (docs/ai-cad-plan.md v3, 2026-09-08).
    if not kernel_available():
        print(f"error: {KERNEL_MISSING}", file=sys.stderr)
        return 2

    try:
        envelope = board_envelope(args.board)
    except (OSError, ValueError, EnclosureError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    model_built = None  # the EnclosureModel the kernel built
    kernel = None       # its KernelReport
    if args.no_model:
        if args.intent:
            print("note: --no-model ignores --intent (deterministic default "
                  "case)", file=sys.stderr)
        spec = parse_enclosure_spec({})
        repair_rounds = 0
        try:
            model_built = build_enclosure(spec, envelope)
            kernel = verify_model(model_built, spec, envelope)
        except EnclosureError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    else:
        _load_dotenv(Path.cwd() / ".env")
        try:
            from .agents.enclosure import propose_enclosure
            from .agents.model import CHEAP_MODEL
        except ImportError as exc:
            print(
                "error: model-driven case generation is not available in this "
                f"build ({exc}); use --no-model for the deterministic default "
                "case",
                file=sys.stderr,
            )
            return 2
        try:
            model = GeminiModel(CHEAP_MODEL)
        except ModelError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        from .agents.stages import proposal_fields

        try:
            # The loop's accepted KernelReport is the receipt; no
            # re-verification.
            spec, repair_rounds, model_built, kernel, _brief = proposal_fields(
                propose_enclosure(
                    model, envelope, style_hint=args.intent, rigorous=args.rigorous
                )
            )
        except Exception as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    out = Path(args.output)
    # The STEP is primary and the STLs derived (plan decision 19).
    from .enclosure.cad import export_model
    from .enclosure.snapshot import render_packet

    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        exports = export_model(model_built, out.parent, out.stem)
    except (EnclosureError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print("engine: kernel (build123d)")
    for path in (exports.step, exports.base_stl, exports.lid_stl):
        print(f"wrote {path}")
    try:
        for path in render_packet(model_built, out.parent):
            print(f"wrote {path}")
    except Exception as exc:  # noqa: BLE001 - snapshots are advisory
        print(f"note: snapshots not rendered: {exc}", file=sys.stderr)
    if out != exports.step:
        # ``-o`` named something other than ``<stem>.step`` in that
        # directory (a different suffix): honour it with a copy.
        out.write_text(exports.step.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"wrote {out}")

    # The 3D assembly is opt-in and additive: the case files above are the
    # product and are already written. A missing kicad-cli refuses in words
    # naming the command (there is no invented board model to fall back to),
    # and the exit code says so, because the engineer asked for the assembly.
    assembly_failed = False
    if args.assemble:
        from .enclosure import assembly as assembly_mod

        try:
            board_step = assembly_mod.export_board_step(
                args.board, out.parent / f"{out.stem}-board.step"
            )
            print(f"wrote {board_step}")
            seated = assembly_mod.build_assembly(model_built, envelope, board_step)
            asm_path = assembly_mod.export_assembly(
                seated, out.parent, f"{out.stem}-assembly"
            )
            print(f"wrote {asm_path}")
            asm_report = assembly_mod.verify_assembly(seated, spec)
            print("Assembly report:")
            print(asm_report.text())
            if not asm_report.passed:
                failed = ", ".join(asm_report.failed)
                print(f"  note: assembly clauses failed: {failed}", file=sys.stderr)
                assembly_failed = args.rigorous
        except EnclosureError as exc:
            print(f"error: --assemble: {exc}", file=sys.stderr)
            assembly_failed = True

    if repair_rounds:
        print(f"  repair rounds: {repair_rounds}", file=sys.stderr)
    if kernel is not None:
        # The clause-by-clause receipt, every line a signed margin.
        print("Kernel report:")
        print(kernel.text())
        if not kernel.passed:
            failed = ", ".join(kernel.failed)
            if args.rigorous:
                print(f"error: kernel clauses failed: {failed}", file=sys.stderr)
                return 1
            print(f"  note: kernel clauses failed: {failed} (files still "
                  "written; --rigorous makes this an error)", file=sys.stderr)

    return 1 if assembly_failed else 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # A tiny subcommand dispatch, kept out of argparse so the historical
    # ``silkscreen "an ldo board"`` form keeps working unchanged.
    if argv and argv[0] == "case":
        return _case_main(argv[1:])

    parser = argparse.ArgumentParser(
        prog="silkscreen",
        description="Generate a placed KiCad PCB from a description, and review it.",
    )
    parser.add_argument("intent", help="what to build, in plain language")
    parser.add_argument("-o", "--output", default="board.kicad_pcb",
                        help="where to write the .kicad_pcb (default: %(default)s)")
    parser.add_argument("-d", "--datasheet", action="append", default=[],
                        metavar="PART=URL",
                        help="a datasheet to read first; repeatable")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--effort", choices=[e.value for e in Effort],
                        default=DEFAULT_EFFORT.value,
                        help="how hard to think (default: %(default)s). "
                             "fast gives the solver a quarter of the budget "
                             "and the repair loop one round; balanced is "
                             "what every run did before this flag existed; "
                             "thorough spends more than twice balanced's "
                             "solver budget and keeps every stage on the "
                             "reasoning model tier")
    # Default None, not the old literals: an explicitly-passed budget must be
    # distinguishable from one nobody chose, or --effort could never decide
    # either of them.
    parser.add_argument("--time-limit", type=float, default=None,
                        help="placement solver budget in seconds; overrides "
                             "--effort's budget")
    parser.add_argument("--repairs", type=int, default=None,
                        help="how many times to send a bad proposal back; "
                             "overrides --effort's repair budget")
    parser.add_argument("--no-review", action="store_true",
                        help="skip the adversarial review pass")
    parser.add_argument("--no-route", action="store_true",
                        help="stop after placement, leaving the copper empty")
    parser.add_argument("--board-only", action="store_true",
                        help="write only the routed .kicad_pcb, no schematic, "
                             "project file or pre-routing board")
    parser.add_argument("--case", action="store_true",
                        help="also generate a 3D-printable case; writes "
                             "enclosure.step and its STLs beside the output "
                             "(needs the cad extra)")
    parser.add_argument("--case-style", default="", metavar="TEXT",
                        help="natural-language case intent, e.g. "
                             "'rounded corners, USB cutout left'")
    parser.add_argument("--rigorous", action="store_true",
                        help="with --case: run the case proposal's full "
                             "strict verify-and-repair loop (slower); "
                             "default is demo-fast, where a failing kernel "
                             "clause rides the receipt as a note")
    # On by default. A request is usually a handful of words, and the whole
    # reason this stage exists is that "a home security camera system" used to
    # become a board with no power input. A planning stage nobody switches on
    # produces exactly the board it was built to prevent, so the flag is the
    # way OUT of it, not the way in.
    parser.add_argument("--no-plan", dest="plan", action="store_false",
                        help="skip the planning pass and design straight from "
                             "the request. Saves one model call; the request "
                             "then has to carry its own detail, including "
                             "where power comes in")
    parser.set_defaults(plan=True)
    parser.add_argument("--bom", action="store_true",
                        help="also source the parts in the background: a "
                             "proposed manufacturer and part number per part, "
                             "each datasheet URL probed for a real PDF; "
                             "writes bom.csv beside the output")
    parser.add_argument("--simulate", action="store_true",
                        help="also verify the circuit in SPICE in the "
                             "background: the model writes a testbench and "
                             "the clauses the intent implies, ngspice runs "
                             "it, and every clause prints with its measured "
                             "value and signed margin")
    args = parser.parse_args(argv)

    _load_dotenv(Path.cwd() / ".env")

    datasheets: dict[str, str] = {}
    for entry in args.datasheet:
        part, sep, url = entry.partition("=")
        if not sep:
            parser.error(f"--datasheet needs PART=URL, got {entry!r}")
        datasheets[part.strip()] = url.strip()

    try:
        model = GeminiModel(args.model)
    except ModelError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    for part in datasheets:
        print(f"reading datasheet: {part}", file=sys.stderr)

    # Opt-in only, so a run without --case or --bom makes the exact call it
    # always made (the plan's both-drivers-identical-by-default rule).
    opt_in_kwargs = (
        {
            "enclosure": True,
            "enclosure_style": args.case_style,
            "enclosure_rigorous": args.rigorous,
        }
        if args.case
        else {}
    )
    # The same rule for the BOM: only a --bom run reaches the pipeline with
    # the sourcing switch at all.
    opt_in_kwargs["plan"] = args.plan
    if args.bom:
        opt_in_kwargs["sourcing"] = True
    # And for the SPICE verdict: only a --simulate run spends the call.
    if args.simulate:
        opt_in_kwargs["simulate"] = True

    # Progress. Without this the CLI is silent for the whole run -- and the run
    # is dominated by the placement solver, which spends its entire
    # --time-limit budget on every board (CP-SAT reaches "feasible" and cannot
    # prove optimality, so it never returns early). Twenty seconds of nothing
    # reads as a hang, which is a large part of what "slow" means here. Only
    # stage boundaries are printed, on stderr, so stdout stays the report a
    # caller may be parsing.
    started = time.monotonic()

    def on_event(event: dict) -> None:
        name = event.get("event")
        if name not in ("stage.start", "stage.done"):
            return
        stage = event.get("stage")
        if not stage:
            return
        mark = "..." if name == "stage.start" else "done"
        # A callback that raises aborts the run, by design -- the service
        # relies on that to stop a run whose client disconnected. But that
        # contract is for real errors. Losing a twenty-second paid run because
        # someone piped the output through `head` and stderr went away is a
        # worse failure than the one it protects against, and progress
        # printing is the one thing here that can fail for a reason having
        # nothing to do with the board. Only the write is guarded, and only
        # for these two: anything else still propagates and still aborts.
        with contextlib.suppress(BrokenPipeError, OSError):
            print(f"[{time.monotonic() - started:6.1f}s] {stage} {mark}",
                  file=sys.stderr)

    try:
        result = generate_pcb(
            model,
            args.intent,
            datasheets=datasheets,
            output=args.output,
            effort=args.effort,
            # UNSET, not the flag's value: the level decides a budget the
            # caller did not name, and reports in result.effort.overrides
            # when the caller did.
            max_repairs=UNSET if args.repairs is None else args.repairs,
            time_limit_s=UNSET if args.time_limit is None else args.time_limit,
            review=not args.no_review,
            route=not args.no_route,
            emit_stages=not args.board_only,
            on_event=on_event,
            **opt_in_kwargs,
        )
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print()
    print(result.summary())
    receipt = getattr(result, "effort", None)
    if receipt is not None:
        # Printed with the board, not buried at the end: the whole point of
        # the default being `fast` is that nobody may mistake the board it
        # produced for one a thorough run would have produced.
        print()
        print(slider(Effort(receipt.level)))
        print(f"  {receipt.headline()}")
        for note in receipt.notes:
            print(f"  note: {note}")
    print()
    for part in result.board.parts:
        print(f"  {part.ref:<5} {part.footprint.name:<20} {part.value}")

    plan_result = getattr(result, "plan", None)
    if plan_result is not None:
        print()
        if plan_result.plan is not None:
            print("Plan:")
            for line in plan_result.plan.brief_text().splitlines():
                print(f"  {line}")
        for warning in plan_result.warnings:
            print(f"  warning: {warning}")

    if result.unread_datasheets:
        # Said before the review, because it changes what the review is worth:
        # a part whose datasheet could not be read was designed and critiqued
        # from the model's general knowledge. This used to be an event only,
        # and the CLI passed no `on_event`, so it was invisible here. The
        # progress callback above now takes stage boundaries only, so this
        # still has to be printed from the result.
        print()
        for line in result.unread_datasheets:
            print(f"Ungrounded: {line}")

    if not result.review.ok:
        # A review that did not run, or whose answer could not be read, gets a
        # line saying so. Printing nothing here is what let an unreadable
        # critic answer look exactly like a board with nothing to flag.
        print()
        print(f"Review: {result.review.note()}")
    elif result.findings:
        print()
        print("Review:")
        for f in result.findings:
            who = "/".join(d.value for d in f.agreed_by) or f.domain.value
            print(f"  {_SEVERITY_MARK[f.severity]} [{who}] {f.title}")
            if f.detail:
                print(f"       {f.detail}")
            if f.citation:
                print(f"       cited: {f.citation}")
            if f.suggested_fix:
                print(f"       fix:   {f.suggested_fix}")

    # A finding the filter threw away and a finding the merge folded into
    # another are both rows the critic wrote that the list above does not
    # show. Printed here for the same reason the unreadable-answer line above
    # is printed: a filter nobody can see is indistinguishable from a critic
    # that said nothing.
    for line in result.review.dropped:
        print(f"  -- dropped: {line}")
    for line in result.review.merged:
        print(f"  -- merged:  {line}")

    # Say plainly which nets have no copper. A board reported as routed when
    # some nets are still ratsnest is the failure this output exists to
    # prevent -- the missing connections are invisible until fabrication.
    if result.route is not None:
        print()
        print(f"Routing: {result.route.summary()}")
        for net, reason in sorted(result.route.unrouted.items()):
            print(f"  unrouted {net}: {reason}")

    # The case's kernel receipt, or its honest absence: an exhausted repair
    # budget -- or a missing cad extra -- degrades to no case, never to a
    # failed run or to a lesser case.
    if args.case:
        case = getattr(result, "enclosure", None)
        print()
        if case is None:
            print("case: generation failed; the board is delivered without one",
                  file=sys.stderr)
        else:
            if case.repair_rounds:
                print(f"  repair rounds: {case.repair_rounds}", file=sys.stderr)
            kernel = getattr(case, "kernel", None)
            if kernel is not None:
                print("Kernel report:")
                print(kernel.text())

    # The BOM's counts, with the vocabulary kept honest: a part number here
    # is a proposal nobody has checked against a distributor, and only a
    # datasheet the probe actually saw as a PDF counts as verified.
    if args.bom:
        bom = getattr(result, "sourcing", None)
        print()
        if bom is None:
            print("BOM: sourcing did not run; the board is delivered without one",
                  file=sys.stderr)
        else:
            print(
                f"BOM: {len(bom.parts)} parts, {bom.proposed} part number(s) "
                f"proposed, {bom.verified} datasheet(s) verified, "
                f"{bom.unresolved} unresolved"
            )
            for warning in bom.warnings:
                print(f"  note: {warning}", file=sys.stderr)
            if args.board_only:
                print("bom.csv not written (--board-only)", file=sys.stderr)

    # The SPICE verdict, one line per clause with its signed margin -- or
    # the one sentence saying why there is no verdict (no simulator, a part
    # with no model, no usable testbench, a simulator that raised). Never
    # nothing: a run that verified nothing must not look like a run that
    # verified everything.
    if args.simulate:
        sim = getattr(result, "simulation", None)
        print()
        if sim is None:
            print("Simulation: did not run; the board is delivered unverified",
                  file=sys.stderr)
        elif sim.ran:
            print(f"Simulation ({sim.analysis}, {sim.simulator}):")
            for clause in sim.clauses:
                flag = " [critical]" if clause.critical else ""
                print(f"  {clause.line()}{flag}")
            for finding in sim.findings:
                print(f"  {finding.severity.upper()}: {finding.title}")
        else:
            print(f"Simulation {sim.status}: {sim.detail}")
        for warning in sim.warnings if sim is not None else ():
            print(f"  note: {warning}", file=sys.stderr)

    for w in result.board.warnings:
        print(f"  note: {w}", file=sys.stderr)

    if result.artifacts:
        print()
        for path in result.artifacts:
            print(f"wrote {path}")
        if result.project_path:
            print()
            print(f"open in KiCad:  {result.project_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
