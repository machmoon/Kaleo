"""The bill of materials, as a contract between a model and the order step.

While the engineer reviews the placed board in KiCad, the engine looks the
parts up in the background: a manufacturer and part number per row, a
datasheet URL, and the KiCad-library 3D model :mod:`models3d` maps to the
land pattern. This module is the deterministic half of that -- the rows, the
validation of what a model answers, and the CSV -- with no model and no
network in it; :mod:`silkscreen.agents.sourcing` owns both.

Two honesty rules shape the vocabulary. An MPN starts as a **proposal**
and becomes ``"verified"`` only when a distributor
(:mod:`silkscreen.agents.distributor`, Mouser behind ``MOUSER_API_KEY``)
lists a part by exactly that number -- which says nothing about stock or
about the package on the board; without a key, or when the lookup could
not answer, ``mpn_status`` stays ``"proposed"`` and ``verify_error`` says
why in words. A datasheet is **verified** only when a probe saw ``%PDF-``
at the URL; a URL nobody probed is ``"none"`` when none was proposed and
``"unprobed"`` when the probe budget ran out first, and one that answered
with an HTML viewer page (distributors serve those from ``.pdf`` links) is
``"not_pdf"``. A model's null is worth more than an invented part number,
and the prompt says so.

Two CSVs come out of the same rows: :func:`bom_csv` is the per-designator
status sheet the UIs read, and :func:`grouped_bom_csv` is the sheet an
assembler ingests -- one line per orderable part with a quantity and the
designators joined.

:func:`parse_sourcing_response` follows the :mod:`netlist` convention:
every failure in a model's answer is collected into one
:class:`SourcingValidationError`, so the whole batch goes back as a single
repair prompt.
"""

from __future__ import annotations

import csv
import dataclasses
import io
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from .board import BoardResult
from .models3d import model_for, why_unmatched

__all__ = [
    "KINDS",
    "MPN_STATUSES",
    "DATASHEET_STATUSES",
    "SourcingEntry",
    "SourcingResult",
    "SourcingValidationError",
    "bom_rows",
    "parse_sourcing_response",
    "bom_csv",
    "grouped_bom_csv",
    "kind_for_ref",
]

#: Reference-designator prefix -> ``kind``. Anything else is a ``device``.
_PREFIX_KINDS: dict[str, str] = {
    "R": "resistor",
    "C": "capacitor",
    "L": "inductor",
    "D": "diode",
    "Y": "crystal",
    # ``netlist.KINDS`` gained ``switch`` and ``testpoint`` with the reference
    # prefixes SW and TP, and its own comment gives *this table* as the reason
    # they are not sub-cases of ``connector`` ("sourcing.bom_rows, which reads
    # the ref prefix"). Without these two rows that reason was unmet and both
    # landed on ``device``, so a button and a bare copper pad read on the BOM
    # as the same kind of thing as a microcontroller.
    "SW": "switch",
    # A test point stays *on* the BOM rather than being dropped. KiCad marks
    # its own TestPoint_Pad footprint ``exclude_from_bom`` because there is
    # nothing to buy, and that is right about purchasing and wrong about this
    # file: ``bom_rows`` is also the order step's manifest of what is on the
    # board, and a part that vanishes from it is the silent drop this package
    # refuses everywhere else. The honest answer is a row whose kind says what
    # it is and whose MPN is null.
    "TP": "testpoint",
}

#: The frozen ``kind`` vocabulary.
KINDS: frozenset[str] = frozenset({*_PREFIX_KINDS.values(), "device"})

#: ``"proposed"`` -- the model named one; ``"verified"`` -- a distributor
#: lists a part by that exact number; ``"none"`` -- the model said null, or
#: no model ran.
MPN_STATUSES: frozenset[str] = frozenset({"verified", "proposed", "none"})

#: ``"verified"`` -- a probe saw ``%PDF-``; ``"not_pdf"`` -- reachable, not a
#: PDF; ``"unreachable"`` -- the probe failed (HTTP error, timeout, a URL the
#: SSRF guard rejected); ``"unprobed"`` -- a URL was proposed but the probe
#: budget ran out before it was asked; ``"none"`` -- no URL was proposed.
DATASHEET_STATUSES: frozenset[str] = frozenset(
    {"verified", "not_pdf", "unreachable", "unprobed", "none"}
)

#: The longest part number accepted from a model. Real MPNs top out around
#: 30 characters; anything longer is prose that leaked into the field.
MAX_MPN_CHARS = 64

#: The longest note passed on from the model. It is shown, not parsed.
MAX_NOTE_CHARS = 200

#: The CSV column order. Frozen: the order zip and the overlay read it.
CSV_HEADER: tuple[str, ...] = (
    "ref",
    "value",
    "kind",
    "package",
    "manufacturer",
    "mpn",
    "mpn_status",
    "datasheet_url",
    "datasheet_status",
    "model3d",
)

