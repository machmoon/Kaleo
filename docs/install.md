# Installing Silkscreen

The short version lives in the [README quickstart](../README.md#quickstart). This is
the long version: what each path actually does, what the API key is and is not needed
for, and what to do when something breaks.

- [What you need](#what-you-need)
- [Path 1 — the install script](#path-1--the-install-script)
- [Path 2 — pip and a venv, by hand](#path-2--pip-and-a-venv-by-hand)
- [Path 3 — Docker](#path-3--docker)
- [Extras: what to install](#extras-what-to-install)
- [GOOGLE_API_KEY: what it is for](#google_api_key-what-it-is-for)
- [The .env caveat](#the-env-caveat)
- [The web UI (Node 22+)](#the-web-ui-node-22)
- [KiCad (optional, but recommended)](#kicad-optional-but-recommended)
- [Verifying the install](#verifying-the-install)
- [Troubleshooting](#troubleshooting)

---

## What you need

| | |
|---|---|
| Python | 3.11 or newer (`python3 -V`) — 3.11 is what CI pins |
| OS | macOS, Linux, Windows. All three run in CI, with identical behaviour |
| Disk | ~400 MB for the venv, almost all of it OR-Tools |
| Network | For `pip install`, and for the prompt-to-PCB path. Nothing else |
| Node | Only for the web UI, and only **22 or newer** |
| KiCad | Never required to *run* Silkscreen; required to *look at* what it makes |
| API key | Only for the model-backed path. The engine and the whole test suite are key-free |

---

## Path 1 — the install script

```bash
git clone https://github.com/machmoon/silkscreen && cd silkscreen
./scripts/install.sh
```

On Windows:

```powershell
git clone https://github.com/machmoon/silkscreen
cd silkscreen
powershell -ExecutionPolicy Bypass -File scripts\install.ps1
```

What it does, in order: find a Python 3.11 or newer (probing each candidate's real
version rather than trusting its name), create `.venv` if it isn't there, `pip install
-e ".[dev,agents,cloud,adk,cad]"`, and — only if Node 22+ is on your PATH — `npm ci` and build
`frontend/dist`. A missing or too-old Node is reported and skipped, not fatal: the CLI
and the API do not need it.

It never uses `sudo` and writes nothing outside the repository, so a failed run leaves
nothing behind. Re-running is safe. Two flags:

| Flag | |
|---|---|
| `--no-web` / `-NoWeb` | Skip the frontend build entirely |
| `--dry-run` / `-DryRun` | Print the plan, change nothing |

It does not install KiCad, does not install Node, and does not ask for an API key —
that is `silkscreen setup`, below.

The install leaves these console commands in the venv, alongside `python -m silkscreen`:

| Command | What it does |
|---|---|
| `silkscreen "<what to build>"` | Generate a board — the same entry point as `python -m silkscreen` |
| `silkscreen setup` | Report what is installed and what is missing, ask once for the API key, and write `.env`. It never prints the key back |
| `silkscreen serve` | Load `.env`, start the service, and open the Ada desktop app (`--web` for the browser, `--no-browser` for neither; `--port`, or `PORT`) |
| `silkscreen-mcp` | The MCP server, JSON-RPC 2.0 over stdio |
| `silkscreen-review <board.kicad_pcb>` | Review an existing board |

`silkscreen serve` runs the service **out of the repository**: `service/` is not part of
the installed package (setuptools packages only `engine/`), so it must be run from an
editable install in a checkout, and it says so rather than raising an `ImportError`.

Activate the venv (`source .venv/bin/activate`, or `.venv\Scripts\Activate.ps1` on
Windows) to get those on your `PATH`; otherwise call them by full path, e.g.
`./.venv/bin/silkscreen-mcp`.

## Path 2 — pip and a venv, by hand

Identical result, three lines, nothing hidden:

```bash
python3 -m venv .venv
./.venv/bin/pip install -e ".[dev,agents,cloud,adk,cad]"
./.venv/bin/python -m pytest -q
```

Windows uses `.venv\Scripts\pip` and `.venv\Scripts\python.exe` throughout. Every
command in the README that starts `./.venv/bin/python` translates the same way.

Editable (`-e`) is the intended install: the package lives in `engine/`, and setuptools
packages only that directory, so an editable install is what makes `engine/` and your
checkout the same code.

## Path 3 — Docker

The root `Dockerfile` builds the web bundle in a Node 22 stage and copies it into a
Python 3.11 slim runtime, so the running container needs no Node at all:

```bash
docker build -t silkscreen .
docker run -p 8080:8080 -e GOOGLE_API_KEY=... silkscreen
```

`http://localhost:8080` then serves both the review UI and the API, same origin — which
is why there are no CORS headers anywhere in the service, and why none should be added.
`GET /healthz` is the readiness probe.

This is the service path only. To use the CLI, which is the supported way to design a
board, install with path 1 or 2.

---

## Extras: what to install

`pip install -e "."` gives you the engine alone — the placer, the KiCad file I/O, the
circuit IR, the footprint and board emitters. Everything else is an extra:

| Extra | Pulls in | Needed for |
|---|---|---|
| `dev` | pytest, ruff | Running the test suite and the linter |
| `agents` | `google-genai`, `pypdf` | The prompt-to-PCB path and datasheet reading |
| `adk` | `google-adk` | The ADK workflow driver (`SILKSCREEN_ENGINE=adk`) |
| `cloud` | `google-cloud-firestore` | The Firestore datasheet-fact cache used by the service |
| `cad` | `build123d` (OCCT), `numpy`, `pillow` | The B-rep enclosure kernel and its 13 acceptance clauses |
| `slack` | `+ pillow` | `python -m slackbot` (the optional PNG board render) |
| `zoom` | `+ websocket-client` | `python -m zoombot` (the RTMS receive half) |
| `teams` | `+ cryptography` | `python -m teamsbot` (Graph callback JWT validation) |
| `training` | `tinker`, `tinker-cookbook` | The experimental Tinker placement adapter |

The full set for development work is `".[dev,agents,cloud,adk,cad]"`, which is what the
reproducible-testing section of the README uses and what CI installs
(`.github/workflows/ci.yml:40`).

**Do not drop `cad` to save an install.** It is the B-rep kernel, and since 2026-09-08 it is
the *only* enclosure engine: without it the case step refuses in words naming this extra
rather than shipping a worse case (the v1 OpenSCAD emitter that used to be the fallback was
removed — `docs/ai-cad-plan.md` v3). The refusal is loud, but all thirteen kernel-clause
tests still **skip while the suite reports green** — the same failure mode as running
without ngspice, and the reason CI installs `cad` on every OS. A green run without `cad` is
not evidence the enclosure kernel works.

---

## `GOOGLE_API_KEY`: what it is for

Silkscreen is layered so that the deterministic parts never touch the network:

- **The engine** (`engine/silkscreen/` outside `agents/`) makes no model calls and no
  network calls at all. Placing, routing, emitting a board, reading a `.kicad_pcb`,
  generating footprints — none of it needs a key.
- **`agents/`** is the only place a model call lives. It needs a Gemini API key to talk
  to a real model, and ships a `ScriptedModel` stand-in so the tests do not.
- **`service/`** is the only place Google Cloud lives.

So: the **entire test suite runs offline with no key**, and so does `scripts/demo.py`.
A key is needed only when you ask Silkscreen to design a circuit from a prompt.

Get one from [Google AI Studio](https://aistudio.google.com/apikey), then either:

```bash
./.venv/bin/silkscreen setup    # asks once, writes .env, never echoes the key
cp .env.example .env            # or do it by hand: set GOOGLE_API_KEY=...
```

A handful of tests in `engine/tests/test_live_model.py` make a real, cheap model call —
they are gated on the key being present and **skip by default**, so the default suite
stays offline and free.

### The `.env` caveat

**Only the CLIs read `.env`, and not all of them.** `cli.py` has a small `_load_dotenv`
that loads the file from the current working directory; `python -m silkscreen`,
`silkscreen-review`, `python -m slackbot` (`slackbot/config.py:99`) and
`python -m googleapps` (`googleapps/config.py:116`) all go through it, and
`silkscreen serve` loads it separately via `onboard.load_dotenv` (`serve.py:74`).
**`service/app.py` does not** — it reads the process environment only. Neither do
`python -m zoombot` and `python -m teamsbot`, even though `.env.example` documents
fourteen of their variables; export those into the shell the same way. So this works:

```bash
python -m silkscreen "..." -o board.kicad_pcb      # picks up .env
```

and this silently starts a service with no key:

```bash
python -m service.app                              # does NOT read .env
```

`silkscreen serve` works around this by loading `.env` itself before starting the
server, which is why it is the easy way to run the app. If you start the service
module directly, export the key yourself:

```bash
set -a && . ./.env && set +a && PORT=8081 python -m service.app     # bash/zsh
```

```powershell
$env:GOOGLE_API_KEY = "..."; python -m service.app                  # PowerShell
```

A service running without a key answers `/generate` with a 502 whose message names
`GOOGLE_API_KEY`; the web UI recognises that specific error and renders setup
instructions rather than an outage.

The right side rail polls `GET /config/status` every five seconds. It shows the
configuration the running backend can actually use, verifies Gemini through cached
model discovery, and checks a configured Ollama endpoint and model. If `.env` changes
without changing the process environment, the rail names only the affected variable
names and asks for a restart; secret values never leave the backend. Tinker and
Firestore are checked for the settings and optional dependencies they require, without
making a paid model call.

### Other environment variables

| Variable | Read by | Meaning |
|---|---|---|
| `GOOGLE_API_KEY` | `agents/` | Gemini API key |
| `GOOGLE_CLOUD_PROJECT` | `service/` | Firestore project for the fact cache |
| `USE_FIRESTORE` | `service/app.py` | Enables the Firestore-backed fact cache |
| `SILKSCREEN_ENGINE` | `agents/` | `adk` or `sdk` — which pipeline driver runs |
| `SILKSCREEN_WEB_DIST` | `service/app.py` | Override the built web bundle's location |
| `PORT` | `service/app.py` | Listen port (default 8080) |

`.env.example` used to list `GOOGLE_CLOUD_LOCATION`, which nothing read — there is no
Vertex AI path. It was removed on 2026-09-06, along with the omission of `USE_FIRESTORE`,
which `service/app.py` does read and which the file now documents.

---

## The web UI (Node 22+)

```bash
node --version    # must be v22 or newer
```

Vite 8 refuses to start on anything older, and CI pins Node 22. The UI is a Svelte 5 +
Vite plain-JS SPA in `frontend/` — no SvelteKit, no TypeScript.

**Development** — two terminals, with Vite proxying `/generate` and `/healthz` to the
Python service:

```bash
PORT=8081 python -m service.app             # terminal 1
cd frontend && npm install && npm run dev   # terminal 2 → http://localhost:5173
```

**Production** — build the bundle and let the Python service serve it same-origin:

```bash
cd frontend && npm run build   # writes frontend/dist/
python -m service.app          # http://localhost:8080 serves UI and API
```

**Its tests:**

```bash
cd frontend && npm test        # Vitest over frontend/src/lib
```

---

## KiCad (optional, but recommended)

Silkscreen writes KiCad's file formats itself with `kiutils`, a pure-Python parser. It
never launches KiCad, imports `pcbnew`, or drives the IPC API. You need KiCad only to
open, check, and fabricate the result — which, unless you are running Silkscreen inside
another tool, you will want to do.

| Platform | Command |
|---|---|
| macOS | `brew install --cask kicad` |
| Windows | `winget install KiCad.KiCad` |
| Debian/Ubuntu | `sudo add-apt-repository ppa:kicad/kicad-8.0-releases && sudo apt install kicad` |
| Fedora / any Linux | `flatpak install flathub org.kicad.KiCad` |

Or download from [kicad.org/download](https://www.kicad.org/download/). Open the output
with **File → Open** in the PCB Editor, or `pcbnew placed.kicad_pcb`. Silkscreen writes
board format `20240108`, which is KiCad 7–8; older KiCad will not read it.

---

## Verifying the install

Run the same checks CI runs, in the same order:

```bash
./.venv/bin/python -m pytest -q                            # tests      (~30 min)
./.venv/bin/python -m ruff check \
    engine service scripts desktop googleapps zoombot teamsbot  # lint  (~2 s)
./.venv/bin/python scripts/check_docs.py                   # doc drift  (~5 s)
./.venv/bin/python scripts/demo.py                         # end-to-end (~20 s)
```

`scripts/demo.py` is the best single smoke test: it reads the 11-footprint fixture
board, solves a placement, writes a real `.kicad_pcb`, and re-parses it to prove the
round-trip — with no key and no network.

---

## Troubleshooting

**`pip install` is slow, or OR-Tools fails to build.**
OR-Tools is ~400 MB of wheels and is the bulk of the install. It publishes wheels for
CPython 3.11+ on macOS/Linux/Windows; if pip starts building from source, you are on an
unsupported Python (check `python3 -V`) or an unusual platform.

**`ModuleNotFoundError: silkscreen`.**
You are running the system Python rather than the venv's. Use `./.venv/bin/python`
(`.venv\Scripts\python.exe` on Windows), or activate the venv.

**`ModelError` about a missing `GOOGLE_API_KEY`.**
Expected without a key. Only the model path raises it — placement, routing, board
emission and the whole test suite do not. If you set it in `.env` and are running the
*service*, see [the `.env` caveat](#the-env-caveat): the service does not read `.env`.

**The web UI shows setup instructions instead of a result.**
Same cause: the service is running without a key. That screen is deliberate — a 502
mentioning `GOOGLE_API_KEY` is rendered as a setup problem, not an outage.

**`npm run dev` fails immediately, or Vite complains about the Node version.**
Node is older than 22. `node --version`, then upgrade.

**The dev server behaves impossibly** — an empty pipeline feed and debug console, or the
view stuck on the intent form while a run completes invisibly.
Restart the Vite dev server. After heavy on-disk churn a long HMR history can serve one
module under two `?t=` stamps in the same page, splitting singletons like the run store
into duplicate instances. Check
`performance.getEntriesByType('resource')` for duplicate `/src/lib/run.js` URLs. The
built bundle served by the Python service is immune to this.

**Port 8080 is already taken.**
`PORT=8081 python -m service.app` (`silkscreen serve` already defaults to 8081). The
frontend dev proxy expects `127.0.0.1:8081`.

**`silkscreen serve` says there is no `service/app.py`.**
You are running it from an installed wheel rather than from an editable install in a
checkout. `service/` is not packaged — only `engine/` is. Install with
`scripts/install.sh` (or `pip install -e .`) and serve from the repository.

**The placer returns `FALLBACK`, or results differ between runs.**
Not an install problem — see the README's
[troubleshooting table](../README.md#troubleshooting). Determinism requires `workers=1`,
which is the default.

**KiCad will not open the output.**
Check the version line at the top of the file: Silkscreen writes `20240108` (KiCad 7–8).
KiCad 6 and earlier cannot read it.
