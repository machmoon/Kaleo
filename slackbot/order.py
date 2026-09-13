"""Prepare a fabrication order, and stop.

This module builds an order *draft*: the board's dimensions and stackup, the
quantity, the files a fabricator would need, and an explicit list of what is
still missing or unresolved. It then stops. There is deliberately no vendor
API call, no cart, no payment path, and no submission -- not disabled, not
behind a flag, absent. A human reads the draft and places the order.

That boundary is a design decision rather than an unfinished edge. Spending
money and committing a physical artefact to fabrication are the two actions in
this product a person must take themselves, and a Slack message from a bot is
not a purchase authorisation. The draft exists to make the human's decision
well-informed and fast; it does not exist to make it unnecessary.

The readiness check is the other half of that. A board with an unresolved
blocker in its review is reported as *not* ready, with the blockers named, so
"the bot prepared an order" is never mistaken for "the design passed".
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine"))

from silkscreen.agents.review import Finding, Severity  # noqa: E402

from .blocks import MAX_LISTED_FINDINGS, context, divider, escape_mrkdwn, section

__all__ = [
    "OrderDraft",
    "prepare_order",
    "draft_blocks",
    "draft_json",
    "DEFAULT_STACKUP",
    "NOT_SUBMITTED",
]

#: Repeated verbatim in the Slack message, the JSON, and the README. Someone
#: skimming any one of the three must not be able to conclude otherwise.
NOT_SUBMITTED = (
    "This is a draft only. No order has been placed, no vendor has been "
    "contacted, and no payment method exists in this system. A human must "
    "review this and order the boards themselves."
)

#: A conservative two-layer prototype stackup. Every value here is a default
#: that a human is expected to confirm, not a derived fact about the design --
#: the pipeline does not yet model copper weight, impedance, or stackup.
DEFAULT_STACKUP: dict[str, Any] = {
    "layers": 2,
    "thickness_mm": 1.6,
    "copper_weight_oz": 1,
    "surface_finish": "HASL (lead-free)",
    "soldermask": "green",
    "silkscreen": "white",
    "min_trace_mm": 0.15,
    "min_space_mm": 0.15,
    "min_drill_mm": 0.3,
}


@dataclass(frozen=True)
class OrderDraft:
    """A fabrication order a human could place, and the reasons not to yet."""

    intent: str
    quantity: int
    width_mm: float
    height_mm: float
    part_count: int
    net_count: int
    stackup: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_STACKUP))
    #: Files this run produced that a fabricator or an engineer would want.
    artifacts: list[str] = field(default_factory=list)
    #: Hard reasons not to order: unresolved blockers from the review.
    blockers: list[str] = field(default_factory=list)
    #: Soft ones: what a fabricator will ask for that we cannot supply yet.
    gaps: list[str] = field(default_factory=list)

    @property
    def area_mm2(self) -> float:
        return round(self.width_mm * self.height_mm, 2)

    @property
    def total_area_mm2(self) -> float:
        return round(self.area_mm2 * self.quantity, 2)

    @property
    def ready(self) -> bool:
        """Ready means: nothing in the review blocks it and nothing is missing.

        Both lists must be empty. A draft with gaps is still worth reading --
        it tells the engineer exactly what to produce next -- but calling it
        ready would be a lie a fabricator would discover, not us.
        """
        return not self.blockers and not self.gaps


def _missing_artifacts(artifacts: list[str]) -> list[str]:
    """What a fabricator needs that this pipeline does not yet emit.

    Named individually rather than as "not implemented", because the gap
    between a placed board and a manufacturable one is the interesting part of
    the remaining work, and a vague message hides it.
    """
    gaps: list[str] = []
    names = " ".join(artifacts).lower()
    if not any(name.endswith(".kicad_pcb") for name in artifacts):
        gaps.append("No board file was produced for this run.")
    if ".zip" not in names and "gerber" not in names:
        gaps.append(
            "Gerber and drill files (RS-274X + Excellon) — not generated; "
            "export them from KiCad, or run the board through a plot step."
        )
    if "bom" not in names:
        gaps.append(
            "Bill of materials with orderable part numbers — the pipeline "
            "designs against datasheets, not against a distributor catalogue."
        )
    if "pos" not in names and "centroid" not in names:
        gaps.append(
            "Pick-and-place / centroid file — only needed if you are ordering "
            "assembly as well as bare boards."
        )
    gaps.append(
        "Routing: the placement is solved, but copper routing is not part of "
        "this draft. A bare board cannot be fabricated from placement alone."
    )
    return gaps


def prepare_order(
    result: Any,
    *,
    quantity: int = 5,
    artifacts: list[str] | None = None,
    stackup: dict[str, Any] | None = None,
) -> OrderDraft:
    """Build an :class:`OrderDraft` from a finished pipeline run."""
    if quantity < 1:
        raise ValueError("quantity must be at least 1")

    board = result.board
    width, height = board.size_mm
    files = list(artifacts or [])

    findings: list[Finding] = list(getattr(result, "findings", ()) or ())
    blockers = [
        f"{f.title}" + (f" [{', '.join(f.parts)}]" if f.parts else "")
        for f in findings
        if f.severity is Severity.BLOCKER
    ]

    merged = dict(DEFAULT_STACKUP)
    merged.update(stackup or {})

    gaps = _missing_artifacts(files)
    # An empty blocker list is only evidence of a clean board when the critic
    # actually delivered a verdict. When it did not, the absence of blockers
    # is an absence of information, and this draft must say so rather than let
    # "0 blocking findings" read as "nothing wrong with it".
    review = getattr(result, "review", None)
    if review is not None and not review.ok:
        gaps.insert(
            0,
            "A design review with a readable verdict — "
            f"{review.note()}, so the empty blocker list below is not "
            "evidence that there is nothing blocking.",
        )

    return OrderDraft(
        intent=result.intent,
        quantity=quantity,
        width_mm=round(width, 3),
        height_mm=round(height, 3),
        part_count=result.spec.part_count(),
        net_count=result.spec.net_count(),
        stackup=merged,
        artifacts=files,
        blockers=blockers,
        gaps=gaps,
    )


def draft_json(draft: OrderDraft) -> dict[str, Any]:
    """The draft as a vendor-neutral document.

    ``submitted`` and ``payment`` are present and false rather than omitted:
    an explicit no is harder to misread downstream than a missing key.
    """
    return {
        "kind": "silkscreen.order_draft",
        "version": 1,
        "submitted": False,
        "payment": None,
        "notice": NOT_SUBMITTED,
        "ready_to_order": draft.ready,
        "intent": draft.intent,
        "quantity": draft.quantity,
        "board": {
            "width_mm": draft.width_mm,
            "height_mm": draft.height_mm,
            "area_mm2": draft.area_mm2,
            "total_area_mm2": draft.total_area_mm2,
            "part_count": draft.part_count,
            "net_count": draft.net_count,
        },
        "stackup": dict(draft.stackup),
        "artifacts": list(draft.artifacts),
        "blocking_findings": list(draft.blockers),
        "missing_for_fabrication": list(draft.gaps),
    }


def draft_blocks(draft: OrderDraft) -> list[dict[str, Any]]:
    """The draft as a Slack message a hardware engineer can sign off on."""
    stack = draft.stackup
    header = (
        ":package: *Fabrication order — draft*\n"
        f">{escape_mrkdwn(draft.intent[:400])}"
    )
    spec = (
        f"*{draft.quantity}* boards · *{draft.width_mm:.2f} × {draft.height_mm:.2f} mm*"
        f" · {stack['layers']} layer · {stack['thickness_mm']} mm · "
        f"{escape_mrkdwn(str(stack['surface_finish']))}\n"
        f"{draft.part_count} parts · {draft.net_count} nets · "
        f"{draft.total_area_mm2:.0f} mm² total area"
    )
    blocks = [section(header), section(spec)]

    if draft.artifacts:
        listed = "\n".join(f"• `{escape_mrkdwn(a)}`" for a in draft.artifacts[:10])
        blocks.append(section(f"*Files from this run*\n{listed}"))

    if draft.blockers:
        listed = "\n".join(
            f"• {escape_mrkdwn(b)}" for b in draft.blockers[:MAX_LISTED_FINDINGS]
        )
        blocks.append(
            section(
                f":red_circle: *Not ready — {len(draft.blockers)} blocking "
                f"finding(s) from the review*\n{listed}"
            )
        )

    if draft.gaps:
        listed = "\n".join(f"• {escape_mrkdwn(g)}" for g in draft.gaps[:8])
        blocks.append(section(f"*Still needed before a fab can build this*\n{listed}"))

    blocks.append(divider())
    blocks.append(section(f":lock: {NOT_SUBMITTED}"))
    blocks.append(
        context(
            "Ordering is deliberately manual: silkscreen prepares the order and "
            "a person places it."
        )
    )
    return blocks
