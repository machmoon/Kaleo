# Judging notes — anticipated questions, honest answers

Prepared answers for the questions judges actually ask. Every claim below is
grounded in a file in this repository (or the named branch/release); where
something is pending or modest, it says so. Do not improve on these answers by
rounding up.

Naming, once: the product and desktop app are **Ada** (repo `machmoon/Ada`,
app v0.3.1). The engine package is still `silkscreen` — that is the engine's
name, and it is fine to say both.

---

## "How do you meet the three Google-stack requirements?"

### 1. Gemini 3.5+ via the Gemini API

Every worker model call funnels through `GeminiModel` in
`engine/silkscreen/agents/model.py` — it constructs a `google.genai` client and
issues `generate_content` requests, including native PDF document parts for
datasheet reading, now with a per-request deadline (`request_timeout_ms` in the
same file; the SDK sets none of its own). The tiering is `gemini-3.7-flash`
primary, `gemini-3.5-flash-lite` behind it, and a Gemma model as the
last-resort rung, assembled in `service/app.py:build_model()` as a
`FallbackModel` chain (`engine/silkscreen/agents/resilience.py`) that validates
each provider's output before accepting it. The web UI additionally offers
model selection with thinking-effort control, backed by live `models.list`
discovery at `GET /models` (`service/models.py`). Full per-requirement
analysis: `docs/gemini.md`.

(Demo-day operational note, distinct from the architecture claim: 3.7-flash
was measured degraded on 2026-08-31, so live demos pin `gemini-3.5-flash`.
The fallback chain is what makes that a selector click, not an outage.)

### 2. A Google agent framework — ADK is the default engine, not a checkbox

This is one of the two strongest claims, and it is worth saying precisely:
**the pipeline's default orchestrator is a Google ADK dynamic workflow.**

- The pipeline (read → propose/repair → schematic → place → route → review) is
  an ADK 2.8 workflow in `engine/silkscreen/agents/adk/` — `workflow.py`
  defines the stage nodes, `runner.py` runs a Runner and session per request.
- `generate_pcb` in `engine/silkscreen/agents/pipeline.py` dispatches on
  `SILKSCREEN_ENGINE`, and the default is `"adk"` (see the `chosen = engine or
  os.environ.get("SILKSCREEN_ENGINE", "") or "adk"` line). `sdk` is kept as a
  kill switch. The flip to ADK-by-default happened 2026-08-30 after a live
  end-to-end gate run (recorded in TODO.txt feature 12).
- Both drivers call the same stage bodies (`agents/stages.py`) and a parity
  suite (`engine/tests/test_adk.py`) asserts same events, same result, same
  exceptions on both engines — the ADK path is load-bearing, not decorative.
- The presentation path `POST /chat/stream` is a genuine ADK `LlmAgent` root
  orchestrator that may ask one clarification and otherwise invokes the
  validated generator as its `generate_board` tool.
- Underneath, the Google GenAI SDK (`google-genai`) is the only model
  interface — also on the accepted framework list, so the claim holds twice
  over.

Analysis and history (including the team's earlier decision *against* ADK and
the reasoned reversal): `docs/agent-framework.md` and TODO.txt features 12–13.

### 3. Google Cloud infrastructure — deployed, and currently down

- **Cloud Run**: a public instance was deployed 2026-08-31 to
  <https://silkscreen-vqdj4x5qbq-uc.a.run.app> (Gemini key from Secret Manager —
  see the README's deployment section). On that date `GET /` served the built web
  UI publicly (200) and `POST /generate` enforced a shared access token (401
  without it), so browsing to it cost nobody anything. **It is not serving now.**
  Checked 2026-09-06: `/readyz` → 500, `/` → 503, unhealthy since 2026-09-05.
  A recorded deploy is not a running one, and this document should not be quoted
  as evidence of a live URL until someone has re-run
  `curl -s -o /dev/null -w '%{http_code}' <url>/readyz` and seen `200`. Redeploy
  with `scripts/deploy.sh`. Until then the judge-facing shape is **a downloadable
  desktop app plus a container that has been deployed and can be redeployed**,
  and the Cloud Run infrastructure requirement is met by `service/`, the
  `Dockerfile`, `scripts/deploy.sh` and the recorded deploy — not by a link
  anyone can click today.
