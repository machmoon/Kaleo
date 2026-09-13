# Repo layout: what is alive, what is not

The repo root mixes the live tree with retired hackathon-era code, vendored
reference code, and a layer of untracked build droppings. Three of the retired
directory names collide with live module names, so "delete the old packing
code" is a genuinely dangerous instruction to hand to a script. This file is
the reference for telling them apart.

> **Status, 2026-09-06: this is a dated snapshot, not a current inventory.** It
> was verified on 2026-08-30 and the tree has moved a long way since. Re-run the
> commands below before acting on anything here; specifically:
>
> - The retired directories are **being removed**, and the removal is not
>   committed. `git ls-tree origin/main` and `git ls-tree HEAD` still list all
>   seven; a working tree that has them deleted shows the deletions unstaged in
>   `git status`. So the `git rm` recipe below may be a no-op, may be exactly
>   right, or may duplicate someone else's in-flight change — **check
>   `git ls-files` first**, which is the point of this document.
> - Every file count in this document is from 2026-08-30 and is now low.
> - Ruff is pointed at **seven** directories, not three
>   (`ci.yml:41`: `engine service scripts desktop googleapps zoombot teamsbot`).
> - `testpaths` is **eight** directories, not two (`pyproject.toml:59-68`).
> - The live tree has grown several top-level packages this table does not name:
>   `app/` (the Kaleo desktop overlay, and the live one), `slackbot/`,
>   `meetings/`, `googleapps/`, `zoombot/`, `teamsbot/`, `billing/`.
> - `desktop/` is **not** uniformly retired: `desktop/kicad_live.py` is live and
>   spawned by path from `service/steps.py:252`, while `desktop/launcher.py`,
>   `desktop/sidecar.py`, `desktop/src-tauri/` and `desktop/silkscreen-app` are
>   the pre-Kaleo shell.
> - The guided cursor is no longer unbuilt in the way this file says: the
>   in-webview pointer ships (`frontend/src/lib/guide.js`,
>   `GuidePointer.svelte`); only the OS-level overlay is absent.

Everything below was verified against the working tree on 2026-08-30 by
grepping `engine/`, `service/`, `scripts/`, and `frontend/` for imports and by
counting tracked files with `git ls-files`. Where a claim is checkable, the
command that checks it is given — re-run them rather than trusting the prose.

## The live tree

| Path | What it is |
| --- | --- |
| `engine/` | The installed `silkscreen` package (58 tracked files) plus `engine/tests/`. `pyproject.toml`'s `packages.find` looks **only** here — nothing outside `engine/` ships. |
| `service/` | The Cloud Run surface: stdlib HTTP server over the agents layer, plus the Firestore fact cache. |
| `frontend/` | The live Svelte 5 + Vite review SPA (60 tracked files). |
| `scripts/` | Dev and CI scripts (`demo.py`, `check_docs.py`, and in-flight packaging work). |
| `docs/`, `design/` | Prose and design sources. Neither is imported or served. |

Newer top-level directories may appear as packaging and distribution work
lands; they are not covered here. This file's job is the alive/retired split.

## Retired pre-rewrite code — tracked, dead, safe to delete

These are hackathon-era code from before the engine rewrite: a FastAPI
datasheet-to-SKiDL server, KiCad-9-DLL scripts, and a Next.js frontend. They
are not linted (ruff is pointed at `engine service scripts`), not packaged, not
tested, and not imported.

| Path | Tracked files | Was |
| --- | --- | --- |
| `mcp/` | 9 | FastAPI + LLM symbol-search server, with its own Dockerfile |
| `pcb/` | 2 | KiCad-9 DLL board generation, OR-Tools in a subprocess |
| `packing/` | 2 | Early rectangle packer and constraints |
| `footprint/` | 2 | Autorouting experiments |
| `frontend-archive/` | 27 | The retired Next.js app |
| `lcsc.py` | 1 | LCSC part scraper |
| `test_skidl.py` | 1 | SKiDL scratch script (not a pytest file) |

### The three name collisions

This is the part that bites. **The retired directory is not the live module:**

- top-level `mcp/` is **not** `engine/silkscreen/mcp/` — the live MCP server
- top-level `packing/` is **not** `engine/silkscreen/packing.py` — the live CP-SAT placer
- top-level `footprint/` is **not** `engine/silkscreen/footprints.py` — live

Delete by explicit root-relative path, one command, never by glob and never
from a script:

```sh
git rm -r --quiet mcp pcb packing footprint frontend-archive
git rm --quiet lcsc.py test_skidl.py
```

