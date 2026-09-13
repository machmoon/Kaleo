## What this changes

<!-- One paragraph. What is different after this lands, and why. -->

## Checks

- [ ] `python -m pytest -q`
- [ ] `python -m ruff check engine service scripts`
- [ ] `python scripts/check_docs.py`
- [ ] `cd frontend && npm test` (only if `frontend/` changed)

## Conventions

- [ ] Dimensions are integer nanometres; no floats entered the pipeline
- [ ] No new coordinate flip — Y-up/Y-down conversion still happens only in `kicad.py`
      (and once, in the SVG group transform, on the web side)
- [ ] Identity and constraint errors raise rather than silently no-op
- [ ] No test count quoted in prose (`scripts/check_docs.py` enforces this)
- [ ] Unbuilt work is still marked unbuilt in README/DEVPOST
- [ ] Nothing was added to the retired top-level directories (`mcp/`, `pcb/`,
      `packing/`, `footprint/`, `frontend-archive/`) or to `vendor/`

## Measured figures

<!-- If this moves the demo's board size or HPWL, rerun `python scripts/demo.py` and
     update the README with what it printed. Delete this section if not applicable. -->
