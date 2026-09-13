# Contributing to Silkscreen

Bug reports, board files that break the parser, and footprint coverage are all welcome.
This file is short on process and long on the conventions a newcomer would otherwise
violate by accident — most of them exist because the bug they prevent is silent.

---

## Setup

```bash
git clone https://github.com/machmoon/silkscreen && cd silkscreen
python3 -m venv .venv
./.venv/bin/pip install -e ".[dev,agents,cloud,adk]"
```

Windows uses `.venv\Scripts\pip` and `.venv\Scripts\python.exe`. The long-form guide,
including the Docker and script paths, is [docs/install.md](docs/install.md).

No API key and no KiCad install are needed to develop or test Silkscreen.

## The checks

Run all four before opening a PR. These are exactly what CI's `test` job runs, in
order, on Ubuntu, macOS and Windows against Python 3.11:

```bash
./.venv/bin/python -m pytest -q                            # 1. tests
./.venv/bin/python -m ruff check engine service scripts desktop # 2. lint
./.venv/bin/python scripts/check_docs.py                   # 3. doc drift
./.venv/bin/python scripts/demo.py                         # 4. end-to-end
```

Narrower runs while you work:

```bash
./.venv/bin/python -m pytest engine/tests/test_packing.py -q   # one file
./.venv/bin/python -m pytest -k test_name -q                   # one test
```

If you touched `frontend/`, run its suite too — CI's `web` job runs it before the
build, on Node 22 or newer:

```bash
cd frontend && npm test
cd frontend && npm run build
```

If you touched `desktop/src-tauri/`, run the macOS desktop gate:

```bash
cargo fmt --manifest-path desktop/src-tauri/Cargo.toml -- --check
cargo clippy --manifest-path desktop/src-tauri/Cargo.toml --locked --all-targets -- -D warnings
cargo test --manifest-path desktop/src-tauri/Cargo.toml --locked --all-targets
```

If you touched the `Dockerfile`, `docker build .` is the only thing that exercises it,
and CI's `docker` job does exactly that.

## Pull requests

- Branch off `main`; do not commit to `main` directly.
- Several people push to `main` concurrently, so `git fetch` and check `git log
  origin/main` before you start and again before you push. Rebase rather than letting
  the histories diverge.