- One operational quirk worth volunteering so nobody "disproves" the deploy:
  Google's edge intercepts `/healthz` on `run.app` domains and answers 404
  before the container sees the request. Liveness is `GET /` (or `/readyz`,
  added for exactly this reason in `service/app.py`, live after the next
  redeploy).
- **The service itself**: `service/app.py` is the HTTP surface (stdlib-only
  server: `/generate`, `/generate/stream`, `/chat/stream`, `/models`,
  `/config/status`, `/healthz`, `/readyz`, plus serving the built SPA
  same-origin). The root `Dockerfile` builds the image;
  `.github/workflows/docker.yml` publishes it to `ghcr.io/machmoon/silkscreen`.
- **Firestore**: `service/cache.py` is a datasheet-fact cache behind a
  `FactStore` protocol — real write-back, with cache hits feeding
  `preloaded_facts` into the pipeline so the second run on a part skips its
  most expensive stage. Tested against a fake client so the suite needs no GCP
  project.

**End-to-end over the cloud, verified 2026-08-31:** a full paid board
generation completed against the public URL — an AMS1117-3.3 LDO intent
returned a complete `.kicad_pcb` (10.55 × 11.8 mm, placement status optimal)
through the token-gated `POST /generate`. Honest detail worth volunteering:
the *first* cloud smoke failed on a known engine limit (the model
nondeterministically added a 2-pin connector, which has no package rule — the
same class fails locally, so it was never a deployment issue); the passing run
excluded connectors explicitly. Details: `docs/cloud-infrastructure.md`.

---

## "What in the demo is real, and what is scripted?"

**Live in the demo:** the model calls (Gemini, with a real key), the datasheet
read, the proposal and repair loop, the CP-SAT solve, the maze routing, the
emitted KiCad files, and the adversarial review's findings. The stage ticks in
the UI are driven only by events actually received over the NDJSON stream —
the frontend's honesty rules forbid inventing progress (see the UI notes in
CLAUDE.md).

**Deliberately not live:** nothing in the happy path. Two things to disclose
if they come up:

- The *test suite* runs against a `ScriptedModel`
  (`engine/silkscreen/agents/model.py`) so the whole pipeline, including
  failure paths, is testable with no key and no network. That is an
  architecture feature, not a demo trick — the demo itself uses the live model.
- Our disaster fallback is a session JSON recorded from a real run earlier the
  same day, restored via the SPA's **Open session**. If we use it on stage, we
  say so on stage.

