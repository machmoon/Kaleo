# Repository Guidelines

## Project Structure & Module Organization

The Python package and tests live in `engine/silkscreen/` and `engine/tests/`.
Model integrations belong in `engine/silkscreen/agents/`; `service/` contains
the HTTP/Cloud Run layer and its tests. The Svelte 5 + Vite app lives in
`frontend/`, with colocated `src/**/*.test.js` tests. Use `scripts/` for CI and
demo utilities, and `docs/` or `design/` for documentation and design sources.

Top-level `mcp/`, `packing/`, `pcb/`, `footprint/`, `frontend-archive/`, and
`test_skidl.py` are retired code. Do not extend them. Treat `vendor/mudriknow/`
as read-only reference code. Read `CONTRIBUTING.md` before changes.

## Build, Test, and Development Commands

- `./scripts/install.sh`: create `.venv`, install Python extras, and build the UI
  when Node 22+ is available.
- `./.venv/bin/python -m pytest -q`: run the offline Python test suite.
- `./.venv/bin/python -m ruff check engine service scripts`: lint live Python.
- `./.venv/bin/python scripts/check_docs.py`: detect documentation drift.
- `./.venv/bin/python scripts/demo.py`: exercise the end-to-end board pipeline.
- `cd frontend && npm test && npm run build`: test and bundle frontend changes.
- `cd frontend && npm run dev`: start the Vite development server.

## Coding Style & Naming Conventions

Follow `.editorconfig`: four spaces by default and two in web files. Ruff targets
Python 3.11, an 88-character line limit, and import ordering. Use `snake_case`
for Python modules/functions and `PascalCase` for classes and Svelte components.

PCB dimensions must remain integer nanometres; convert only through `units.py`.
Keep coordinate-frame conversion in the established boundaries and preserve
deterministic CP-SAT defaults (`workers=1`). Never reformat generated KiCad files.

## Testing Guidelines

Name Python files `test_*.py` and frontend tests `*.test.js`. Add regressions
beside the affected layer; use independent expected-value logic for geometry
bugs. Run a single test with
`python -m pytest engine/tests/test_packing.py -q` or `pytest -k test_name -q`.
Every behavioral change needs a regression test; the suite must remain offline
and deterministic.

## Commit & Pull Request Guidelines

Recent commits use concise, imperative, sentence-case subjects such as
`Add approved constraint verification`; keep each commit focused. Branch from
`main`, fetch before starting and pushing, and rebase to avoid divergent history.
PRs should explain intent and verification, link relevant issues or `TODO.txt`
items, and include screenshots for UI changes. Run the checks above and address
automated review findings with a fix or evidence.

## Security & Configuration

Copy `.env.example` to `.env`; never commit API keys or generated board outputs.
Keep engine tests key-free and network-free, and preserve the existing layer
boundary: model calls in `agents/`, cloud dependencies in `service/`.
