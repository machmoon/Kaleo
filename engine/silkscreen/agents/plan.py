"""Decide what is being built before proposing how to build it.

A user intent is eleven words -- "a home security camera system" -- and
:func:`~silkscreen.agents.propose.propose_circuit` turns it straight into a
netlist. Nothing in that path ever decides how the board is *powered*, what
rails it needs, what blocks it is made of, or what plugs into it. The result
is a confident circuit with no power entry: the model draws a 3.3 V rail out
of thin air, no connector, no battery, and the case cuts no hole because
there is nothing to cut a hole for.

This module is the stage that runs first. It is the
:mod:`silkscreen.agents.sourcing` shape applied to planning: the model is
shown the intent and the vocabulary the builder can actually draw, and asked
for ONE JSON object -- what is being built, where the power comes in, which
rails to generate, which functional blocks, and what external connectivity.
The answer goes through :func:`parse_plan_response`, which follows
:mod:`silkscreen.netlist` throughout: **every** failure is collected into one
:class:`PlanValidationError` so the whole batch goes back as a single repair
prompt. After ``max_repairs`` rounds the loop gives up **loudly** -- a
:class:`PlanResult` with ``plan=None`` and a warning naming the failure. An
unplanned run is honest; a half-parsed plan is a brief the rest of the
pipeline would design against.

Two rules make this worth running at all:

* **The plan may only ask for parts the builder can draw.** The prompt
  carries :func:`silkscreen.board.supported_packages_text` (the
  :mod:`~silkscreen.agents.propose` rule 7, applied one stage earlier) and
  the connector/battery package names from :mod:`silkscreen.footprints`, and
  the parser rejects a package outside that vocabulary. A plan that asks for
  a barrel jack the engine cannot lay out does not make the run better
  informed -- it makes it *more confidently wrong*, because propose then
  designs against a promise the board stage silently breaks.
* **The plan may say it does not know.** ``power.source`` has an explicit
  ``"unknown"`` value, and choosing it *requires* an entry in
  ``assumptions`` saying what was assumed and why. An invented certainty --
  "USB-C, 5 V" on an intent that never mentioned a cable -- is worse than a
  stated assumption, because only one of the two is visible to the engineer
  who has to correct it.

Intended live tier: the reasoning tier rather than
:data:`~silkscreen.agents.model.CHEAP_MODEL` -- deciding whether a security
camera runs off a wall adapter or a battery is a judgement pass, not recall.
The tier is the caller's to construct; this module only ever sees the
:class:`Model` protocol.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..board import supported_packages_text
from .model import Model

__all__ = [
    "BLOCK_MAX",
    "CONNECTIVITY_MAX",
    "PLAN_MARKER",
    "POWER_SOURCES",
    "RAILS_MAX",
    "BoardPlan",
    "Connectivity",
    "FunctionalBlock",
    "PackageVocabulary",
    "PlanResult",
    "PlanValidationError",
    "PowerEntry",
    "Rail",
    "package_vocabulary",
    "parse_plan_response",
    "plan_prompt",
    "propose_plan",
]

#: Frozen (the connector contract): appears verbatim in every prompt so any
#: ``ScriptedModel.by_marker`` can key on it.
PLAN_MARKER = "BOARD-PLAN v1"

#: How power gets onto the board. Frozen vocabulary, and ``"unknown"`` is a
#: first-class member: an intent that does not say how the thing is powered
#: must be answerable without inventing a cable. Every other value names a
#: physical thing the board has to carry, so each one implies a connector or
#: a holder the builder must be able to draw.
POWER_SOURCES = (
    "usb",
    "barrel_jack",
    "terminal_block",
    "pin_header",
    "battery",
    "unknown",
)

#: Bounds. A plan longer than this is a specification document, and the
#: point of the stage is a brief the propose prompt can carry whole.
LINE_MAX_CHARS = 200
BUILDING_MAX_CHARS = 200
RAILS_MIN = 1
RAILS_MAX = 6
BLOCK_MIN = 1
BLOCK_MAX = 10
CONNECTIVITY_MAX = 8
ASSUMPTIONS_MAX = 8

#: How much of the batched error list a give-up warning carries.
_WARNING_CHARS = 400


class PlanValidationError(ValueError):
    """A model's plan is not usable.

    ``errors`` holds one message per problem so the whole batch can go back
    to the model in a single repair prompt (the :mod:`~silkscreen.netlist`
    convention). Nothing here raises on the first failure.
    """

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__(
            f"{len(errors)} problem(s) in board plan:\n  - " + "\n  - ".join(errors)
        )


@dataclass(frozen=True)
class PackageVocabulary:
    """The connector and battery packages the builder can actually draw.

    Read from :mod:`silkscreen.footprints` at call time rather than frozen
    into a constant here, because this module must not be the second place
    those names live -- a plan validated against a stale copy would advertise
    a part the board stage refuses.

    ``known`` is false when :mod:`~silkscreen.footprints` does not export the
    tables (an older engine, or a checkout where that half has not landed).
    Membership is then not checked and the prompt says less, which is the
    honest degradation: refusing every package because the list could not be
    read would be a validation failure about *this module*, reported as if
    the model had erred.
    """

    connectors: tuple[str, ...] = ()
    batteries: tuple[str, ...] = ()
    known: bool = True


def package_vocabulary() -> PackageVocabulary:
    """The connector/battery names :mod:`silkscreen.footprints` advertises."""
    try:
        from ..footprints import BATTERY_PACKAGES, CONNECTOR_PACKAGES
    except ImportError:
        return PackageVocabulary(known=False)
    return PackageVocabulary(
        connectors=tuple(sorted(CONNECTOR_PACKAGES)),
        batteries=tuple(sorted(BATTERY_PACKAGES)),
        known=True,
    )


@dataclass(frozen=True)
class PowerEntry:
    """How power gets onto the board -- the decision this stage exists for.

    ``connector_package`` and ``battery_package`` are
    :mod:`silkscreen.footprints` package names, so the propose stage can name
    the part and the board stage can lay it out. ``source == "unknown"``
    carries neither, and the plan's ``assumptions`` then say so out loud.
    """

    source: str
    input_voltage: str = ""
    connector_package: str | None = None
    battery_package: str | None = None
    #: One line on what the entry has to survive -- reverse polarity, a wall
    #: adapter's tolerance, a charger. Advisory; the propose stage reads it.
    notes: str = ""

    @property
    def decided(self) -> bool:
        """Did the plan actually choose a power entry?"""
        return self.source != "unknown"

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "input_voltage": self.input_voltage,
            "connector_package": self.connector_package,
            "battery_package": self.battery_package,
            "notes": self.notes,
            "decided": self.decided,
        }


@dataclass(frozen=True)
class Rail:
    """One supply rail the board has to generate. Ground is implied."""

    name: str
    voltage: str
    #: Which rail (or the input) this one is derived from, in the plan's own
    #: vocabulary. Empty when it comes straight off the power entry.
    derived_from: str = ""
    purpose: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "voltage": self.voltage,
            "derived_from": self.derived_from,
            "purpose": self.purpose,
        }


@dataclass(frozen=True)
class FunctionalBlock:
    """One thing the board does, named so propose can design it."""

    name: str
    purpose: str
    rail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "purpose": self.purpose, "rail": self.rail}


@dataclass(frozen=True)
class Connectivity:
    """Something external that plugs into the board.

    ``package`` is a :mod:`silkscreen.footprints` connector package, for the
    same reason :class:`PowerEntry` carries one: a plan asking for "an I2C
    header" that the builder cannot draw is worse than a plan that asked for
    nothing.
    """

    purpose: str
    package: str
    signals: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "purpose": self.purpose,
            "package": self.package,
            "signals": list(self.signals),
        }


@dataclass(frozen=True)
class BoardPlan:
    """A validated brief: what to build, powered how, out of what."""

    building: str
    power: PowerEntry
    rails: tuple[Rail, ...] = ()
    blocks: tuple[FunctionalBlock, ...] = ()
    connectivity: tuple[Connectivity, ...] = ()
    #: What the plan assumed because the intent did not say, and why.
    #: Required to be non-empty when the power source is ``"unknown"``.
    assumptions: tuple[str, ...] = ()
    open_questions: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "building": self.building,
            "power": self.power.as_dict(),
            "rails": [r.as_dict() for r in self.rails],
            "blocks": [b.as_dict() for b in self.blocks],
            "connectivity": [c.as_dict() for c in self.connectivity],
            "assumptions": list(self.assumptions),
            "open_questions": list(self.open_questions),
        }

    def packages(self) -> tuple[str, ...]:
        """Every footprint package the plan asked for, deduplicated.

        The board stage has to be able to draw all of these; a caller wiring
        this in can check that before spending the propose call.
        """
        names = [
            self.power.connector_package,
            self.power.battery_package,
            *(c.package for c in self.connectivity),
        ]
        return tuple(dict.fromkeys(n for n in names if n))

    def brief_text(self) -> str:
        """The plan as prompt text, for the stage that designs against it.

        Deterministic, so the same plan produces the same propose prompt --
        a plan rendered two ways is two different runs.
        """
        lines = [f"Building: {self.building}"]
        power = self.power
        if power.decided:
            entry = f"Power entry: {power.source}"
            if power.connector_package:
                entry += f" via {power.connector_package}"
            if power.battery_package:
                entry += f" with battery holder {power.battery_package}"
            if power.input_voltage:
                entry += f", input {power.input_voltage}"
            lines.append(entry)
        else:
            lines.append(
                "Power entry: NOT DECIDED -- the intent did not say. "
                "See the assumptions below."
            )
        if power.notes:
            lines.append(f"  {power.notes}")
        if self.rails:
            lines.append("Rails to generate:")
            for rail in self.rails:
                origin = f" from {rail.derived_from}" if rail.derived_from else ""
                why = f" -- {rail.purpose}" if rail.purpose else ""
                lines.append(f"  - {rail.name} ({rail.voltage}){origin}{why}")
        if self.blocks:
            lines.append("Functional blocks:")
            for block in self.blocks:
                rail = f" on {block.rail}" if block.rail else ""
                lines.append(f"  - {block.name}{rail}: {block.purpose}")
        if self.connectivity:
            lines.append("External connectivity:")
            for conn in self.connectivity:
                signals = (
                    f" [{', '.join(conn.signals)}]" if conn.signals else ""
                )
                lines.append(f"  - {conn.package}: {conn.purpose}{signals}")
        if self.assumptions:
            lines.append("Assumptions made because the request did not say:")
            lines.extend(f"  - {a}" for a in self.assumptions)
        if self.open_questions:
            lines.append("Open questions:")
            lines.extend(f"  - {q}" for q in self.open_questions)
        return "\n".join(lines)


@dataclass
class PlanResult:
    """What the planning stage produced, and what went wrong.

    ``plan is None`` means the model never gave a usable answer and
    ``warnings`` says so; a caller that never ran the stage has no
    :class:`PlanResult` at all. Two failures that must not read alike: a run
    that planned nothing on purpose and a run whose plan was mangled.
    """

    plan: BoardPlan | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.plan is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "plan": None if self.plan is None else self.plan.as_dict(),
            "warnings": list(self.warnings),
        }


# ---------------------------------------------------------------- prompt


_PLAN_PROMPT_TEMPLATE = """\
You are a hardware engineer writing the one-page brief for a PCB before
anyone draws a schematic ({marker}). You are given a short request, often a
single sentence. Decide what is actually being built and, above all, decide
HOW IT IS POWERED. Respond with ONE JSON object -- no prose, no code fence:

