# AI CAD — final plan (CEO decision, 2026-08-31)

Feature: AI-generated OpenSCAD enclosures for the KiCad PCBs the pipeline already
produces. This document is the contract. Five engineers build the five workstreams
below concurrently in one tree; **no two workstreams touch the same file**, and the
public interfaces in "Frozen contracts" are pinned — anyone who needs a contract
change edits this file first, in its own commit, and tells the other four.

Read CLAUDE.md first. Everything there applies: integer nanometres everywhere,
no quiet zeros, all validation failures batched into one error, gated-binary
convention copied from `test_spice.py`, offline suite green with no keys and no
new required binaries.

## Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | **No OpenSCAD in the Docker image. v1 ships `.scad` text + code view + download; PNG/STL render only via the locally-gated CLI.** | Infra/Engineering are right: 350–800 MB and cold-start CPU spikes in a stdlib single-process Cloud Run server, to produce a preview that is not the product. Product's real differentiator — the verified-fit receipt with signed margins — is computed by the *offline* verifier and ships in v1 untouched. |
| 2 | v2 preview candidate is an `openscad-wasm` lazy chunk in the SPA, not three.js and not server render. Decided later; v1 leaves the `find_openscad()` seam and the additive response shape it would need. | Keeps the server stdlib-only forever; client-side render scales with users, not instances. |
| 3 | Model emits validated JSON `EnclosureSpec`, never raw SCAD. Deterministic code injects every measured dimension; the model never types a board millimetre. | The `netlist.py` founding lesson. The model chooses *style within bounds* (lid, wall, cutout selection); geometry comes from the `.kicad_pcb`. |
| 4 | Pure-Python `.scad` emission. The C++ source at `vendor/openscad` is never built, linked, or imported — it is a read-only reference, gitignored, disclosed in DEVPOST. The only interaction with OpenSCAD is exec-ing a user-installed binary, same arms-length boundary as ngspice. | GPL-2.0 stays outside the codebase; MIT project stays MIT. |
| 5 | Enclosure failure never fails the run: exhausted repair budget → `enclosure: null` + a visible `enclosure.failed` event, board still delivered. The repair rounds themselves are visible events (Product's fix-it loop), the degradation is honest (Infra's requirement). | The board is still the product. |
| 6 | Naming: code says `enclosure` everywhere (package, stage, events, response key, artifact `enclosure.scad`); user-facing surfaces say **Case** (SPA tab label, CLI `--case`). | One code vocabulary, one product vocabulary; no half-renamed modules. |
| 7 | CLI v1 is `--case` / `--case-style` / `--case-render` on the existing generate command. The standalone `silkscreen case board.kicad_pcb` subcommand and `--no-model` deterministic default case are deferred to v2. **Amended during implementation:** the standalone subcommand shipped in v1 after all — `silkscreen case <board> [-o] [--intent] [--stl] [--no-model]` — and `--case-render` was never built; the generate command carries only `--case` / `--case-style`, with local STL rendering living on the subcommand's `--stl`. | Keeps workstream D small and the v1 surface reviewable. |
| 8 | CI: `apt-get install openscad` on the **Linux job only**, mirroring ngspice; macOS brew skipped (slow/flaky), Windows skips. Python/web/docker jobs otherwise unchanged. Gated tests skip cleanly everywhere else. | Same convention as every other external verifier. |
| 9 | Heights: `.kicad_pcb` carries no Z, so component heights come from a table keyed by footprint class, with an explicit default that lands as a **warning in the fit report**, never a silent guess. | No quiet zeros. |
| 10 | Coordinate frames: `BoardEnvelope` stays in the KiCad Y-down frame — **no new flip**. The one place enclosure geometry changes frame is inside `emit.py`, which maps the envelope into OpenSCAD's frame. | The project has exactly one Y flip (placer boundary) and this feature does not add a second wandering one. |
| 11 | Hackathon constraints intact: proposals go through the existing `Model` protocol (Gemini via GenAI SDK, `CHEAP_MODEL = gemini-3.5-flash-lite`), the pipeline gains one ADK `@node`, both drivers stay event-identical, service stays on Cloud Run. No new required binary, no new infra dependency. | Non-negotiable submission requirements. |
| 12 | DEVPOST gets the feature tagged `[not yet built]` until merged, plus the optional-openscad-CLI disclosure under Third-party code. `check_docs.py --fix` before merge; never quote a frontend test count. | Documentation discipline section of CLAUDE.md. |

## v1 scope

**In:** two-piece case (base + lid) derived from actual `.kicad_pcb` geometry;
natural-language case intent ("rounded corners, USB cutout left"); JSON spec →
validate → batched repair loop (≤3 repairs); offline fit verification with signed
per-axis margins; `enclosure.scad` written beside the project and returned in the
API/SPA; hash-addressed **Case** tab with code view, parameters, fit receipt,
download; `--case` on the CLI with locally-gated STL/PNG render; ScriptedModel
offline tests throughout.

**Out (v1):** server-side rendering of any kind; in-browser 3D viewer;
arbitrary 3D modeling; real connector-geometry cutouts (the footprint set has no
connectors — cutouts are rectangular openings sized from courtyard extents);
print-readiness claims; ~~standalone `case` subcommand~~ (amended during
implementation: it shipped in v1 — see decision 7); conversational revision of
an existing case (the chat root can simply re-run generation).

## Layout

New package `engine/silkscreen/enclosure/` (installed package — **not** a
top-level directory, which is the retired-code graveyard):

