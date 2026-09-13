# The 3-minute demo

> **QA-verified model-tier warning (re-verified 2026-08-31, demo eve):**
> `gemini-3.7-flash` (the default primary) was measured degraded today. The
> request-timeout fix is now merged (per-request deadline + Gemma last-resort
> rung, PRs #41/#42), so a hung primary times out and fails over instead of
> sitting silently — but failover still costs minutes you do not have on stage.
> **Pin `gemini-3.5-flash` for every live run on the CLI**
> (`--model gemini-3.5-flash`). **You cannot do this in the web/Ada model
> selector:** `IntentForm.svelte:25,30` offers only `gemini-3.7-flash` and
> `gemini-3.1-pro-preview`, and that selector picks the *orchestrator* model, not
> the worker the datasheet and proposal stages use. So a web demo runs on the
> default tier whatever you click — plan the risky beats on the CLI, or accept the
> failover cost.
>
> **The 2-pin trap (upgraded today):** any 2-pin semiconductor or connector
> crashes board emission — there is no package rule for 2-pin parts
> (`No package rule for 'J_IN' with 2 pins`, observed live on the cloud
> instance today). This is NOT just "don't ad-lib LED intents": the model
> **nondeterministically adds 2-pin connectors (J_IN) even to the golden
> AMS1117 intent**. Until the fix on `fix/two-pin-package-rule` merges, every
> live intent must end with an explicit exclusion — "use only the regulator
> and capacitors; no connectors, headers, LEDs, diodes, or test points." Stick
> to the scripted intents verbatim.

The judge-facing live demo, timed. Every click here exists in shipped code;
every line you say is backed by a file. The disaster plan for each step is at
the bottom — read it once before you go on stage, because the recovery moves
only work if you rehearsed them.

**One-line pitch:** describe a board out loud, watch an agent pipeline read the
datasheet, design it, place it, route it, and then argue against its own work —
and walk away with a real KiCad project.

---

## Setup checklist (T minus 30 minutes)

Do these in order. The demo has no live-fixable failure mode for a skipped step.

1. `git fetch` and confirm you are on the demo commit. Four people push here.
2. `cd frontend && npm run build` — demo the **built** bundle, never `npm run dev`.
   The Vite dev server has a known split-module failure mode after heavy churn
   (documented in CLAUDE.md); the built bundle served by the service is immune.
3. Start the engine with the key loaded, **naming the port explicitly**:
   `./.venv/bin/silkscreen serve --port 8081` (it reads `.env`; bare
   `python -m service.app` does not). `serve.py` defaults to **8081** since
   2026-09-06 (`DEFAULT_PORT`), so a bare `silkscreen serve` also lands here;
   `--port` or `PORT` override it. Then confirm
   `http://127.0.0.1:8081/healthz` — locally `/healthz` works fine; the
   404-at-the-edge gotcha (below) is cloud-only.
4. Launch **Ada** (installed at `/Applications/Ada.app`, v0.3.1, from
   `Ada_0.3.1_aarch64.dmg` on the tagged GitHub release — **Apple silicon
   only**; do not plan this demo on an Intel Mac). In its Engine page, set the
   base URL to `http://127.0.0.1:8081` and watch the health check go green.
5. Voice out: flip the voice toggle on. With an ElevenLabs key in Ada's voice
   settings you get the good voice; with no key it uses the system voice — the
   fallback is automatic. Play one test run at venue volume.
6. Voice in (optional): Ada's spoken intent goes through the engine's
   `/transcribe` endpoint. **If you have not tested it in this room, type the
   intent instead.** Typing is not a downgrade; the pipeline is the show.
7. **Record the golden session now.** Run the golden intent once in the web SPA,
   let it finish, click **Save session**. That JSON file is your no-network,
   no-model, no-luck fallback — **Open session** restores the full transcript,
   trace, findings, and board artifact locally.
8. Pre-generate the KiCad beat: run the CLI once and keep the output directory:
   `./.venv/bin/silkscreen "<golden intent>" --model gemini-3.5-flash -o demo/board.kicad_pcb`.
   You get `board.kicad_pro`, `board.kicad_sch`, `board.placed.kicad_pcb`,
   `board.kicad_pcb`. Open `board.kicad_pro` in KiCad now
   (`/Applications/KiCad/KiCad.app`, KiCad 10 — it opens the emitted KiCad 7–8
   format fine) and leave it in the Dock.
9. Cloud liveness check — **this is currently a blocker, not a formality.**
   As of 2026-09-06 <https://silkscreen-vqdj4x5qbq-uc.a.run.app> is **down**:
   `/readyz` answers 500, `/` answers 503, and it has been unhealthy since
   2026-09-05. Redeploy (`scripts/deploy.sh`, then
   `scripts/smoke.sh --health-only <url>`) and confirm `GET /` renders the web UI
   **before** you plan any cloud beat; if it is still down, cut the cloud beat and
   say the service is deployed but not currently up rather than clicking a dead
   link on stage. **Do not probe `/healthz` on the cloud URL** in any case:
   Google's edge intercepts `/healthz` on `run.app` domains and answers 404 before
   the container sees it, so a 404 there tells you nothing. Liveness is `GET /` or
   `/readyz`.
10. macOS Do Not Disturb on. Close Slack.

## The golden intent

Type or speak exactly this (it is the reviewer-demo prompt from TODO.txt, and
it stays inside the footprint generator's supported set):

> An AMS1117-3.3 regulator in SOT-223 with a 10 uF input capacitor and a 22 uF
> ceramic output capacitor. Use only the regulator and passive components — no
> connectors, headers, LEDs, diodes, switches, or test points — and preserve
> the ceramic output-capacitor choice so the adversarial review can check it
> against the selected AMS1117 datasheet.

The "no connectors" clause is load-bearing, not politeness: even with the old
"only the regulator and passives" wording, the model has nondeterministically
added a 2-pin `J_IN` connector, which crashes board emission (see the warning
block). Say the exclusion; do not trim it.

Add the AMS1117-3.3 datasheet URL in the datasheets field and switch grounding
on (both the SPA form and Ada's run form take part-to-URL rows; grounding only
does anything when a URL is present). The first-run form was simplified in the
demo punch-list pass (#43), so this is fewer fields than you rehearsed last
week.

Why this intent: the AMS1117 is an old bipolar LDO that needs output-capacitor
ESR in a stability band — a low-ESR ceramic is the classic subtle mistake. The
adversarial reviewer is explicitly prompted to probe regulator output
capacitors and ESR (`engine/silkscreen/agents/review.py`), so this run reliably
produces a finding class a hardware judge will recognize as *real*, not staged.
The exact wording varies run to run because it is a live model — say so if
asked; it is a strength.

---

## The 3:00 script

**0:00 — Cold open, Ada on top of the desktop.**
Ada is already floating. Say:

> "This is Ada — a desktop copilot for a PCB engine called Silkscreen. I'm going
> to describe a power supply, and it's going to read the datasheet, design the
> circuit, refuse to build it until it validates, place it, route copper, and
> then — the part we care about — argue against its own design."

**0:15 — Enter the intent, start the run.**
Type (or speak) the golden intent, datasheet URL filled, grounding on, model
pinned to Gemini 3.5 Flash. Hit run. The stream starts immediately
(`run.accepted`, then live stage events over NDJSON).

**0:25 — Narrate the stream while it works.** The stages tick as real events
arrive — read, propose, validate/repair, schematic, place, route, review.
Talking points, in the order the stages light up:

> "It's reading the actual PDF — Gemini's native document vision, page numbers
> kept for citations." (read)
>
> "Now it proposes a circuit into a validated intermediate representation. If
> the model wires a capacitor by one leg or names a pin the part doesn't have,
> every error goes back in one batch as a repair prompt. Nothing reaches a
> board file until this validates." (propose / repair rounds)
>
> "Placement is not the model — it's Google OR-Tools CP-SAT minimizing
> wirelength and board area with real courtyard clearance. And routing is an A*
> maze router that names any net it can't finish instead of pretending. The
> honesty is the feature." (place / route)
>
> One sentence on the stack while review runs: "Orchestration is Google's Agent
> Development Kit — the pipeline is an ADK dynamic workflow, and that's the
> default engine, not a wrapper. And the same service is live on Cloud Run
> right now."

**1:15 — WOW BEAT 1: the voice.** The run completes and Ada *speaks the digest
of the review out loud* while the findings render. Shut up and let it talk.
Expected content: the finding about the ceramic output capacitor on the
AMS1117, against the datasheet. Then:

> "It just told me my output capacitor choice can make this regulator oscillate
> — and it cites the datasheet. That's the product: not drawing faster,
> catching what you'd have missed."

**1:35 — Switch to the web SPA (already open in a tab).** Show the finished
run: review findings on the left, click the ceramic-cap finding — the parts it
names **cross-highlight on the board well**. Flick through the Schematic tab.
One line:

> "Same engine, same run. Findings, schematic, and the placed board — click a
> finding and it lights up the parts it's about."

**1:55 — WOW BEAT 2: the guided pointer.** On the selected finding card, click
**Guide me**. A consent card appears ("Walk through …?"); click **Start
guide**. An animated pointer lands on "Show on board"; click **Next**; the tab
switches and the pointer lands on the actual capacitor on the board. Say:

> "And for someone new to EDA, it doesn't just tell you — it walks you there.
> The pointer only advances when you say so, and if it can't find its target it
> says that instead of pointing at the wrong thing."

**2:20 — WOW BEAT 3: KiCad, now one click from Ada.** In Ada, hit **Save** —
a native save dialog writes the project to disk — then click the **Open in
KiCad** button that appears. (If you want zero risk, Cmd-Tab to the pre-opened
KiCad project instead; the button is the better story, the Dock is the safer
one — pick during rehearsal.)

> "Everything you saw is a real KiCad project — schematic, placed board, routed
> copper. KiCad's own ERC passes on the schematic. No KiCad was involved in
> making it: Silkscreen writes the file format directly, pure Python, so it
> runs headless, in CI, anywhere. Any net the router couldn't finish is left as
> ratsnest and named, with the reason — you finish it here, in the tool you'd
> use anyway."

**2:45 — Close.**

> "Gemini through the GenAI SDK, orchestrated by ADK dynamic workflows as the
> default engine, and the service is live on Cloud Run with a Firestore
> datasheet cache — the same UI you saw is public at that URL, with paid
> generation behind an access token, and it generated a full board over that
> URL this afternoon. The deterministic engine underneath runs
> entirely offline — the whole test suite needs no network, no key, and no
> KiCad. Describe a board; get a project back; and it tells you what's wrong
> with it before your fab does."

**Before you say any of the above out loud, re-check the URL.** Those cloud claims
were verified on 2026-08-31 and the instance is **down as of 2026-09-06**
(`/readyz` 500, `/` 503). A recorded deploy is not a running one. If it is still
down, drop the words "is live" and "the same UI you saw is public at that URL"
from the paragraph above and say the service is deployed to Cloud Run with a
Firestore cache and is not currently serving — an honest sentence survives a judge
opening the link; the other one does not.

Cloud claims you can make **once the URL answers again**, all verified 2026-08-31:
the URL serves the web UI publicly, `POST /generate` is token-gated (401 without),
**and a full paid board generation completed over the public URL** — an AMS1117-3.3 LDO intent
returned a complete `.kicad_pcb` (10.55 × 11.8 mm, status optimal). Caveat
worth knowing before Q&A: the *first* cloud smoke failed when the model added
a 2-pin connector (the package-rule limit above); the passing run used the
connector-free phrasing — one more reason the exclusion clause in the golden
intent is not optional.

---

## The 90-second cut

Drop the SPA and KiCad. One surface, one artifact, one argument.

- **0:00** Cold open line, type the golden intent in Ada (model pinned to 3.5
  Flash), run. (15 s)
- **0:15** Narrate the stream, compressed: "reads the real datasheet with page
  citations — validates the circuit before anything is built — CP-SAT placement
  — A* routing that names what it can't finish — then an adversarial review."
  (45 s)
- **1:00** Voice beat: Ada speaks the ceramic-cap finding. (15 s)
- **1:15** Close: "That's a real KiCad project on disk — one click opens it in
  KiCad — and the review just caught a stability bug with a datasheet citation.
  Gemini + ADK dynamic workflows + a live Cloud Run service with Firestore."
  (15 s)

If the live run has not finished by 1:00, **Open session** the golden JSON in
the SPA and the voice beat becomes show-the-finding; the close is unchanged.

## The 30-second cut

No live run. SPA with the golden session already open, KiCad behind it.

> "You describe a board in plain language. Ada's engine reads the datasheet,
> designs and validates the circuit, places it with CP-SAT, routes it, and then
> argues against its own design — this finding says my output capacitor can
> make the regulator oscillate, with the datasheet page cited. [click finding —
> board highlights] And it's a real KiCad project [Cmd-Tab] — schematic, routed
> board, ERC clean. Gemini, ADK dynamic workflows as the default engine, live
> on Cloud Run, Firestore."

---

## Fallback plan, per failure

| Risk | What actually happens | Recovery — rehearse it |
|---|---|---|
| **400: "No package rule for 'J_IN' with 2 pins"** (or any 2-pin part) | The model nondeterministically added a 2-pin connector/LED/diode; board emission has no package rule for 2-pin parts and refuses. Observed live today even on a "passives only" intent. Fix in flight on `fix/two-pin-package-rule`, not merged. | Hit Generate again — it is nondeterministic and the strengthened golden intent usually avoids it. Second strike: **Open session** the golden JSON, or run the pre-generated local project. Never improvise a new intent to dodge it. |
| **Model slow or degraded mid-run** | `gemini-3.7-flash` measured degraded today. The per-request deadline is merged, so a hung call now times out and fails over (3.5-flash-lite, then a Gemma last-resort rung) — visibly, but slowly. | You should already be pinned to `gemini-3.5-flash`; if you forgot, the stalled turn keeps retry / edit / switch-model controls — retry once on 3.5 Flash. If that stalls too: **Open session** with the pre-saved golden JSON and narrate it as "a run from this morning" — say it plainly; judges respect it. |
| **No wifi / venue network dies** | Model calls are the only network in the product. | Everything else is genuinely live offline: **Open session** restores the full golden run in the SPA (local file, no server round-trip); `python scripts/demo.py` does a real CP-SAT solve in front of them in ~20 s, deterministically; the pre-generated KiCad project carries the KiCad beat. The demo loses "watch the model think" and keeps everything else. |
| **Cloud URL looks dead** | Two different causes, and they need different answers. (a) A `/healthz` probe: Google's edge intercepts it on `run.app` domains and 404s before the container answers — that 404 means nothing. (b) It is actually down: as of 2026-09-06, `/readyz` answers 500 and `/` answers 503, and has since 2026-09-05. | Check `GET /` and `/readyz` yourself before the demo, not on stage. If `/` renders, show it. If it does not, **do not open the link** — redeploy with `scripts/deploy.sh` beforehand, or say plainly that the service is deployed and not currently up and move to the local engine, which does the entire demo. |
| **Audio out fails / room is loud** | The speech layer never throws — a failed TTS call degrades to silence with one warning, and the findings render on screen regardless. | The findings *are* the captions; read the top one aloud yourself. If ElevenLabs specifically fails, the system voice is the automatic fallback — no action needed. |
| **Spoken intent misfires** | Voice input goes through the engine's `/transcribe` and depends on room noise. | Type it. The script above already treats typing as the default; speaking is a garnish, not a beat. |
| **Ada won't launch or won't connect** | The .dmg is aarch64-only; the engine URL is configured in its Engine page with a live health check. | The web SPA does the entire demo except the spoken digest — run the same script there and read the digest yourself. Check the health light *before* going on stage. |
| **SPA looks broken (empty feed, stuck form)** | This is the Vite dev-server split-module trap. | You should never be on the dev server on stage. If you somehow are: kill it, use the built bundle the service serves at `/`. |
| **Open in KiCad does nothing / KiCad missing** | The button needs KiCad installed; it is at `/Applications/KiCad/KiCad.app` on the demo machine. | Cmd-Tab to the pre-opened project (checklist step 8). If KiCad is truly gone, the SPA's Schematic tab and board well cover the visuals; say the files are on disk. Never install anything live. |
| **Run finishes but the reviewer misses the capacitor finding** | It is a live model; the class is reliably elicited but not contractual. | Whatever findings it did produce are real — present those, with their citations. If you need the ceramic-cap beat specifically, the golden session has it. |
| **Service 502 mentioning GOOGLE_API_KEY** | You launched the service without the key exported (`python -m service.app` does not read `.env`). | The SPA renders this as setup instructions, not an outage — fix is `silkscreen serve`, which loads `.env`, or export the key and restart. Ten seconds if you know it; that is why it is on the T-30 checklist. |