- A [Greptile](https://greptile.com) bot reviews every PR automatically. Its P1
  findings have a real hit rate on this repo, so answer them: fix, or refute on the
  thread with evidence, or record it in `TODO.txt`. Verify before either — reproduce
  the claim against the actual code, since findings have also been flatly wrong.
- Open feature work is tracked in `TODO.txt`. RAG/retrieval (`agents/retrieval.py`,
  `agents/grounding.py`) is an owned lane — coordinate before picking it up.

---

## Conventions that are not negotiable

These are the ones that break boards silently. A violation usually does not raise, and
the run reports success.

### 1. Every dimension is an integer nanometre

Nanometres are KiCad's own internal unit, and they are the only unit that flows through
the pipeline. **No floats.** Unit confusion between mm, mils and nm is a
board-destroying class of bug that nothing downstream can detect, so conversion happens
at the edges only, through `units.py` (`mm()`, `mil()`, `to_mm()`).

If you find yourself writing `0.25` for a clearance, you want `mm(0.25)`.

### 2. Coordinate frames flip in exactly one place

The solver works **Y-up**. KiCad is **Y-down**. `kicad.py` owns the flip at both
boundaries — pad offsets on the way in, placements on the way out — and **nothing else
may convert**. The same rule crosses the HTTP boundary: the service serialises
placements in the solver's Y-up frame, and `frontend/src/lib/board.js` does the flip
exactly once, in the SVG group transform. Nothing downstream of that may flip again.

Two flips look correct on a symmetric board and are wrong on every other one.

### 3. Identity problems raise; they never no-op

`extract_parts` and `apply_placements` share one identity rule (`_placer_ref`). A
duplicate reference designator, a footprint with no ref at all, or an `edge_refs` entry
naming a part that is not on the board is a hard error — not a silently skipped
constraint. Keep it that way: a missing constraint produces a board that looks placed
and is not.

### 4. Regression tests must not share the blind spot

The bug class this project fears most is "the solver reserved a box that doesn't match
the part that was written" — the run succeeds and the opened board has parts stacked on
each other. Tests for geometry therefore compute expected values from the raw board
file with independent maths that never calls `kicad.py` (see the overlap truth function
in `engine/tests/test_kicad.py`). A check written in terms of the code under test
inherits its blind spot.

### 5. Determinism means `workers=1`

CP-SAT's multi-worker portfolio interleaves results non-deterministically regardless of
seed. `workers=1` is the default and the determinism test asserts it. Do not raise it
in library defaults to make something faster.

### 6. The layers do not leak

- The engine makes **no network calls** and holds no model code.
- `agents/` is the only place a model call lives.
- `service/` is the only place Google Cloud lives.
- Each layer keeps an offline stand-in (`ScriptedModel`, `MemoryFactStore`) so the whole
  suite runs with no keys. A new dependency that breaks the offline property is a
  design change, not a detail.

### 7. Docs discipline

- **Never quote a test count in prose.** `scripts/check_docs.py` checks every "N tests"
  claim in `README.md` and `DEVPOST.md` against pytest's own collection count, and that
  number has drifted repeatedly. `check_docs.py --fix` rewrites stale ones in place.
  There is no `MODULES` entry for `frontend/`, so a Vitest count quoted in either file
  would be compared against the Python suite total and fail CI.
- The README's measured figures (board size, HPWL) are reproduced exactly by
  `scripts/demo.py`. If your change moves them, rerun the demo and update the README
  rather than leaving stale numbers.
- Unbuilt features stay visibly unbuilt: `[not yet built]` in `DEVPOST.md`, "Not built"
  in the README status table. Do not promote something to done ahead of the code.

### 8. Service HTTP surface

`/generate`'s response is **additive only** — `blockers` is the compatibility surface.
There are deliberately no CORS headers and no `do_OPTIONS` (the UI is served
same-origin), and content types come from the explicit `_CONTENT_TYPES` table, never
from `mimetypes` — the Windows registry maps `.js` to `text/plain`.

### 9. Frontend conventions

`data-testid` goes on intrinsic elements only, never on component tags, since
components destructure `$props()` without spreading rest props. A repeated row shares
one id across instances, disambiguated by `data-ref` or `data-sev` rather than an index
suffix. Log strings go through the scrub pipeline in `src/lib/log.js` — never write to
`msg` or to an export directly. Colours are tokens in `src/styles/tokens.css`; nothing
hardcodes a board hex value.

---

## Directories that are not the live code

The repo still contains pre-rewrite hackathon code. It is not linted, not packaged, not
tested, and nothing in `engine/`, `service/`, `scripts/` or the tests imports it. **Do
not add code to it, and do not edit it expecting an effect.**

| Retired | The live code is |
|---|---|
| top-level `mcp/` | `engine/silkscreen/mcp/` |
| top-level `packing/` | `engine/silkscreen/packing.py` |
| top-level `footprint/` | `engine/silkscreen/footprints.py` |
| top-level `pcb/` | `engine/silkscreen/board.py`, `kicad.py` |
| `frontend-archive/` | `frontend/` |
| `lcsc.py`, `test_skidl.py` | nothing — dead |

The name collisions are the trap: `mcp/` is not `engine/silkscreen/mcp/`.

`vendor/mudriknow/` is third-party MIT code, copied unmodified as a **read-only**
reference for an unbuilt feature. Ruff and pytest skip it. Do not modify it, do not
import from it, and do not count it in project metrics.

---

## Where things live

```
engine/silkscreen/   the installed package: placer, IR, KiCad I/O, emitters, CLI
  agents/            the only place a model call happens
  audit/             optional review of a finished board
  mcp/               MCP server over stdio
engine/tests/        the engine suite; fixtures/ref.kicad_pcb is the shared fixture
service/             Cloud Run surface: stdlib HTTP server + Firestore fact cache
service/tests/       the service suite
frontend/            Svelte 5 + Vite review UI (plain JS, no SvelteKit, no TypeScript)
desktop/             Chromium fallback, Python sidecar, and native Tauri host
scripts/             demo.py, check_docs.py
docs/                install guide and the hackathon-requirement analyses
```

`CLAUDE.md` at the repo root is the deep version of this file — the full architecture
notes, the recorded known issues, and the team decisions behind them. It is worth
reading before a first non-trivial change.

## License

By contributing you agree that your contribution is licensed under the MIT License, the
same as the rest of the project.