```
engine/silkscreen/enclosure/
  __init__.py      A   (docstring + re-exports of A's names only; everyone else
                        imports submodules directly, so B–D never edit it)
  errors.py        A   whole error taxonomy, incl. render errors (frozen here)
  ir.py            A   EnclosureSpec / Cutout, parse_enclosure_spec
  board_shape.py   B   BoardEnvelope extraction
  heights.py       B   footprint-class → height table
  emit.py          B   deterministic .scad emitter
  verify.py        B   offline fit checks, FitReport
  render.py        C   gated OpenSCAD CLI wrapper
engine/silkscreen/agents/enclosure.py  C   propose_enclosure + prompt
```

## Frozen contracts

All dimensions are **integer nanometres** (`silkscreen.units`). JSON at the
model boundary and `params` at the API boundary use mm floats; the conversion
happens once in `parse_enclosure_spec` (in) and once in the formatters (out).

### errors.py (owner A — includes the render errors C raises)

```python
class EnclosureError(Exception): ...
class EnclosureValidationError(EnclosureError):
    errors: list[str]          # every failure, batched — one repair prompt
class CavityFitError(EnclosureError):
    margins_nm: dict[str, int] # signed, keys "x","y","z"; negative = collision
class CutoutError(EnclosureError): ...      # bad ref, bad face, overlap
class WallError(EnclosureError): ...        # below MIN_WALL_NM etc.
class RenderUnavailable(EnclosureError):
    executable: str            # "openscad" — names what was searched for
class RenderFailed(EnclosureError): ...
class EmptyGeometryError(RenderFailed): ... # OpenSCAD warned-and-emitted-nothing
```

### ir.py (owner A)

```python
MIN_WALL_NM: int          # mm(1.2) — printable FDM minimum
DEFAULT_WALL_NM: int      # mm(2.0)
DEFAULT_CLEARANCE_NM: int # mm(1.0) board-to-cavity
FACES: tuple[str, ...]    # ("left", "right", "front", "back", "top")
LIDS: tuple[str, ...]     # ("friction", "screw", "none")

@dataclass(frozen=True)
class Cutout:
    id: str          # unique within the spec
    ref: str         # board ref, e.g. "J1" — engine resolves geometry
    face: str        # member of FACES
    margin_nm: int   # opening margin around the resolved courtyard interval

@dataclass(frozen=True)
class EnclosureSpec:
    wall_nm: int
    clearance_nm: int
    lid: str                      # member of LIDS
    corner_radius_nm: int         # 0 = square
    cutouts: tuple[Cutout, ...]
    standoffs: bool               # auto-placed by the emitter, not positioned by the model
    vents: bool
    label: str | None             # embossed text on the lid, sanitised

def parse_enclosure_spec(text: str) -> EnclosureSpec
```

`parse_enclosure_spec` accepts raw model output (fenced JSON tolerated, mm
floats in the JSON) and **collects every failure into one
`EnclosureValidationError`** — negative/zero dims, wall below `MIN_WALL_NM`,
unknown face/lid, duplicate cutout ids, malformed refs. Ref *existence* is not
checked here (the IR does not know the board); that is `verify_fit`'s job.

### board_shape.py / heights.py (owner B)

```python
@dataclass(frozen=True)
class PartExtent:
    ref: str
    x_min_nm: int; y_min_nm: int; x_max_nm: int; y_max_nm: int  # KiCad Y-down, absolute
    height_nm: int
    height_default: bool   # True when the heights table had no entry (surfaces as a warning)

@dataclass(frozen=True)
class BoardEnvelope:
    outline_nm: tuple[tuple[int, int], ...]  # Edge.Cuts polygon, KiCad Y-down
    x_min_nm: int; y_min_nm: int; x_max_nm: int; y_max_nm: int
    thickness_nm: int                        # board substrate, default mm(1.6)
    parts: tuple[PartExtent, ...]
    max_height_nm: int

def board_envelope(path: str | Path,
                   *, heights: Mapping[str, int] | None = None) -> BoardEnvelope
# raises ValueError when the board has no Edge.Cuts outline — a board without a
# boundary cannot be encased, per the set_board_outline convention.

# heights.py
DEFAULT_HEIGHT_NM: int                      # mm(3.0)
HEIGHTS_NM: dict[str, int]                  # footprint-class key → height
def height_for(footprint_name: str) -> tuple[int, bool]   # (height, was_default)
```

No new Y flip: everything stays in the KiCad frame.

### emit.py (owner B)

```python
def emit_scad(spec: EnclosureSpec, envelope: BoardEnvelope) -> str
```

Deterministic (same inputs → byte-identical output). nm→mm **only** inside the
formatter via `units.to_mm` with fixed precision. Emits named parameters at the
top (`board_x`, `board_y`, `wall`, `clearance`, `cavity_z`, …) and named modules
`base()`, `lid()`, `standoffs()`. This is the one place geometry crosses into
OpenSCAD's frame: `emit.py` owns the KiCad-Y-down → SCAD map (`_sy`), a
distinct boundary from the solver↔KiCad flip that `kicad.py` owns — each of
the two "one flip" statements is scoped to its own boundary, and neither
crossing may happen anywhere else. Structural invariants (tier-1 tests read these back out of
the text): `cavity_x == board_x + 2*clearance`, `outer − cavity == 2*wall` per
axis, each cutout opening covers its connector's courtyard interval + margin.

### verify.py (owner B)

```python
@dataclass(frozen=True)
class FitReport:
    margins_nm: dict[str, int]     # signed, per axis — the receipt
    warnings: tuple[str, ...]      # e.g. "clearance under 0.5 mm", "U1 height defaulted"
    params_mm: dict[str, float]    # the emitted parameters, for display

def verify_fit(spec: EnclosureSpec, envelope: BoardEnvelope,
               *, strict: bool = False) -> FitReport
```