### How the deadness was established

Not by trusting this file or CLAUDE.md. Three independent checks agree:

```sh
# 1. Nothing in the live tree imports any of them.
grep -rnE '^\s*(from|import)\s+(backend|mcp|pcb|packing|footprint|lcsc|skidl)\b' \
    engine service scripts        # -> no matches

# 2. No config or code references the retired trees by path.
grep -rnE '(frontend-archive|lib_pickle_dir)/' \
    --include='*.py' --include='*.toml' --include='*.yml' \
    engine service scripts pyproject.toml .github    # -> no matches

# 3. pyproject.toml packages only engine/, and pytest's testpaths are
#    engine/tests, service/tests — the retired trees are collected by neither.
```

`.dockerignore` is a fourth, independent witness: it names
`frontend-archive/`, `backend/`, `mcp/`, `pcb/`, `packing/`, `footprint/`,
`lib_pickle_dir/`, `lcsc.py`, `test_skidl.py`, and `test_skidl_sklib.py`
explicitly as things that must not reach the build context. Its list and the
grep results match exactly.

## Vendored reference code — do not delete

`vendor/mudriknow/` (124 tracked files) is MudrikNow, MIT-licensed, copied
unmodified at upstream `ad58192` as a read-only reference for the guided-cursor
feature — whose in-webview half now ships (`frontend/src/lib/guide.js`,
`GuidePointer.svelte`) as a fresh Svelte implementation of MudrikNow's *design*,
with only the OS-level overlay still unbuilt. Nothing imports the vendored code;
ruff and pytest skip it.

It is **disclosed in DEVPOST under "Third-party code"**, so removing it while
that disclosure stands is an attribution problem, not a cleanup. It is also
excluded from `.gitattributes` normalisation and `.editorconfig` trimming for
the same reason: it must stay byte-identical to upstream. Do not count it in
project metrics.

## Untracked droppings — ignored, but still clutter

None of the following is tracked (`git ls-files` returns nothing for any of
them) and all are matched by `.gitignore`, so `git status` stays clean whether
or not you delete them. They are pure `ls` noise — and the reason the root
looks abandoned at first glance.

| Path | Ignored by | Notes |
| --- | --- | --- |
| `__pycache__/`, `.pytest_cache/`, `.ruff_cache/` | Python section | regenerated |
| `placed.kicad_pcb` | Generated boards | `scripts/demo.py` output |
| `lib_pickle_dir/` | SKiDL section | SKiDL library cache |
| `skidl_REPL.*`, `test_skidl.*` (`.erc`/`.log`/`.net`) | `*.erc`, `*.log`, `*.net` | SKiDL run output |
| `test_skidl_sklib.py` | `*_sklib.py` | generated, despite the name |
| `backend/` | `C[0-9]*.json`, `C[0-9]*.jpg`, `adjacency_*.json`, … | ~46 MB of scraped LCSC parts |
| `frontend/silkscreen/` | *nothing* | see below |

### `frontend/silkscreen/` is deliberately not ignored

A half-scaffolded Next.js husk (~506 MB) sitting **inside** the live frontend:
it has `.next/`, `node_modules/`, `public/`, and `next-env.d.ts` but **no
`package.json`**, so it cannot build, and nothing references it. It is the
single most confusing thing in the tree, purely because of where it sits.

It is intentionally left out of `.gitignore`. An ignore rule would hide it from
`git status` and make it permanent; leaving it visible is the pressure to
delete it. It is untracked, so `rm -rf frontend/silkscreen` loses nothing.

## What the ignore rules do and do not do

`.gitignore` only stops **new** junk from being committed. It has no effect on
anything already tracked — an ignore rule added for a tracked file does
nothing until `git rm --cached` untracks it.

As of 2026-08-30 no such case exists: every pattern in `.gitignore` was checked
against `git ls-files` and none matches a tracked file, so **no
`git rm --cached` is needed anywhere**. Re-check with:

```sh
git ls-files | git check-ignore --stdin    # -> no output
```

Companion files, all deliberately narrow:

- **`.gitattributes`** — no global `* text=auto`. Board files are
  `text eol=lf`, not `-text`, so placement changes stay reviewable as
  s-expression diffs instead of "binary files differ".
- **`.editorconfig`** — editor defaults only; it enforces nothing. Python
  settings agree with ruff's `line-length = 88`, and ruff wins on conflict.
  Board files and `vendor/` are exempted from whitespace trimming.