#: The grouped BOM's columns: one line per orderable part. ``dnp`` is
#: always empty for now -- the circuit IR has no do-not-populate concept
#: yet -- but the column is there because every assembler template has it
#: and a buyer fills it by hand.
GROUPED_CSV_HEADER: tuple[str, ...] = (
    "designators",
    "qty",
    "value",
    "package",
    "manufacturer",
    "mpn",
    "mpn_status",
    "distributor_sku",
    "dnp",
)

_REF_PREFIX = re.compile(r"^[A-Za-z]*")
_OPTIONAL_FIELDS = ("manufacturer", "mpn", "datasheet_url", "note")


class SourcingValidationError(ValueError):
    """A model's sourcing answer is not usable.

    ``errors`` holds one message per problem so the whole batch can go back
    to the model in a single repair prompt (the :mod:`netlist` convention).
    """

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__(
            f"{len(errors)} problem(s) in sourcing response:\n  - "
            + "\n  - ".join(errors)
        )


@dataclass(frozen=True)
class SourcingEntry:
    """One BOM row: what the board carries, and what was found for it."""

    ref: str
    value: str
    kind: str
    #: :attr:`~silkscreen.footprints.Footprint.name` of the placed part.
    package: str
    manufacturer: str | None = None
    mpn: str | None = None
    mpn_status: str = "none"
    datasheet_url: str | None = None
    datasheet_status: str = "none"
    #: The distributor that confirmed ``mpn`` (``"Mouser"``), its own stock
    #: number and product page. Set only when ``mpn_status`` is
    #: ``"verified"``.
    distributor: str | None = None
    distributor_sku: str | None = None
    distributor_url: str | None = None
    #: Why a proposed MPN is not ``"verified"``, in words -- the distributor
    #: does not list it, or the lookup could not answer. Never set on a
    #: verified row, never set when no distributor was configured to ask.
    verify_error: str | None = None
    #: :attr:`~silkscreen.models3d.Model3D.path`, or None.
    model3d: str | None = None
    #: Why ``model3d`` is None, from :func:`~silkscreen.models3d.why_unmatched`.
    model3d_note: str | None = None
    #: Anything the model said worth passing on. Bounded, never parsed.
    note: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """Every field, JSON-safe."""
        return dataclasses.asdict(self)


@dataclass
class SourcingResult:
    parts: list[SourcingEntry]
    warnings: list[str] = field(default_factory=list)

    @property
    def verified(self) -> int:
        """Rows whose datasheet a probe actually saw as a PDF."""
        return sum(1 for p in self.parts if p.datasheet_status == "verified")

    @property
    def proposed(self) -> int:
        """Rows the model named a part number for, confirmed or not."""
        return sum(1 for p in self.parts if p.mpn_status in ("proposed", "verified"))

    @property
    def confirmed(self) -> int:
        """Rows whose part number a distributor lists."""
        return sum(1 for p in self.parts if p.mpn_status == "verified")

    @property
    def unresolved(self) -> int:
        """Rows with no part number at all."""
        return sum(1 for p in self.parts if p.mpn_status == "none")

    def as_dict(self) -> dict[str, Any]:
        return {
            "parts": [p.as_dict() for p in self.parts],
            "verified": self.verified,
            "proposed": self.proposed,
            "confirmed": self.confirmed,
            "unresolved": self.unresolved,
            "warnings": list(self.warnings),
        }


def kind_for_ref(ref: str) -> str:
    """The ``kind`` a reference designator implies: R/C/L/D/Y, else device."""
    prefix = _REF_PREFIX.match(ref).group(0).upper()
    return _PREFIX_KINDS.get(prefix, "device")


def bom_rows(board: BoardResult) -> list[SourcingEntry]:
    """One unsourced row per placed part, in board order.

    Everything a row carries here is known from the board alone: the kind
    from the ref prefix, and the 3D model from :mod:`models3d`, with the
    reason whenever the library has no honest match. Statuses are ``"none"``
    because nothing has been looked up yet -- these are the rows the order
    step falls back to when no model answers.
    """
    rows: list[SourcingEntry] = []
    for part in board.parts:
        name = part.footprint.name
        model = model_for(name, part.ref)
        rows.append(
            SourcingEntry(
                ref=part.ref,
                value=part.value,
                kind=kind_for_ref(part.ref),
                package=name,
                model3d=model.path if model is not None else None,
                model3d_note=(
                    None if model is not None else why_unmatched(name, part.ref)
                ),
            )
        )
    return rows


def _clean(value: Any, *, limit: int | None = None) -> str | None:
    """A stripped string, or None for null and for the empty string.

    An empty answer is the same statement as null -- the model had nothing
    -- and must not become a proposed part number of zero characters.
    """
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    return text[:limit] if limit is not None else text