Every failure raises a specific error (`CavityFitError` with signed margins,
`CutoutError` naming an absent ref — hard error per the `edge_refs` convention —
`WallError`); nothing returns a quiet zero. `strict=True` promotes warnings to
errors (the `Testbench(strict=True)` precedent) and is what the agent loop uses.
**Amended during implementation:** `strict=True` promotes only the
*spec-fixable* warnings (e.g. a tight clearance). Warnings the model cannot fix
by editing the spec — a defaulted component height, an empty board — are never
promoted and always ride `FitReport.warnings`, or the repair loop would spin
forever on a board fact no spec change can alter.

### render.py (owner C — gated CLI half, never on the service path)

```python
def find_openscad() -> str | None          # shutil.which seam
def available() -> bool
def render_stl(scad: str, out_path: Path, *, timeout_s: float = 60.0) -> Path
def render_png(scad: str, out_path: Path, *, timeout_s: float = 60.0) -> Path
```

Absent binary → `RenderUnavailable("openscad")`. Non-zero exit or stderr errors
→ `RenderFailed`. A well-formed but empty STL → `EmptyGeometryError`, never a
vacuous pass. Test gating copies `test_spice.py` exactly (`HAS_OPENSCAD`,
`needs_openscad` skipif).

### agents/enclosure.py (owner C)

```python
ENCLOSURE_PROMPT: str   # must contain the literal marker "ENCLOSURE-SPEC v1"
                        # (frozen so any workstream's ScriptedModel.by_marker can key on it)

class EnclosureProposalError(EnclosureError):
    attempts: int

def propose_enclosure(model: Model, envelope: BoardEnvelope,
                      *, style_hint: str = "", max_repairs: int = 3,
                      on_event: Callable[[dict], None] | None = None,
                      ) -> tuple[EnclosureSpec, int]   # (spec, repair_rounds)
```

**Amended during implementation:** the return type is
`tuple[EnclosureSpec, FitReport, int]` — `(spec, fit, repair_rounds)`. The
accepting round's `FitReport` is returned rather than discarded so callers
never re-verify what the loop already verified.

Mirrors `propose.py`: deterministic facts injected into the prompt (outline
size, part rects, per-zone max height, edge-adjacent refs with their faces);
repair rounds resend the full prompt + the batched errors + the previous
proposal; `ModelError` propagates unwrapped (so `FallbackModel` wraps
unchanged); budget exhaustion raises `EnclosureProposalError(attempts=…)`.
Each round also runs `verify_fit(strict=True)` so fit failures feed the repair
loop, not just JSON-shape failures. Uses `CHEAP_MODEL`.

### Pipeline / events (owner C)

`generate_pcb` gains `enclosure: bool = False` and `enclosure_style: str = ""`
(opt-in, so both drivers stay event-identical by default). `enclosure_stage`
lands in `stages.py` **after `route_stage`, before `review_stage`**, no-ops
silently when `enclosure` is falsy (the `route_stage` pattern), and writes
`enclosure.scad` beside the project only when `output` is set (the
`schematic_stage` filesystem rule). The ADK workflow gains one always-run
`@node(name="enclosure")`; `test_adk.py`-style parity applies.

*Revised 2026-09-03:* the stage now **starts on a worker thread right after
placement repair** (`start_enclosure_stage`, from a `placed_snapshot`) and is
**joined after route, before review** (`EnclosureJob.result`); the ADK graph
has `enclosure_start` and `enclosure` nodes at those two points. The event
names are unchanged; only their position relative to schematic/route is now
wall-clock order.

`PipelineResult` gains:

```python
class EnclosureResult(NamedTuple):
    spec: EnclosureSpec
    scad: str
    fit: FitReport
    repair_rounds: int
    rendered: bool          # always False on the service path in v1
# PipelineResult.enclosure: EnclosureResult | None
```

Event names (frozen — D passes them through, E renders them):

```
{"event": "stage.start", "stage": "enclosure"}
{"event": "enclosure.round", "round": <int>, "errors": <int>, "first_error": <str ≤160>}
{"event": "stage.done", "stage": "enclosure", "cutouts": <int>, "lid": <str>,
 "wall_mm": <float>, "repair_rounds": <int>, "rendered": false}
{"event": "enclosure.failed", "error": <str ≤160>}   # run continues, enclosure=None
```

Any `EnclosureError`/`EnclosureProposalError` inside the stage is caught by the
stage body itself → `enclosure.failed` + `None`. Everything else (including
callback exceptions) propagates as today.

### Service response (owner D — additive)

```json
"enclosure": {
  "scad": "<full text>",
  "params": {"board_x": 48.2, "wall": 2.0, ...},
  "fit": {"margins_mm": {"x": 1.0, "y": 1.0, "z": 0.55}},
  "warnings": ["U1 height defaulted to 3.0 mm"],
  "repair_rounds": 1
}
```

or `"enclosure": null`. Request opt-in: `"enclosure": true`,
`"enclosure_style": "<free text ≤500 chars>"` on `POST /generate` and
`/generate/stream`; the stream passes the events above through unchanged.
Invalid `enclosure_style` type/length is a plain pre-stream 400 like every
other field. The `.scad` rides the JSON exactly as `kicad_pcb` does.

### CLI (owner D)

```
--case                 generate an enclosure; writes enclosure.scad beside -o
--case-style TEXT      natural-language case intent
--case-render          additionally render enclosure.stl + enclosure.png via the
                       local openscad binary; exits with a clear message naming
                       the executable when it is absent (RenderUnavailable)
```