{{
  "building": "<one line: what this board is>",
  "power": {{
    "source": "usb|barrel_jack|terminal_block|pin_header|battery|unknown",
    "input_voltage": "<what arrives at the connector, e.g. 5V or 3.0V>",
    "connector_package": "<connector package name>" | null,
    "battery_package": "<battery holder package name>" | null,
    "notes": "<one line: what the input has to survive>" | null
  }},
  "rails": [
    {{"name": "<net name, e.g. +3V3>", "voltage": "<e.g. 3.3V>",
     "derived_from": "<rail or input this comes from>" | null,
     "purpose": "<what runs on it>" | null}}
  ],
  "blocks": [
    {{"name": "<block>", "purpose": "<what it does>",
     "rail": "<which rail powers it>" | null}}
  ],
  "connectivity": [
    {{"purpose": "<what plugs in here>", "package": "<connector package name>",
     "signals": ["<signal names on the connector>"]}}
  ],
  "assumptions": ["<what you assumed because the request did not say, and why>"],
  "open_questions": ["<what a person still has to decide>"]
}}

Hard rules -- an answer breaking any of these is rejected automatically:

1. "power" is not optional and is the point of this brief. A board with no
   decided power entry cannot be built: something physical has to bring in
   volts, and it has to be a part on the board.