**About the golden intent:** the AMS1117 prompt is chosen because a ceramic
output capacitor on that regulator is a genuine, datasheet-verifiable
stability issue, and the reviewer's prompt explicitly probes regulator output
capacitors and ESR (`engine/silkscreen/agents/review.py`). The *class* of
finding is reliable; the exact wording varies run to run because it is a live
model. The review is also refutation-prompted (an agent asked "is this
correct?" says yes), and findings naming parts the circuit does not contain
get those references stripped — the filter code is in the review stage, not in
the demo. The intent also explicitly excludes connectors and 2-pin parts,
because the model nondeterministically adds a 2-pin input connector and the
emitter has no package rule for 2-pin parts (fix in flight on
`fix/two-pin-package-rule`) — that exclusion is honesty about a real limit,
and we say so if asked.

---

## "What's the licensing story for the desktop app?"

The desktop overlay (**Ada**, v0.3.1) is a fork of Pluely, a GPL-3.0 Tauri
app. The rest of the repository is MIT. The boundary that keeps those
compatible is **process separation**:

- Ada talks to the engine over HTTP against the documented `/generate`,
  `/generate/stream`, `/transcribe`, and `/healthz` surface — exactly as a
  browser does. Two programs communicating, not one program in two languages.
  No code crosses the boundary in either direction, and the engine is
  independently useful and independently installable (`pip install` of an MIT
  package).
- The fork is documented in `app/NOTICE.md`: upstream repo and fork commit,
  the GPL-3.0 licence preserved verbatim, and — as GPL section 5 requires — a
  dated log of every modified file.
- We also cleaned the fork up on principle: upstream's PostHog telemetry was
  removed, its updater (pointing at upstream's update server) was removed, the
  network capability was narrowed from `http(s)://**` to **plain http on
  loopback only, plus any `https://` origin** — `app/src-tauri/capabilities/default.json`
  (mirrored in `cross-platform.json`) allows `http://127.0.0.1:*`,
  `http://localhost:*` and the single-segment `https://*` patterns, matching what
  `EngineConnection.tsx::validateEngineBaseUrl` accepts (since 2026-09-06; the
  2026-08-31 to 09-06 builds were loopback only). Say the second half carefully:
  the https grant is **unverified live** — no https request has been exercised
  against a built app, so whether the app can reach the Cloud Run instance above
  is not yet demonstrated (`app/NOTICE.md`, 2026-09-06 row). Upstream's screen-share
  invisibility
  (`contentProtected`) was deliberately turned **off** — we want the assistant
  visible in a shared design review, which is the opposite default of its
  lineage.

**Where it lives:** the app source (`app/`) is on the `feat/kaleo` branch —
PR #22, **merged into `main`** (`38ec898`). The shipped artifact
is real either way: `Ada_0.3.1_aarch64.dmg` is attached to the tagged GitHub
release (the README points at the tagged releases for downloads) alongside the
Python wheel and the web bundle, and the installed app does native
save-to-disk with a one-click **Open in KiCad** after saving.

---

## "Why does it matter that KiCad isn't required?"

Because the board file is the API. Most AI-and-KiCad tools are plugins: they
live inside KiCad's Python environment, need KiCad installed *and running*,
and inherit its version and platform constraints. Silkscreen reads and writes
the KiCad formats directly (`kiutils`, pure Python; format `20240108`, KiCad
7–8, which KiCad 10 opens fine), so it is headless-native, CI-native,
cross-platform with identical behavior, and — the part we care most about —
**fully testable**: the entire suite runs with no network, no API key, and no
KiCad install. When you *do* have KiCad, Ada's post-save **Open in KiCad**
button hands the project straight to it.

Two supporting points that preempt the obvious follow-ups:

- *"So it never checks against real KiCad?"* It does — as a verification gate,
  not a runtime dependency. `kicad-cli sch erc` and `pcb drc` are run on
  emitted output locally; the first DRC ever run found a real short (a via
  crossing a foreign track) that the entire test suite had passed over. The
  router's clearance handling was rebuilt because of it (TODO.txt feature 15).
- *Symbols and footprints are generated, not looked up* — the emitted project
  embeds its own `lib_symbols` and land patterns, so it opens on a machine
  with no KiCad libraries and cannot silently resolve to a different part
  (`engine/silkscreen/schematic.py`, `footprints.py`). The footprint generator
  covers a narrow set on purpose (chip passives, SOT-23/223, SOIC, LQFP) and
  raises rather than guessing at an unknown package — which is exactly why a
  2-pin connector in a proposal fails loudly instead of producing a wrong
  board.

---

## "What are the limits?" — say these before they're asked

- **The router is modest, by design and by admission.** Two-layer A* over a
  0.25 mm grid, nets routed sequentially, no rip-up-and-retry. On a dense LQFP
  board it finishes 6 of 50 nets. The contract is honesty: every unrouted net
  is named with the reason and left as ratsnest for a human in KiCad; a net is
  routed all-or-nothing so a board can never *look* finished where it isn't
  (`engine/silkscreen/routing.py`, README "Routing the copper").
- **Placement is `FEASIBLE`, not `OPTIMAL`, and we report it that way.** 2D
  packing with wirelength is NP-hard; within the budget CP-SAT returns a valid
  solution plus a genuine gap bound. Single-sided placement only; edge
  constraints don't control connector facing.
- **No package rule for 2-pin semiconductors or connectors.** A proposal
  containing one fails at board emission with a named error rather than
  guessing a land pattern. The model sometimes adds such a part unprompted, so
  demo intents exclude them explicitly; the fix is in flight
  (`fix/two-pin-package-rule`).
- **The review is not sign-off.** Model findings are graded and filtered, and
  the separate board-review tool (`silkscreen-review`) keeps *proven* findings
  (measured by deterministic checkers, shown with the measurement) apart from
  *suggested* ones (argued by the model, dashed on the render). Nothing the
  model says can delete or reword a proven finding. It catches real classes of
  mistakes with citations; it does not guarantee a working circuit — that gap
  is why `spice/` exists (passive-network verification against closed-form
  theory is real today; IC behavioral models are not).
- **Cloud end-to-end has exactly one verified run.** The Cloud Run instance
  is live, serving the UI publicly, token-gated on `POST /generate`, and has
  completed one full board generation over the public URL (2026-08-31). One
  passing smoke is one passing smoke — claim it as verified, not as load
  tested.
- **Voice.** Ada speaks the review digest aloud (ElevenLabs with a key, system
  voice otherwise — automatic fallback, degrades to silence rather than
  crashing a run) and takes spoken intent through the engine's `/transcribe`
  endpoint. The web SPA's mic button remains deliberately inert on `main`
  ("Voice input — not yet built" — `frontend/src/components/MicButton.svelte`);
  voice is a desktop-app feature.
- **Known issues are tracked, not hidden.** CLAUDE.md carries a deliberate
  known-issues list. The formerly demo-critical one — a hung model call with
  no request deadline — is fixed and merged (per-request deadline plus a
  Gemma last-resort rung); the demo fallback plan now guards degraded-tier
  slowness, not silent hangs.

---

## Rapid-fire answers

- **"Isn't this just prompting?"** No. The model proposes and reviews;
  everything between is deterministic and model-free — a validated circuit IR
  that collects every failure into one batched repair prompt
  (`engine/silkscreen/netlist.py`), a CP-SAT placer (`packing.py`), a grid
  router (`routing.py`), and direct KiCad file emission in integer nanometres.
  The layer boundary is physical: `agents/` is the only package that can make
  a network call.
- **"How do I verify any of this?"** Clone it. (Not "open the live URL" — see
  section 3: that instance is down as of 2026-09-06.) The full suite, the lint,
  the doc check, and an end-to-end placement demo run offline from a clean
  checkout with no network, no key and no KiCad; budget **around half an hour**
  for the suite, not the "four minutes" this line used to claim — it has grown
  from two test directories to eight. Install with the `cad` extra
  (`".[dev,agents,cloud,adk,cad]"`) or the thirteen enclosure-kernel clauses skip
  while the run still reports green. Open the emitted project in KiCad and run
  ERC/DRC yourself.
- **"Why Firestore and not a dict?"** The fact cache persists *across requests
  and instances*: extracted datasheet facts are the most expensive stage, and
  a second request for the same part skips it. The in-memory `FactStore`
  stand-in exists so tests need no GCP project — same protocol, same code
  path.
- **"What happens when Gemini is down?"** Every request carries a deadline, and
  `FallbackModel` fails over between rungs — 3.7 Flash, 3.5 Flash-Lite, then a
  Gemma last resort — with output validation at each hop; the SPA surfaces
  retries in the event stream. The engine below the model keeps working
  entirely offline.
- **"Who did the third-party code disclosure?"** `vendor/mudriknow/` (MIT,
  unmodified, reference-only for the guided-pointer design — the in-app
  pointer in `frontend/src/lib/guide.js` reimplements its consent-gate design,
  not its code) and the Pluely fork behind Ada (GPL-3.0, `app/NOTICE.md`).
  Both are disclosed in DEVPOST.