**Amended during implementation:** `--case-render` was never built. The
generate command carries `--case` / `--case-style` only, and rendering moved
to the standalone subcommand that shipped in v1 (see decision 7):

```
silkscreen case <board.kicad_pcb>
  -o PATH        where to write enclosure.scad
  --intent TEXT  natural-language case intent
  --stl          additionally render enclosure.stl via the local openscad
                 binary; exits with a clear message naming the executable when
                 it is absent (RenderUnavailable)
  --no-model     emit the deterministic default-spec case with no API call
```

## Workstreams

Interfaces above are the build-against contract; C (and D, E) start immediately
against them without waiting for A/B code. File ownership is exclusive —
if you need a file another workstream owns, you need a contract conversation,
not a merge conflict.

### A — Enclosure IR + errors
**Owns:** `engine/silkscreen/enclosure/__init__.py`, `enclosure/errors.py`,
`enclosure/ir.py`, `engine/tests/test_enclosure_ir.py`.
Frozen dataclasses, all-integer-nm, `parse_enclosure_spec` with batched
`EnclosureValidationError`. Tests: reject negative/zero dims, walls below
`MIN_WALL_NM`, unknown face/lid, duplicate cutout ids; assert a multi-error
input reports *all* errors in one exception; fenced-JSON tolerance.
`__init__.py` re-exports A's names only — nobody else edits it.