def parse_sourcing_response(
    raw: str, expected_refs: Sequence[str]
) -> dict[str, dict[str, str | None]]:
    """Validate a model's sourcing answer against the rows that were asked.

    The shape is ``{"parts": [{"ref", "manufacturer", "mpn", "datasheet_url",
    "note"}]}`` with nulls allowed anywhere but ``ref``. The answer must cover
    every expected ref exactly once and name nothing else; text fields must
    be strings or null; a datasheet URL must be http(s); an MPN longer than
    :data:`MAX_MPN_CHARS` is prose, not a part number. Every failure is
    collected and raised together as :class:`SourcingValidationError`,
    unparseable JSON included, so one repair round can fix all of them.

    Returns ``ref -> {"manufacturer", "mpn", "datasheet_url", "note"}`` with
    every field present (None where the model said null or nothing), the
    note bounded to :data:`MAX_NOTE_CHARS`.
    """
    # Imported here, not at module level: ``silkscreen.agents`` imports the
    # stages, the stages import the sourcing agent, and that imports this
    # module -- a top-level import would close that circle on whichever side
    # was imported first.
    from .agents.model import ModelError, parse_json

    errors: list[str] = []
    try:
        data = parse_json(raw)
    except ModelError as exc:
        raise SourcingValidationError([f"response was not JSON: {exc}"]) from exc

    if not isinstance(data, dict):
        raise SourcingValidationError(
            [f"top level must be a JSON object, got {type(data).__name__}"]
        )
    parts = data.get("parts")
    if not isinstance(parts, list):
        raise SourcingValidationError(
            ['"parts" must be a list of objects, one per ref']
        )

    expected = list(expected_refs)
    wanted = set(expected)
    seen: set[str] = set()
    found: dict[str, dict[str, str | None]] = {}
    for index, item in enumerate(parts):
        where = f"parts[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{where} must be an object")
            continue
        ref = item.get("ref")
        if not isinstance(ref, str) or not ref.strip():
            errors.append(f'{where} needs a "ref" string')
            continue
        ref = ref.strip()
        if ref not in wanted:
            errors.append(f"{where}: {ref!r} is not a part on this board")
            continue
        if ref in seen:
            errors.append(f"{where}: {ref!r} appears more than once")
            continue
        seen.add(ref)

        entry: dict[str, str | None] = {}
        for name in _OPTIONAL_FIELDS:
            value = item.get(name)
            if value is not None and not isinstance(value, str):
                errors.append(
                    f'{ref}: "{name}" must be a string or null, '
                    f"got {type(value).__name__}"
                )
                entry[name] = None
                continue
            entry[name] = _clean(
                value, limit=MAX_NOTE_CHARS if name == "note" else None
            )

        mpn = entry["mpn"]
        if mpn is not None and len(mpn) > MAX_MPN_CHARS:
            errors.append(
                f"{ref}: mpn is {len(mpn)} characters, longer than "
                f"{MAX_MPN_CHARS}; a part number, not a description"
            )
        url = entry["datasheet_url"]
        if url is not None and urlsplit(url).scheme.lower() not in ("http", "https"):
            errors.append(f"{ref}: datasheet_url {url!r} is not an http(s) URL")
        found[ref] = entry

    for ref in expected:
        if ref not in seen:
            errors.append(
                f"{ref}: missing from the response (use null fields if unknown)"
            )

    if errors:
        raise SourcingValidationError(errors)
    return found


def bom_csv(result: SourcingResult) -> str:
    """The BOM as CSV: :data:`CSV_HEADER`, one row per part, ``\\n`` line ends.

    Deterministic for a given result -- the same rows produce the same bytes
    -- and None is written as the empty cell, never as the text ``None``.
    """
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(CSV_HEADER)
    for entry in result.parts:
        row = entry.as_dict()
        writer.writerow(["" if row[c] is None else row[c] for c in CSV_HEADER])
    return out.getvalue()


def _group_key(entry: SourcingEntry) -> tuple[str, str, str, str]:
    return (
        entry.value,
        entry.package,
        entry.manufacturer or "",
        entry.mpn or "",
    )


def grouped_bom_csv(result: SourcingResult) -> str:
    """The assembler's BOM: :data:`GROUPED_CSV_HEADER`, one line per part.

    Rows sharing value, package, manufacturer and MPN collapse into one line
    with ``qty`` and their designators joined by a space, in board order of
    first appearance -- what a fab's BOM importer expects (``R1 R2``, ``2``)
    rather than the per-designator sheet :func:`bom_csv` writes. Two rows
    with the same value and package but no MPN still group: the buyer picks
    one part for both. The group's ``mpn_status`` is ``"verified"`` only
    when every row in it is, ``"proposed"`` when any row names a part, else
    ``"none"``; the SKU is the first confirmed row's. Deterministic, ``\\n``
    line ends, None as the empty cell.
    """
    groups: dict[tuple[str, str, str, str], list[SourcingEntry]] = {}
    for entry in result.parts:
        groups.setdefault(_group_key(entry), []).append(entry)

    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(GROUPED_CSV_HEADER)
    for (value, package, manufacturer, mpn), members in groups.items():
        statuses = {m.mpn_status for m in members}
        if statuses == {"verified"}:
            status = "verified"
        elif statuses & {"verified", "proposed"}:
            status = "proposed"
        else:
            status = "none"
        sku = next((m.distributor_sku for m in members if m.distributor_sku), "")
        writer.writerow(
            [
                " ".join(m.ref for m in members),
                len(members),
                value,
                package,
                manufacturer,
                mpn,
                status,
                sku,
                "",
            ]
        )
    return out.getvalue()
