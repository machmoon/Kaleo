"""Tests for the order module.

Covers ``OrderOptions`` validation, the ``preflight`` manufacturability gate
(the refusal that matters most: unrouted nets), and the manifest/zip
packaging built on top of it.

Most fixtures here are hand-built ``BoardResult`` objects rather than solved
boards, so the suite runs with effectively no solver at all; the one test
that needs a real placement keeps ``time_limit_s`` small.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile

import pytest
from silkscreen.board import BoardResult, PlacedPart, build_board
from silkscreen.footprints import Footprint, Pad
from silkscreen.netlist import parse_circuit_spec
from silkscreen.order import (
    MANIFEST_FILENAME,
    OrderIssue,
    OrderIssueSeverity,
    OrderOptions,
    OrderPreflight,
    SolderMaskColour,
    SurfaceFinish,
    _finalise,
    board_summary,
    order_manifest,
    package_zip,
    preflight,
)
from silkscreen.packing import Layer, PackStatus
from silkscreen.units import mm

# ---------------------------------------------------------------- helpers


def _footprint(ref: str, net: str) -> Footprint:
    """A minimal one-pad footprint: just enough to carry a net onto a board."""
    return Footprint(
        name=f"FP_{ref}",
        pads=[Pad(number="1", x_nm=0, y_nm=0, w_nm=mm(0.6), h_nm=mm(0.6), net=net)],
        courtyard_w_nm=mm(1.0),
        courtyard_h_nm=mm(1.0),
    )


def _placed(
    ref: str, net: str, *, layer: Layer = Layer.TOP, x_nm: int = 0
) -> PlacedPart:
    """A single-pad placed part on ``net``.

    Every net built this way touches exactly one pad, unless the same
    ``net`` name is deliberately reused across more than one part.
    """
    return PlacedPart(
        ref=ref,
        footprint=_footprint(ref, net),
        x_nm=x_nm,
        layer=layer,
    )


def _board(
    parts: list[PlacedPart],
    *,
    width_mm: float = 20.0,
    height_mm: float = 20.0,
    solver_status: str = PackStatus.FEASIBLE.value,
    warnings: tuple[str, ...] = (),
) -> BoardResult:
    """A hand-built board.

    Net names are read off the parts' own pads, so a board assembled purely
    from :func:`_placed` calls has exactly one pad per net.
    """
    nets = [p.footprint.pads[0].net for p in parts if p.footprint.pads]
    return BoardResult(
        parts=list(parts),
        nets=nets,
        width_nm=mm(width_mm),
        height_nm=mm(height_mm),
        solver_status=solver_status,
        warnings=list(warnings),
    )


def _issue(code: str, severity: OrderIssueSeverity, title: str = "t") -> OrderIssue:
    """A throwaway issue for sort/dedup tests; only code/severity/title matter."""
    return OrderIssue(code=code, severity=severity, title=title, detail="d")


def _small_spec():
    """Two passives wired to each other on two nets: small and fast to solve."""
    return parse_circuit_spec(
        {
            "passives": {
                "r1": {"type": "resistor", "value": "10k"},
                "c1": {"type": "capacitor", "value": "100nF"},
            },
            "nets": {
                "NET1": ["r1.1", "c1.1"],
                "NET2": ["r1.2", "c1.2"],
            },
        }
    )


# ---------------------------------------------------------------- OrderOptions


def test_order_options_defaults():
    options = OrderOptions()
    assert options.quantity == 5
    assert options.layers == 2
    assert options.thickness_mm == 1.6
    assert options.surface_finish == SurfaceFinish.LEAD_FREE_HASL
    assert options.mask_colour == SolderMaskColour.GREEN
    assert options.silkscreen_white is True
    assert options.assembly is False
    assert options.assembly_side == "top"
    assert options.panel_columns == 1
    assert options.panel_rows == 1


def test_quantity_zero_is_rejected():
    with pytest.raises(ValueError, match="quantity"):
        OrderOptions(quantity=0)


def test_quantity_negative_is_rejected():
    with pytest.raises(ValueError, match="quantity"):
        OrderOptions(quantity=-1)


def test_quantity_bool_is_rejected():
    """bool is an int subclass; True must not sneak through as quantity=1."""
    with pytest.raises(ValueError, match="quantity"):
        OrderOptions(quantity=True)


def test_layers_must_be_a_supported_count():
    with pytest.raises(ValueError, match="layers"):
        OrderOptions(layers=3)


def test_thickness_must_be_a_stocked_value():
    with pytest.raises(ValueError, match="thickness_mm"):
        OrderOptions(thickness_mm=1.7)


def test_assembly_side_must_be_known():
    with pytest.raises(ValueError, match="assembly_side"):
        OrderOptions(assembly_side="left")


def test_panel_columns_out_of_range_is_rejected():
    with pytest.raises(ValueError, match="panel_columns"):
        OrderOptions(panel_columns=0)


def test_panel_rows_out_of_range_is_rejected():
    with pytest.raises(ValueError, match="panel_rows"):
        OrderOptions(panel_rows=11)


# ------------------------------------------------------- preflight: unrouted nets


def test_preflight_blocks_a_real_board_with_unrouted_nets():
    """The refusal that is the point of the module: this pipeline never
    routes, so every net joining two or more pads is still open copper."""
    board = build_board(_small_spec(), time_limit_s=3.0)
    pre = preflight(board)
    assert pre.orderable is False

    unrouted = next(i for i in pre.issues if i.code == "unrouted-nets")
    assert unrouted.severity == OrderIssueSeverity.BLOCKER
    assert unrouted.title == "2 net(s) have no copper connecting them"
    assert "2 net(s) join two or more pads" in unrouted.detail


def test_single_pad_nets_do_not_trigger_unrouted_nets():
    """The most important negative test in this file: the check is about
    nets that genuinely need copper, not a blanket refusal of every board.

    Every net here touches exactly one pad, so there is nothing to route.
    """
    parts = [_placed(f"R{i}", f"NET{i}", x_nm=mm(3 * i)) for i in range(5)]
    board = _board(parts, width_mm=25.0, height_mm=25.0)
    pre = preflight(board)
    codes = {issue.code for issue in pre.issues}
    assert "unrouted-nets" not in codes


# --------------------------------------------------- preflight: structural blockers


def test_zero_parts_is_a_blocker():
    board = _board([], width_mm=20.0, height_mm=20.0)
    pre = preflight(board)
    assert [i.code for i in pre.issues] == ["no-parts"]
    assert pre.issues[0].severity == OrderIssueSeverity.BLOCKER
    assert pre.orderable is False


def test_zero_width_outline_is_a_blocker():
    parts = [_placed("R1", "NET1"), _placed("R2", "NET2", x_nm=mm(3))]
    board = _board(parts, width_mm=0.0, height_mm=20.0)
    pre = preflight(board)
    codes = {i.code for i in pre.issues}
    assert "degenerate-outline" in codes
    assert "no-parts" not in codes

    outline = next(i for i in pre.issues if i.code == "degenerate-outline")
    assert outline.severity == OrderIssueSeverity.BLOCKER
    assert pre.orderable is False


# ----------------------------------------------- preflight: warnings and notes


def test_tiny_board_warns_but_stays_orderable():
    parts = [_placed("R1", "NET1"), _placed("R2", "NET2", x_nm=mm(2))]
    board = _board(parts, width_mm=5.0, height_mm=20.0)
    pre = preflight(board)
    assert [i.code for i in pre.issues] == ["tiny-board"]
    assert pre.issues[0].severity == OrderIssueSeverity.WARNING
    assert pre.blockers == ()
    assert pre.orderable is True, "a warning must not block ordering"


def test_fallback_status_warns():
    board = _board([_placed("R1", "NET1")], solver_status=PackStatus.FALLBACK.value)
    pre = preflight(board)
    assert [i.code for i in pre.issues] == ["fallback-placement"]
    assert pre.issues[0].severity == OrderIssueSeverity.WARNING


def test_feasible_status_does_not_warn():
    """FEASIBLE is the normal outcome on a real board and must not cry wolf."""
    board = _board([_placed("R1", "NET1")], solver_status=PackStatus.FEASIBLE.value)
    pre = preflight(board)
    assert pre.issues == ()


def test_solver_warnings_produce_a_note():
    board = _board(
        [_placed("R1", "NET1")],
        warnings=("CP-SAT trimmed a redundant constraint",),
    )
    pre = preflight(board)
    assert [i.code for i in pre.issues] == ["solver-warnings"]
    note = pre.issues[0]
    assert note.severity == OrderIssueSeverity.NOTE
    assert "CP-SAT trimmed a redundant constraint" in note.detail
    assert pre.orderable is True


# ------------------------------------------------- preflight: sorting and dedup


def test_issues_are_sorted_blocker_then_warning_then_note():
    board = _board(
        [],
        width_mm=0.0,
        height_mm=0.0,
        solver_status=PackStatus.FALLBACK.value,
        warnings=("placer ran out of budget",),
    )
    pre = preflight(board)
    assert [i.code for i in pre.issues] == [
        "degenerate-outline",
        "no-parts",
        "fallback-placement",
        "tiny-board",
        "solver-warnings",
    ]
    assert [str(i.severity) for i in pre.issues] == [
        "blocker",
        "blocker",
        "warning",
        "warning",
        "note",
    ]


def test_finalise_sorts_by_severity_then_code():
    # preflight()'s own checks each fire at most one issue per code, so there
    # is no black-box way to observe _finalise's sort/dedup through
    # preflight() alone; it is exercised directly here instead.
    out = _finalise(
        [
            _issue("zz", OrderIssueSeverity.NOTE),
            _issue("bb", OrderIssueSeverity.BLOCKER),
            _issue("mm", OrderIssueSeverity.WARNING),
            _issue("aa", OrderIssueSeverity.BLOCKER),
        ]
    )
    assert [i.code for i in out] == ["aa", "bb", "mm", "zz"]


def test_finalise_deduplicates_same_code_and_parts_keeping_the_first():
    first = _issue("x", OrderIssueSeverity.NOTE, title="first")
    second = _issue("x", OrderIssueSeverity.NOTE, title="second")
    out = _finalise([first, second])
    assert len(out) == 1
    assert out[0].title == "first", "the first occurrence should win"


# ---------------------------------------------------------------- OrderPreflight


def test_orderpreflight_blockers_and_orderable_are_derived():
    blocker = _issue("b", OrderIssueSeverity.BLOCKER)
    warning = _issue("w", OrderIssueSeverity.WARNING)
    note = _issue("n", OrderIssueSeverity.NOTE)

    mixed = OrderPreflight(issues=(blocker, warning, note))
    assert mixed.blockers == (blocker,)
    assert mixed.orderable is False

    clean = OrderPreflight(issues=(warning, note))
    assert clean.blockers == ()
    assert clean.orderable is True


def test_a_clean_board_is_orderable():
    """Pin the exact shape of a board this module calls orderable: adequately
    sized, every net touching only one pad, a solved placement status, and
    no solver warnings.
    """
    parts = [_placed(f"R{i}", f"NET{i}", x_nm=mm(3 * i)) for i in range(4)]
    board = _board(
        parts, width_mm=20.0, height_mm=20.0, solver_status=PackStatus.OPTIMAL.value
    )
    pre = preflight(board)
    assert pre.issues == ()
    assert pre.orderable is True


# ---------------------------------------------------------------- board_summary


def test_board_summary_reports_mm_and_parts_by_side():
    parts = [
        _placed("R1", "NETA", layer=Layer.TOP),
        _placed("R2", "NETB", layer=Layer.BOTTOM),
        _placed("R3", "NETC", layer=Layer.TOP),
    ]
    board = _board(parts, width_mm=15.0, height_mm=12.0)
    summary = board_summary(board, OrderOptions())

    assert json.loads(json.dumps(summary)) == summary
    assert summary["width_mm"] == pytest.approx(15.0)
    assert summary["height_mm"] == pytest.approx(12.0)
    assert summary["part_count"] == 3
    assert summary["parts_by_side"] == {"top": 2, "bottom": 1}
    assert summary["net_count"] == 3
    assert summary["nets_with_two_or_more_pads"] == 0


def test_board_summary_panel_maths_scales_with_columns_and_rows():
    board = _board([_placed("R1", "NET1")], width_mm=10.0, height_mm=8.0)
    summary = board_summary(board, OrderOptions(panel_columns=3, panel_rows=2))
    assert summary["boards_per_panel"] == 6
    assert summary["panel_width_mm_no_gaps_or_rails"] == pytest.approx(30.0)
    assert summary["panel_height_mm_no_gaps_or_rails"] == pytest.approx(16.0)


# ---------------------------------------------------------------- order_manifest


def test_order_manifest_round_trips_and_reflects_options():
    board = _board([_placed("R1", "NET1")], width_mm=20.0, height_mm=20.0)
    options = OrderOptions(quantity=10, layers=4, panel_columns=2, panel_rows=3)
    pre = preflight(board)
    manifest = order_manifest(board, options, pre)

    assert json.loads(json.dumps(manifest)) == manifest
    assert manifest["requires_human_approval"] is True
    assert "Nothing has been purchased" in manifest["disclaimer"]
    assert manifest["orderable"] == pre.orderable
    assert manifest["options"]["quantity"] == 10
    assert manifest["options"]["layers"] == 4
    assert manifest["options"]["panel_columns"] == 2
    assert manifest["options"]["panel_rows"] == 3
    assert manifest["options"]["surface_finish"] == "lead_free_hasl"


def test_order_manifest_is_not_orderable_when_blockers_exist():
    board = _board([], width_mm=20.0, height_mm=20.0)
    pre = preflight(board)
    assert pre.blockers  # sanity: this board really is blocked

    manifest = order_manifest(board, OrderOptions(), pre)
    assert manifest["orderable"] is False
    assert manifest["blocker_count"] == len(pre.blockers)
    assert manifest["requires_human_approval"] is True
    assert {i["code"] for i in manifest["issues"]} == {i.code for i in pre.issues}


# ---------------------------------------------------------------- package_zip


def test_package_zip_round_trips_and_contains_all_files():
    manifest = {"generator": "silkscreen", "orderable": True}
    files = [("gerbers/F_Cu.gbr", "gerber data"), ("bom.csv", "ref,val\nR1,10k\n")]
    data = package_zip(files, manifest)

    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert set(archive.namelist()) == {
            "gerbers/F_Cu.gbr",
            "bom.csv",
            "order-manifest.json",
        }
        assert archive.read("gerbers/F_Cu.gbr").decode() == "gerber data"
        assert archive.read("bom.csv").decode() == "ref,val\nR1,10k\n"
        assert json.loads(archive.read("order-manifest.json").decode()) == manifest
        for info in archive.infolist():
            assert info.date_time == (1980, 1, 1, 0, 0, 0)


def test_package_zip_is_byte_identical_across_calls():
    manifest = {"a": 1, "b": [1, 2, 3]}
    files = [("readme.txt", "hello")]
    first = package_zip(files, manifest)
    second = package_zip(files, manifest)
    assert hashlib.sha256(first).hexdigest() == hashlib.sha256(second).hexdigest()
    assert first == second


def test_package_zip_rejects_duplicate_supplied_filenames():
    with pytest.raises(ValueError, match="duplicate"):
        package_zip([("x.txt", "1"), ("x.txt", "2")], {"a": 1})


def test_package_zip_rejects_a_supplied_file_named_like_the_manifest():
    with pytest.raises(ValueError, match="duplicate"):
        package_zip([(MANIFEST_FILENAME, "not the real manifest")], {"a": 1})