### B — Board envelope + emitter + offline verify
**Owns:** `enclosure/board_shape.py`, `enclosure/heights.py`, `enclosure/emit.py`,
`enclosure/verify.py`, `engine/tests/test_enclosure_geometry.py`,
`engine/tests/test_enclosure_emit.py`, `engine/tests/test_enclosure_verify.py`.
**Amended during implementation:** the emitter and verifier tests landed
together as `engine/tests/test_scad_emit.py` (there is no
`test_enclosure_emit.py` or `test_enclosure_verify.py`); geometry tests are in
`engine/tests/test_enclosure_geometry.py` as planned.
Geometry tests compute expected bboxes with inline math over the raw
`ref.kicad_pcb` fixture, never by calling the extractor (the `test_kicad.py`
oracle discipline), and include a rotated-footprint case (the issue-9 bug
class). Emitter tests are tier-1 always-on: an independent `.scad` reader in
the test file (regex/token extraction of numeric literals, importing no emitter
constants) asserts the structural invariants listed under emit.py, plus the
round-trip property (emitted `.scad` → tier-1 reparse → dims equal the IR's).
Fully offline; B ships no gated tests. **Amended during implementation:**
`test_scad_emit.py` does carry gated render tests alongside its offline tier
(see the workstream C amendment — `test_enclosure_render.py` was never
created).

### C — Agent stage + repair loop + render gate + pipeline wiring
**Owns:** `engine/silkscreen/agents/enclosure.py`, `enclosure/render.py`,
`engine/silkscreen/agents/stages.py`, `agents/pipeline.py`,
`agents/adk/workflow.py`, `.github/workflows/ci.yml` (the one-line Linux
openscad install), `engine/tests/test_enclosure_agent.py`,
`engine/tests/test_enclosure_render.py`.
`propose_enclosure` per contract; `ScriptedModel.by_marker` keyed on the
`"ENCLOSURE-SPEC v1"` marker keeps tests offline; tests assert the repair
prompt contains the batched validation errors and that `enclosure.failed`
degrades without killing the run. `test_enclosure_render.py` holds **all**
gated tests (tier-2): render an STL, parse its bounding box with inline
vertex min/max, assert the cavity contains the board box and the mesh is
non-empty. **Amended during implementation:** there is no
`test_enclosure_render.py` — the gated (`needs_openscad`) tests live in
`engine/tests/test_scad_emit.py` and `engine/tests/test_enclosure_agent.py`
instead, gating exactly as planned. Until A/B land, C develops against local stubs matching the frozen
signatures and swaps to real imports at integration — the stubs never merge.

### D — Service + CLI surface
**Owns:** `service/app.py`, `engine/silkscreen/cli.py`,
`service/tests/test_enclosure_service.py`, `engine/tests/test_enclosure_cli.py`.
Additive `enclosure` response key and request opt-in per contract; stream
pass-through; the field-validation 400 must not regress known issue 10's
pattern (validate `enclosure`/`enclosure_style` before the pipeline runs).
CLI flags per contract. Service tests monkeypatch `generate_pcb` (existing
convention) and assert the additive key, the `null` degradation, and that the
one-shot response never grows raw model output. CLI tests cover `--case`
writing `enclosure.scad` and `--case-render` failing with the executable's
name when openscad is absent (ungated: assert the message, mocking
`find_openscad` to return `None`). **Amended during implementation:** the CLI
tests landed as `engine/tests/test_cli_case.py` (not
`test_enclosure_cli.py`), and the absent-binary case exercises the shipped
`case` subcommand's `--stl` flag rather than `--case-render` (see decision 7).

### E — Frontend Case tab + chores
**Owns:** `frontend/src/lib/enclosure.js`, `frontend/src/lib/enclosure.test.js`,
`frontend/src/lib/tabs.js`, `frontend/src/lib/tabs.test.js`,
`frontend/src/lib/run.js`, `frontend/src/lib/run.test.js`,
`frontend/src/App.svelte`, `frontend/src/components/CaseTab.svelte`,
plus chores: `.gitignore`, `vendor/README.md`, `DEVPOST.md`,
`docs/ai-cad-plan.md` (this file's upkeep), and the CLAUDE.md gated-tools
paragraph at merge time.
Fifth hash-addressed **Case** tab, peer in `tabs.js`: code view + copy +
download (`enclosure.scad` via the existing `download.js` — reused, not
modified), parameter table, and the fit receipt (signed margins + warnings).
Chat artifact card follows the existing compact-card pattern. `data-testid`s
on intrinsic elements only, disambiguated by identity attributes.
Chores: gitignore `vendor/openscad/` and `*.stl` (NOT `*.scad` wholesale —
fixtures must stay trackable); vendor/README.md entry with the shallow-clone
command and the do-not-import rule; DEVPOST `[not yet built]` tag until merged
+ optional-openscad disclosure; `check_docs.py --fix` before merge; never a
frontend test count in README/DEVPOST.

## Integration order

A and B merge first (independently). C merges once both are in and its stubs
are deleted. D and E merge in any order after C (both depend only on frozen
event/response shapes, so they can be reviewed in parallel). Every PR passes
the offline suite with no openscad installed; the Linux CI job additionally
exercises the gated tier. After merge, remove the `[not yet built]` tag and
add openscad to CLAUDE.md's gated-tools paragraph (E owns both edits).

---

# v2 — the CAD-engineer case (research decision, 2026-09-03)

v1 shipped a style-picker: the model chose seven knobs, the emitter drew a
plain OpenSCAD box, and nobody ever looked at the result. A five-agent
research pass over the open-source AI-CAD field (full reports in the session;
citations inline below) settled what "the best open-source AI CAD design" is
today and what of it to clone.

## What the field says

| Finding | Evidence |
|---|---|
| The winning architecture is *LLM proposes → B-rep kernel builds → deterministic geometric assertions come back as typed errors → bounded repair*. Renders are a diagnostic, not the gate. | CADTests (arXiv 2605.07807): execution + property tests beat ReAct-with-tracebacks by ~10 pts; "visual feedback via multi-view renderings does not improve performance". CADSmith (2603.26512): kernel metrics + independent judge took IoU 0.81→0.96. Consensus-selection (2608.09706) beats a VLM verifier on the same candidates. |
| A constrained IR / parametric library beats free code for parts that must mate. | Multi-Agent-CAD: JSON brief → JSON plan → deterministic build123d translator, 99.3 % feature pass at 116× fewer tokens than free-code CAD skills. Pointer-CAD v2: −13 % without a plan stage. MUSE (2605.28579): frontier models write watertight code far more often than *overlap-free* assemblies. |
| build123d/CadQuery on OCCT is the kernel. OpenSCAD cannot write STEP, has no fillet primitive, and no B-rep validity. | Every 2025-26 benchmark targets CadQuery; `cadquery-ocp-novtk` 7.9.3 ships wheels for mac/linux/windows 3.10-3.14 (verified here: shell+fillet+STEP in 30 ms). |
| The largest open project (earthtojake/text-to-cad, 14 k★, MIT) is a skill harness, not a library: brief → `gen_step()` source → `validate` (BRepCheck, **signed volume per solid**, open shells, self-intersection) → `measure`/`interference` → 4-view snapshot packet (iso, opposite iso, top, front) → repair loop with failure classes. | Its own docs: `refs --facts` "ok" is not a geometry claim; convert every visual concern into a geometry check before it becomes a validation claim. |
| pzfreo/build123d-mcp's `verify_spec` (hole / boss / wall_thickness_at / material_at_point → PASS/FAIL/UNVERIFIED) lifted a model on CADGenBench 0.360→0.457 and validity 88→100 %. | The declarative assertion vocabulary to copy into `kernel.py`. |
| What a good FDM PCB case actually is (YAPP_Box v3, kicad2freecad-enclosures, turbocase, NopSCADlib): standoffs **at the board's mounting holes**, 4–5 mm tall, Ø7 with insert bores (M3: Ø4.0 × 5.8); a **ring** lip 1.2 wide × 3 deep with 0.2 slack and a 0.5 vertical gap; 0.5 × 45° elephant-foot chamfers; cutouts sized from the **plug** (USB-C overmold 12.35 × 6.5 → 13 × 7 opening), not the receptacle courtyard; vents ≥ 1.5 mm with 2 mm boss keep-out. | v1 violated all three headline rules: corner standoffs 2 mm tall at no hole, plug lip, courtyard cutouts. Numbers now live in `enclosure/rules.py`. |

## Decisions

| # | Decision |
|---|---|
| 13 | **Kernel: build123d (OCCT) behind a new `cad` extra** (`pip install -e ".[cad]"`). Gated exactly like ngspice/openscad: `needs_build123d` skips, `KernelUnavailable` names the extra. The OpenSCAD emitter (`emit.py`) and offline verifier (`verify.py`) stay as the **fallback** so the suite, Cloud Run, and a machine without the extra keep working. CI installs the extra on **every OS** — `ci.yml:40` installs `".[dev,agents,cloud,adk,cad]"` once for the whole Linux/macOS/Windows matrix, and the frozen-contracts table below says the same. |
| 14 | **The model still proposes JSON, never code.** ENCLOSURE-SPEC v2 adds `mount` (holes/pins/corners/none), `insert` (M2/M2.5/M3/M4/self_tap), `material` (PLA/PETG/ABS) and the `snap` lid; `friction` parses as `lip`. Every measured number comes from `rules.py` + the board. |
| 15 | **Board facts grow**: mounting holes (NPTH / unconnected drills ≥ 2 mm), connector class per part (→ plug envelope), and a footprint `Height` property (turbocase convention) beating the class table. |
| 16 | **Acceptance = kernel clauses, every one a signed margin**, thirteen of them (`kernel.py:114` `CLAUSES` is the list; this row enumerated twelve and omitted `underside`): valid topology, positive volume per solid, solid count, bbox, board keep-out clash volume = 0, headroom above the tallest part, underside clearance, boss concentric with its hole, every cutout admits its plug prism, lid mates with the base (assembled intersection = 0, lip gap within slack), minimum wall, overhangs ≤ 45° in the printed orientation, vents clear of bosses. `KernelReport` is the receipt; `KernelFitError` carries the failing clause names. Nothing returns a quiet zero. |
| 17 | **Snapshots are advisory and bounded**: a 4-view + section packet from a dependency-free software renderer (tessellate → numpy → Pillow), at most two critic rounds, findings filtered to parts the spec names (the `review.py` rule), never the gate. |
| 18 | **Loop** (`agents/enclosure.py`): brief → spec → build (a `KernelError` with its failure class goes back as a repair) → kernel report → optional critic → compact text report → revise; three outer rounds; the OpenSCAD path is the fallback when the kernel is absent. |
| 19 | **Artifacts**: `enclosure.step` (assembly, labelled `base`/`lid`) is primary; `enclosure-base.stl`, `enclosure-lid.stl` derived; `enclosure.scad` becomes a preview wrapper that `import()`s the STLs so the desktop's live OpenSCAD window keeps working unchanged. |

## Frozen contracts (v2)

```
enclosure/rules.py       design-rule tables (this commit)
enclosure/ir.py          EnclosureSpec + mount/insert/material, LIDS = lip|screw|snap|none
enclosure/board_shape.py PartExtent.connector/lib_id, MountingHole, BoardEnvelope.mounting_holes
enclosure/errors.py      KernelUnavailable, KernelError(failure_class), KernelFitError(failed, margins_nm)
enclosure/cad.py         EnclosureModel, build_enclosure(spec, envelope), export_model(model, dir, stem), preview_scad(...)
enclosure/kernel.py      Clause, KernelReport, verify_model(model, spec, envelope)
enclosure/snapshot.py    render_packet(model, out_dir), render_grid(model) -> PNG bytes
agents/enclosure.py      propose_enclosure(...) keeps its signature; returns kernel report + brief when the kernel is present
```

Frames: `EnclosureModel` geometry is in the **assembled** frame: X as KiCad,
Y flipped (front = KiCad max-Y, the `emit.py` map), Z up, origin at the
base's outer bottom-left-front corner. `lid` is stored in its **printed**
orientation (outer face on the bed at z = 0, lip pointing +Z) and
`lid_assembled` is the `Location` taking it onto the base, so printability
checks run on what the printer sees and mating checks run on what the user
sees. The board keep-out solid (substrate slab + one prism per part, top and
bottom side) is built once, in `cad.py`, from `BoardEnvelope` alone — the
verifier never re-derives it.

## v2 status (2026-09-04)

Built on `local/openscad`, uncommitted, by five parallel workstreams against
the frozen contracts above:

| Piece | State |
|---|---|
| `enclosure/cad.py` | Built. All four lid styles build and export (STEP assembly, two STLs, preview `.scad`) for the 11-part STM32 fixture in 0.2–10 s. Label is a **deboss** (an emboss on the bed face fails `overhang` honestly). Lip/snap lids widen the clearance so the ring never overhangs the board edge — the kernel's `headroom` clause caught the original 0.2 mm overhang. |
| `enclosure/kernel.py` | Built. All 13 clauses, measurement method documented per clause in the module; every lid style passes with margin ≥ 0 on the fixture. |
| `enclosure/snapshot.py` | Built. Five-view packet and 2×2 grid in under a second, byte-deterministic, no OpenGL. |
| `agents/enclosure.py` | Built. ENCLOSURE-SPEC v2 prompt, mounting-hole/plug facts, build → verify → repair loop with `KernelError` failure classes, optional bounded image critic (`critic=True`) that never gates. Returns `EnclosureProposal`. |
| stage / service / CLI / CI | Built. `EnclosureResult.kernel/exports/snapshots/engine`, `enclosure.kernel`/`files`/`engine` in the JSON, `<stem>.step` etc. beside the board on the steps route, `silkscreen case` prints the kernel report, CI installs the `cad` extra on every OS. |
| overlay + SPA | Built. Case step shows engine, clause table with signed margins, "Reveal case files" / "Open 3D model". |

Still open: an in-app 3D viewer **for the case**. The overlay does now have a real WebGL
viewer (`app/src/components/ModelViewer.tsx`, mounted from `StepPanel.tsx`), but it renders
the order step's board GLB; the case's `.step`/`.stl` are still only revealed on disk and
handed to an external application, and nothing here tessellates them for the webview. Also
open: the critic has not been exercised live
against Gemini; `rules.py` plug envelopes cover the common KiCad connector
classes only.

---

# v3 — the OpenSCAD path is removed (2026-09-08)

**Supersedes decision 1 in full, decision 19 in part, and the fallback half of
decision 13.** Decisions 2, 4, 6, 7, 8, 10 and 12 are affected only where they
name OpenSCAD; everything else above stands and is left readable as written.

## What changed

`engine/silkscreen/enclosure/emit.py` (the v1 `.scad` text emitter),
`verify.py` (the offline fit verifier) and `render.py` (the gated OpenSCAD CLI
wrapper) are **deleted**, along with `cad.preview_scad`, the `.scad` member of
`ExportPaths`, the `RenderUnavailable` / `RenderFailed` / `EmptyGeometryError`
error classes, and `engine/tests/test_scad_emit.py`. The build123d/OCCT kernel
is the only enclosure engine. Where it is absent the case step **refuses in
words** naming `pip install -e ".[cad]"`:

* `propose_enclosure` raises `KernelUnavailable` **before** the first model
  call — asking a model to style a case that cannot be built is a paid call
  for nothing;
* `enclosure_stage` catches it like any other `EnclosureError`, emits
  `enclosure.failed` carrying the message (the sentence is under 160
  characters on purpose, so it survives the event seam's truncation), and
  answers `None` — the board is still the product, per decision 5;
* `silkscreen case` prints it and exits 2.

## Why

TODO.txt feature 28 measured what the fallback actually was:

* **v1 and v2 build different physical objects.** Same spec, same board: v2 is
  1.20 mm wider and deeper on every board and 2.5 mm taller on two, because v2
  widens clearance 1.00 → 1.60 mm so its 1.2 mm ring lip clears the board edge,
  and says so in a warning. v1 has no counterpart and its "lip" is a solid slab
  plugging the cavity mouth. *Only v2's case is right, and v1 ships.*
* **v1's receipt cannot fail.** `verify_fit`'s margins equal `spec.clearance`
  exactly, on every board, at every clearance — x/y is clearance minus an
  overhang that is always zero, and z's clearance cancels because the cavity is
  built from `max_height`. Tripling a connector's height does not move it.

A fallback that silently ships a wrong box behind a receipt that cannot fail is
worse than no case at all. The failure mode being removed is not "the case is
missing", which a person can see; it is "the case is subtly wrong and the
receipt says it is fine", which a person cannot.

## Consequences, and the decisions they reverse

**Decision 1 (no OpenSCAD in the image) is reversed as applied to the kernel,
not on its own terms.** That decision rejected shipping the OpenSCAD *renderer
binary*: 350–800 MB of apt tree plus a CPU-spiking subprocess per preview, in a
stdlib single-process Cloud Run server, "to produce a preview that is not the
product". OCP is a different thing — a linked library the case path imports
lazily (measured: 2.1 s first import, 215 MB installed) — and since this
decision it *is* the product: it builds the geometry and computes the
acceptance clauses. The `Dockerfile` therefore installs
`".[agents,cloud,adk,cad]"`. Different thing, different call.

**Decision 19 loses its third artifact.** `export_model` writes
`<stem>.step` plus the two STLs and nothing else; there is no preview wrapper,
because there is nothing left to preview it with. The desktop's OpenSCAD
window is not fed by this repo any more.

**Decision 13's fallback clause is void.** The `cad` extra stays an *extra*
rather than moving into base `dependencies`: OR-Tools + kiutils is the lean
install, OCP is 215 MB, and the refusal is what makes an extra honest. CI
already installs it on all three OSes (`ci.yml:40`), so the
`apt-get install openscad` step is gone.

**The service contract changes shape rather than emptying.** `/generate`'s
`enclosure` block carries `"step"` (the ISO 10303-21 assembly as text — ASCII
and self-contained, which is what makes it a legitimate replacement for the
inline `.scad`) where it carried `"scad"`, and drops `"params"`, `"fit"` and
`"engine"`; the kernel clause table is the only receipt. On the one-shot route,
which writes nothing durable, the stage exports into a `TemporaryDirectory` and
reads the STEP back, so a complete case still ships.

## Left open

`frontend/src/lib/enclosure.js` and `CaseTab.svelte` still read `scad`,
`params`, `fit` and `engine`, so the SPA's Case tab renders nothing until it
is repointed at `step` and the kernel clause table. Outside this change's
scope and tracked as a follow-up. (`service/steps.py` and the desktop bridge
were moved to `.step` in the same pass, by their owner.)

`CavityFitError` and `WallError` remain in `errors.py` and the package's
`__all__`: nothing raises them any more, since `verify.py` was their only
author, but they are named in the frozen taxonomy above and removing them was
not part of this change.

---

# v4 — the board in the case (2026-09-08)

Everything above builds and checks a *case* around a synthetic keep-out solid:
courtyard boxes from `board_shape.py`, extruded to heights from a class table.
v4 adds the other half of the picture — the real board, from KiCad's own 3D
export, seated in that case and measured there — in
`engine/silkscreen/enclosure/assembly.py`, with `freecad_check.py` as an
independent reader of the finished file. Both are opt-in and additive: nothing
in the existing case path changes, and `silkscreen case --assemble` is the
only caller.

## What it does

`kicad-cli pcb export step` produces `<stem>-board.step` (the substrate plus
one solid per footprint that names a 3D model); build123d imports it, seats it
on the standoffs, and `<stem>-assembly.step` comes out with three labelled
children — `base`, `lid`, `board` — where `board` in turn carries every part
under its own name. `verify_assembly` then measures seven clauses on that
assembled B-rep, in `kernel.py`'s own `Clause`/`KernelReport` types with the
same signed-margin-in-nanometres convention.

## Decisions

| # | Decision | Why |
|---|----------|-----|
| 20 | **FreeCAD is not needed to build the assembly, and is not used for it.** build123d's `import_step` reads the kicad-cli STEP directly — six solids of the demo board, assembly labels intact, in 0.25 s. Seating is therefore one OCCT in one process, no subprocess, no temp file. | The obvious prior art here is KiCad StepUp (`kicadStepUptools.py`), which merges a board and a case *inside FreeCAD* — but it is a FreeCAD workbench, so FreeCAD is its host, not its requirement. Taking the tool without the reason would have bought a subprocess and a second kernel for a capability we already had. |
| 21 | **Where FreeCAD does appear it is out-of-process, always, and only as a second reader.** `freecad_check.crosscheck_step` runs `freecadcmd <script.py> <step>` and parses one marked JSON line. Nothing in `silkscreen` may `import FreeCAD`; `test_enclosure_assembly.py` pins that with an `ast` walk over the package (a grep could not tell the module's own import from the string it hands the child). | FreeCAD and build123d each link their own OCCT; two OCCT runtimes in one address space is undefined behaviour. And a checker written in terms of the library that produced the artifact shares its blind spots — the reason `audit/geometry.py` re-reads `.kicad_pcb` instead of calling `kicad.py`. |
| 22 | **The board is seated by a pure translation, and the board's *underside* meets the standoff tops.** | Both halves are read from source. KiCad's exporter negates Y and puts z = 0 at the board's underside: `pcbnew/exporters/step/step_pcb_model.cpp`, where `makeWireFromChain` builds every point as `gp_Pnt( pcbIUScale.IUTomm(x - aOrigin.x), -pcbIUScale.IUTomm(y - aOrigin.y), aZposition )` and `getBoardBodyZPlacement` sets `aZPos = bottom`. That is the same handedness `cad.py` already uses, so no rotation is involved. The seating rule is TurboCase's: `turbocase/scad.py::generate` emits the whole case under `scale([1, -1, 1])` and draws the board at `translate([0, 0, floor_height + standoff_height]) pcb();` — which is exactly `_Dims.board_bottom = wall + standoff_h` here. |
| 23 | **The board is a sibling in the assembly, never fused into base or lid.** | TurboCase again: its board is drawn only under `if (show_pcb && $preview)`. The printed object must not change because someone asked to look at the fit. |
| 24 | **The X/Y seat comes from `model.board_boxes[0]`, not from re-deriving `wall + clearance`.** | The emitter is the authority on where it put the board. A second copy of that arithmetic in the verifier would agree with the first while both were wrong — `test_kicad.py`'s lesson, and the reason `cutout_admits_plug` builds its own prisms instead of reusing the emitter's. |
| 25 | **Measurements are taken on flat, individually-moved solids, never on the imported assembly tree.** | Measured, not assumed: a build123d node fetched through `.children` reports a correct *global* bounding box but hands `_bool_op` a wrapped shape that has lost the placement. On the demo board that read as an 8.97 mm³ clash between the SOT-223 and the case — a quarter of the part's own volume, sitting at the origin — with the board correctly seated. `_labelled_solids` therefore takes `shape.solids()` (global frame) and recovers each label by matching bounding boxes. |
| 26 | **A missing `kicad-cli` refuses in words naming the exact command.** `BoardModelUnavailable`, and there is no fallback board model. Exit 0 with no file, or a non-zero exit, are refusals too. | The `KernelUnavailable` rule applied to the second binary. A case verified against an invented board is worse than a case verified against no board, which is the v3 argument restated. |

## The seven clauses

Same `Clause` objects, same report, same "a clause that cannot be evaluated
fails with a reason" rule; a second *list*, not a second machine.

| Clause | Measures |
|--------|----------|
| `board_imported` | every solid in the file has volume; states the count, the board body's volume and the component count |
| `board_registration` | the seated board body's XY extent vs the emitter's own substrate box, 0.01 mm — the transform's receipt |
| `board_seated` | a downward ray at each standoff centre meets case material at the board's underside, within 0.01 mm |
| `seated_clash` | `volume(base ∩ board) + volume(lid ∩ board)` on the *real* board; pass margin is the measured clearance |
| `seated_headroom` | lid material over each real top-side part, probed over that part's own footprint (`kernel._headroom`'s method) |
| `part_height_claim` | each real component's top vs the keep-out box the case was built around, matched by XY centre — **the clause this module exists for**, since it is the only place the height table can be caught under-claiming |
| `cutout_clear_of_board` | `kernel._plug_prisms`' own prisms, with the seated board added as a third obstacle |

The substrate's *thickness* is deliberately a warning rather than a clause:
KiCad's STEP board body is the dielectric between the outer copper layers
while `(general (thickness))` is the finished stack, and they differ by the
copper and mask — 1.510 mm against 1.600 mm on the demo board. Seating is on
the imported underside, so the difference can only ever show up as headroom,
which `seated_headroom` measures for real.

## Measured, on a generated AMS1117-3.3 board (macOS, KiCad 10.0.6, FreeCAD 1.1.3)

* `kicad-cli pcb export step`: **0.4 s**, **328 127 bytes**, AP214, 6
  `MANIFOLD_SOLID_BREP`s, 191 `ADVANCED_FACE`s — PCB plus five component
  solids.
* `import_step` into build123d: **0.25 s**. Seat + verify: **under 1 s**.
* `<stem>-assembly.step`: **801 405 bytes**, eight solids.
* Seat offset `(5.6, 21.45, 6.0)` mm; every clause passed, tightest margins
  `part_height_claim +0.105 mm` (SOT-223: 1.785 mm of real body under a
  1.8 mm claim, plus mask) and `seated_headroom +0.605 mm`.
* FreeCAD, out of process, on the exported file: `base` 3894.4663 mm³ and
  `lid` 1536.0942 mm³ against build123d's 3894.466 / 1536.094, and the board
  body at z 6.000..7.510 — the seated height, in somebody else's kernel.

## Not done, on purpose

* **No connector cutout has ever been checked against a real connector
  model.** `cutout_clear_of_board` is implemented and tested, but the
  emitter's footprint set has no connectors yet (TODO.txt), so on every board
  this repo can currently generate the clause reports *nothing to check*.
* **Nothing calls this from the service, the pipeline or the desktop.** The
  assembly is a CLI flag; wiring it into `/generate` or `service/steps.py`
  would mean shipping a second STEP and a second clause table through those
  contracts, and belongs to their owners.
* **No render.** The assembly is a file to open, not a picture;
  `snapshot.py` still draws the case alone.