2. Every "connector_package", "battery_package" and connectivity "package"
   must be one of the names listed below, exactly. A part the builder cannot
   draw does not make the design better informed, it makes it wrong with
   more confidence. If nothing listed fits, pick the closest one that does
   and say so in "assumptions".
3. If the request genuinely does not say how the thing is powered, set
   "source" to "unknown" and put an entry in "assumptions" saying what you
   would assume and why. A stated assumption is worth more than an invented
   certainty. "unknown" with an empty "assumptions" list is rejected.
4. Between {rails_min} and {rails_max} rails, and between {block_min} and
   {block_max} blocks. Ground is implied -- do not list it as a rail. Rail
   names follow the board's convention: GND, +3V3, +5V, VIN.
5. At most {connectivity_max} connectivity entries and {assumptions_max}
   assumptions. This is a brief, not a specification.
6. The schematic stage can only lay out these IC packages, judged by a
   device's highest pin number: {packages}. Do not plan around a part that
   needs anything else.
7. Every line is one line. Nothing over {line_max} characters.

{vocabulary}
"""


def _vocabulary_block(vocab: PackageVocabulary) -> str:
    """The buildable connector and battery names, for the prompt."""
    if not vocab.known:
        # Said plainly rather than silently omitted: a prompt with no list
        # is a prompt the model will fill from its own imagination, and the
        # reader of a logged prompt deserves to know which case this was.
        return (
            "Connector and battery packages: this engine build does not "
            "advertise a list. Name no connector_package or battery_package "
            "you are not certain of; use \"unknown\" and an assumption."
        )
    connectors = "\n".join(f"  - {name}" for name in vocab.connectors)
    batteries = "\n".join(f"  - {name}" for name in vocab.batteries)
    return (
        "Connector packages the builder can draw (use these names exactly):\n"
        f"{connectors or '  (none)'}\n\n"
        "Battery holder packages the builder can draw:\n"
        f"{batteries or '  (none)'}"
    )


def plan_prompt(vocab: PackageVocabulary | None = None) -> str:
    """The planning prompt, carrying the vocabulary the builder honours.

    Built at call time rather than at import, because the connector tables
    are read from :mod:`silkscreen.footprints` and a prompt frozen at import
    would advertise whatever the list happened to be then.
    """
    if vocab is None:
        vocab = package_vocabulary()
    return _PLAN_PROMPT_TEMPLATE.format(
        marker=PLAN_MARKER,
        rails_min=RAILS_MIN,
        rails_max=RAILS_MAX,
        block_min=BLOCK_MIN,
        block_max=BLOCK_MAX,
        connectivity_max=CONNECTIVITY_MAX,
        assumptions_max=ASSUMPTIONS_MAX,
        line_max=LINE_MAX_CHARS,
        packages=supported_packages_text(),
        vocabulary=_vocabulary_block(vocab),
    )


# ---------------------------------------------------------------- parsing


def _strip_code_fence(text: str) -> str:
    """Remove a ``` fence if the model wrapped its JSON in one.

    Copied in spirit from :mod:`silkscreen.netlist`: a fenced answer is both
    likely and, unhandled, fatal.
    """
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _text(
    value: Any,
    where: str,
    errors: list[str],
    *,
    required: bool = True,
    limit: int = LINE_MAX_CHARS,
) -> str:
    """One string field, checked for type, emptiness and length.

    Each of those is its own message: a model that returned a number and one
    that returned an essay made different mistakes and need to be told which.
    """
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        errors.append(f"{where} must be a string, got {type(value).__name__}")
        return ""
    text = value.strip()
    if required and not text:
        errors.append(f"{where} is empty")
    if len(text) > limit:
        errors.append(f"{where} is {len(text)} characters, longer than {limit}")
    return text


def _string_list(
    value: Any, where: str, errors: list[str], *, limit: int
) -> tuple[str, ...]:
    """A list of short strings, or a batch of messages saying why not."""
    if value is None:
        return ()
    if not isinstance(value, list):
        errors.append(
            f"{where} must be a list of strings, got {type(value).__name__}"
        )
        return ()
    if len(value) > limit:
        errors.append(f"{where} has {len(value)} entries, more than {limit}")
    out: list[str] = []
    for index, entry in enumerate(value):
        text = _text(entry, f"{where}[{index}]", errors)
        if text:
            out.append(text)
    return tuple(dict.fromkeys(out))


def _package(
    value: Any,
    where: str,
    allowed: tuple[str, ...],
    known: bool,
    errors: list[str],
    *,
    what: str,
) -> str | None:
    """One package name, checked against what the builder can draw.

    Membership is only checked when the vocabulary could be read; see
    :class:`PackageVocabulary`. An unreadable list is this module's problem
    and must not be reported to the model as its mistake.
    """
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{where} must be a {what} package name or null")
        return None
    name = value.strip()
    if known and name not in allowed:
        errors.append(
            f"{where} is {name!r}, which the builder cannot draw; "
            f"allowed {what} packages: {list(allowed)}"
        )
        return None
    return name


def _parse_power(
    raw: Any, vocab: PackageVocabulary, errors: list[str]
) -> PowerEntry:
    if not isinstance(raw, dict):
        errors.append(
            f'"power" must be an object saying how the board is powered, '
            f"got {type(raw).__name__}"
        )
        return PowerEntry(source="unknown")

    source_raw = raw.get("source")
    source = "unknown"
    if not isinstance(source_raw, str) or source_raw.strip() not in POWER_SOURCES:
        errors.append(
            f"power.source is {source_raw!r}; allowed: {list(POWER_SOURCES)}"
        )
    else:
        source = source_raw.strip()

    connector = _package(
        raw.get("connector_package"),
        "power.connector_package",
        vocab.connectors,
        vocab.known,
        errors,
        what="connector",
    )
    battery = _package(
        raw.get("battery_package"),
        "power.battery_package",
        vocab.batteries,
        vocab.known,
        errors,
        what="battery holder",
    )
    voltage = _text(
        raw.get("input_voltage"), "power.input_voltage", errors, required=False
    )
    notes = _text(raw.get("notes"), "power.notes", errors, required=False)

    # The one rule this stage exists for: a decided source must name the
    # physical thing that carries it, or the board has no power entry and
    # every stage downstream will happily pretend otherwise.
    if source == "battery":
        if battery is None:
            errors.append(
                'power.source is "battery" but power.battery_package is '
                "null; a battery needs a holder the builder can place"
            )
    elif source != "unknown" and connector is None:
        errors.append(
            f"power.source is {source!r} but power.connector_package is "
            f"null; power has to arrive through a part on the board"
        )
    if source != "unknown" and not voltage:
        errors.append(
            "power.input_voltage is empty; a decided power entry knows what "
            "arrives at it"
        )

    return PowerEntry(
        source=source,
        input_voltage=voltage,
        connector_package=connector,
        battery_package=battery,
        notes=notes,
    )


def _parse_rails(raw: Any, errors: list[str]) -> tuple[Rail, ...]:
    if not isinstance(raw, list):
        errors.append(
            f'"rails" must be a list of supply rails, got {type(raw).__name__}'
        )
        return ()
    if len(raw) < RAILS_MIN:
        errors.append(
            f"the plan has {len(raw)} rail(s); at least {RAILS_MIN} is "
            f"required (every board runs on something)"
        )
    if len(raw) > RAILS_MAX:
        errors.append(f"the plan has {len(raw)} rails, more than {RAILS_MAX}")
    rails: list[Rail] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw):
        where = f"rails[{index}]"
        if not isinstance(entry, dict):
            errors.append(f"{where} must be an object, got {type(entry).__name__}")
            continue
        name = _text(entry.get("name"), f"{where}.name", errors)
        if name and name in seen:
            errors.append(f"{where}.name is {name!r}, which is listed twice")
        elif name:
            seen.add(name)
        rails.append(
            Rail(
                name=name,
                voltage=_text(entry.get("voltage"), f"{where}.voltage", errors),
                derived_from=_text(
                    entry.get("derived_from"),
                    f"{where}.derived_from",
                    errors,
                    required=False,
                ),
                purpose=_text(
                    entry.get("purpose"), f"{where}.purpose", errors, required=False
                ),
            )
        )
    return tuple(rails)


def _parse_blocks(raw: Any, errors: list[str]) -> tuple[FunctionalBlock, ...]:
    if not isinstance(raw, list):
        errors.append(
            f'"blocks" must be a list of functional blocks, '
            f"got {type(raw).__name__}"
        )
        return ()
    if len(raw) < BLOCK_MIN:
        errors.append(
            f"the plan has {len(raw)} block(s); at least {BLOCK_MIN} is required"
        )
    if len(raw) > BLOCK_MAX:
        errors.append(f"the plan has {len(raw)} blocks, more than {BLOCK_MAX}")
    blocks: list[FunctionalBlock] = []
    for index, entry in enumerate(raw):
        where = f"blocks[{index}]"
        if not isinstance(entry, dict):
            errors.append(f"{where} must be an object, got {type(entry).__name__}")
            continue
        blocks.append(
            FunctionalBlock(
                name=_text(entry.get("name"), f"{where}.name", errors),
                purpose=_text(entry.get("purpose"), f"{where}.purpose", errors),
                rail=_text(
                    entry.get("rail"), f"{where}.rail", errors, required=False
                ),
            )
        )
    return tuple(blocks)


def _parse_connectivity(
    raw: Any, vocab: PackageVocabulary, errors: list[str]
) -> tuple[Connectivity, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        errors.append(
            f'"connectivity" must be a list of connectors, '
            f"got {type(raw).__name__}"
        )
        return ()
    if len(raw) > CONNECTIVITY_MAX:
        errors.append(
            f"connectivity has {len(raw)} entries, more than {CONNECTIVITY_MAX}"
        )
    out: list[Connectivity] = []
    for index, entry in enumerate(raw):
        where = f"connectivity[{index}]"
        if not isinstance(entry, dict):
            errors.append(f"{where} must be an object, got {type(entry).__name__}")
            continue
        purpose = _text(entry.get("purpose"), f"{where}.purpose", errors)
        package = _package(
            entry.get("package"),
            f"{where}.package",
            vocab.connectors,
            vocab.known,
            errors,
            what="connector",
        )
        if entry.get("package") is None:
            errors.append(
                f"{where}.package is null; a connector on the board needs a "
                f"land pattern, so say which one"
            )
        signals = _string_list(
            entry.get("signals"), f"{where}.signals", errors, limit=32
        )
        out.append(
            Connectivity(purpose=purpose, package=package or "", signals=signals)
        )
    return tuple(out)


def parse_plan_response(
    raw: str | dict, *, vocab: PackageVocabulary | None = None
) -> BoardPlan:
    """Parse and validate a model's plan into a :class:`BoardPlan`.

    Accepts an already-decoded dict or raw model text, tolerating a Markdown
    code fence. **Every** problem is collected and raised together as
    :class:`PlanValidationError` so one repair round can fix all of them --
    the :mod:`silkscreen.netlist` convention, and the reason this never
    raises on the first failure.

    ``vocab`` is the buildable package list; left unset it is read from
    :mod:`silkscreen.footprints` (see :func:`package_vocabulary`).
    """
    if vocab is None:
        vocab = package_vocabulary()

    if isinstance(raw, str):
        try:
            data = json.loads(_strip_code_fence(raw))
        except json.JSONDecodeError as exc:
            raise PlanValidationError(
                [f"response is not valid JSON: {exc}"]
            ) from exc
    else:
        data = raw

    if not isinstance(data, dict):
        raise PlanValidationError(
            [f"expected a JSON object, got {type(data).__name__}"]
        )

    errors: list[str] = []
    building = _text(
        data.get("building"), "building", errors, limit=BUILDING_MAX_CHARS
    )
    power = _parse_power(data.get("power"), vocab, errors)
    rails = _parse_rails(data.get("rails"), errors)
    blocks = _parse_blocks(data.get("blocks"), errors)
    connectivity = _parse_connectivity(data.get("connectivity"), vocab, errors)
    assumptions = _string_list(
        data.get("assumptions"), "assumptions", errors, limit=ASSUMPTIONS_MAX
    )
    questions = _string_list(
        data.get("open_questions"), "open_questions", errors, limit=ASSUMPTIONS_MAX
    )

    # "I do not know" is allowed; "I do not know, and I will not say what I
    # would have assumed" is not. Without this the undecided answer is the
    # cheapest one for a model to give, and the stage stops deciding anything.
    if power.source == "unknown" and not assumptions:
        errors.append(
            'power.source is "unknown" but "assumptions" is empty; say what '
            "you would assume about the power source and why"
        )

    if errors:
        raise PlanValidationError(errors)

    return BoardPlan(
        building=building,
        power=power,
        rails=rails,
        blocks=blocks,
        connectivity=connectivity,
        assumptions=assumptions,
        open_questions=questions,
    )


# ---------------------------------------------------------------- the stage


def propose_plan(
    model: Model,
    intent: str,
    *,
    vocab: PackageVocabulary | None = None,
    max_repairs: int = 1,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> PlanResult:
    """Expand a thin intent into a buildable brief; never guess one.

    One model call, plus at most ``max_repairs`` more when the answer fails
    :func:`parse_plan_response` -- the batched errors go back as a single
    repair prompt each time. When the budget is spent the result carries
    ``plan=None`` and one warning naming the failure: an unplanned run is
    honest, a half-parsed plan is a brief the whole pipeline then designs
    against.

    Note the return type: this is a :class:`PlanResult`, not a
    :class:`BoardPlan`, precisely so "the model could not plan this" is a
    value a caller can carry rather than an exception it has to catch to keep
    the run alive. The board is still the product.

    ``on_event`` receives ``plan.round`` per rejected answer and one final
    ``plan.ready`` -- the decided power source and the counts, never the
    model's text.

    Raises:
        ModelError: the model could not be reached. Deliberately not
            wrapped -- an outage is a different condition from a bad answer,
            and callers route them differently (the
            :func:`~silkscreen.agents.propose.propose_circuit` convention).
    """
    if vocab is None:
        vocab = package_vocabulary()
    base = f"{plan_prompt(vocab)}\nWhat the engineer asked for:\n{intent}\n"
    prompt = base

    plan: BoardPlan | None = None
    last_errors: list[str] = []
    raw = ""
    for round_no in range(max_repairs + 1):
        # A transport failure is NOT wrapped: ModelError propagates so a
        # FallbackModel's failover -- and the service's 502 -- stay intact.
        raw = model.generate(prompt, temperature=0.0, max_output_tokens=4096)
        try:
            plan = parse_plan_response(raw, vocab=vocab)
        except PlanValidationError as exc:
            last_errors = list(exc.errors)
        else:
            break
        if on_event is not None:
            # Validation errors are engine-generated, not model text, so they
            # are safe to put on the wire -- truncated all the same.
            on_event(
                {
                    "event": "plan.round",
                    "round": round_no + 1,
                    "errors": len(last_errors),
                    "first_error": str(last_errors[0])[:160] if last_errors else "",
                }
            )
        if round_no == max_repairs:
            break
        problems = "\n".join(f"  - {e}" for e in last_errors)
        prompt = (
            f"{base}\nYour previous plan was rejected. Fix ALL of these and "
            f"return the corrected JSON object:\n{problems}\n\n"
            f"Your previous plan was:\n{raw}\n"
        )

    if plan is None:
        detail = "; ".join(last_errors)[:_WARNING_CHARS]
        return PlanResult(
            plan=None,
            warnings=[
                f"the board was not planned: no usable answer after "
                f"{max_repairs + 1} attempt(s) ({detail})"
            ],
        )

    warnings: list[str] = []
    if not plan.power.decided:
        # Not a failure -- a stated one. The run continues, and the engineer
        # reading the warnings finds out that the power entry was a guess
        # rather than discovering it when the case has no hole in it.
        warnings.append(
            "the plan could not decide a power source from the request: "
            + "; ".join(plan.assumptions)[:_WARNING_CHARS]
        )
    if on_event is not None:
        on_event(
            {
                "event": "plan.ready",
                "power_source": plan.power.source,
                "packages": list(plan.packages()),
                "rails": len(plan.rails),
                "blocks": len(plan.blocks),
                "assumptions": len(plan.assumptions),
            }
        )
    return PlanResult(plan=plan, warnings=warnings)
