# Design review — SPA + Kaleo overlay (2026-08-31)

> **Dated snapshot — do not read this as current.** Verified 2026-08-31 and not
> maintained since. Several findings have been fixed and several components it
> cites no longer exist (`ErrorPanel.svelte`, `RunProgress.svelte`,
> `PipelineFeed.svelte` are all gone from `frontend/src/components/`). Treat every
> item as "was true on 2026-08-31"; re-check against the tree before acting on
> one, and do not cite a line number from here as evidence about today's code.

Read-only review of both user surfaces ahead of judging. Every claim below was
checked against the code as of this pass: the SPA at `frontend/src`, the Kaleo
overlay at the `kaleo-dev` scratchpad checkout (`app/src/pages/{kaleo,workbench,engine}`
only — `contexts/`, `hooks/`, and shared `components/` were being refactored by
other agents while this was written, so line numbers cited from those files are
snapshot evidence, not stable references).

Format per item: **problem → evidence → smallest fix → effort (S/M/L)**.

---

## A. Punch list — web SPA (`frontend/src`)

### SPA-1. The kind-specific error copy is dead code; a failed run shows raw server text

**Problem.** `ErrorPanel.svelte` — the component carrying the documented
"no-API-key renders as setup instructions, not an outage" behaviour, plus the
timeout/network/internal copy — is imported by **nothing** (`grep -rn ErrorPanel
frontend/src` matches only the file itself). The live error surface is the
generic recovery card in `ConversationView.svelte:145–173`: heading "Run
failed", then `{$run.error?.message}` verbatim, for every kind. An unkeyed
service (the single most common first-run failure, and the one a judge cloning
the repo will hit) now shows a raw 502 string next to seven controls (two retry
buttons, three selects, copy, edit) instead of the three-step `.env` fix. The
`kind` machinery still works end to end — `api.js:104` still maps a 502
mentioning `GOOGLE_API_KEY` to `'no-api-key'`, `run.js:359–366` still carries
it — it just no longer reaches any copy.

**Evidence.** `frontend/src/components/ErrorPanel.svelte` (orphaned);
`frontend/src/components/ConversationView.svelte:145–173`;
`frontend/src/lib/api.js:95–110`.

**Smallest fix.** Branch the recovery card on `$run.error.kind`: for
`no-api-key` render the ErrorPanel setup steps (and hide the retry row — retry
cannot succeed); for `network` include the `PORT=8081 python -m service.app`
line; keep the generic body for the rest. Lifting the copy out of
ErrorPanel.svelte into the recovery block and then deleting ErrorPanel is less
work than re-mounting the component. **Effort: M** (S if only `no-api-key` and
`network` get bespoke copy — that covers the two demo-day failure modes).

### SPA-2. The running view has no clock

**Problem.** The `elapsed` store (`run.js:513–525`) is consumed only by
`RunProgress.svelte`, which is also orphaned (no imports). The live running
view is `ActivityCard` inside the chat thread, whose header shows only
`running · N events` (`ActivityCard.svelte:28–31`). Before the first stream
frame arrives — several seconds on a real run, forever on a stalled one —
the card is completely static: no clock, no motion, nothing that
distinguishes "working" from "hung". The old design's stated fallback ("the
un-ticked list with a real clock") lost its clock in the chat refactor.

**Evidence.** `frontend/src/components/RunProgress.svelte:2,61–64` (only
`elapsed` consumer, orphaned); `frontend/src/components/ActivityCard.svelte:23–41`
(no time display); `grep -rn elapsed frontend/src/components` confirms.

**Smallest fix.** In `ActivityCard`, for the entry whose `phase === 'running'`,
import `elapsed` from `run.js` and append `formatDuration($elapsed / 1000)` to
the `.meta` line. One import, one span. **Effort: S**

### SPA-3. The intent form's first screen is written for its authors, not a first-run judge

**Problem.** Three compounding first-run costs on the one screen a judge sees
first:

- The **orchestrator panel** (`IntentForm.svelte:175–221`) puts three selects
  between the intent box and the run button, annotated in insider vocabulary:
  "Request pace … 6 RPM", "Gemini 3 always thinks internally; Fast uses its
  lowest supported effort", and a heading note that reads ungrammatically
  ("Chooses clarification and calls the board generator", line 178). These are
  power-user controls whose defaults are already correct.
- The **"NO budget" toggle defaults to active** (`IntentForm.svelte:36` —
  `seed.no_solver_budget !== false` is `true` on an empty seed) and its active
  style is solid-ink mono all-caps (`:398`), so the default state reads like a
  warning/error chip, next to a disabled number input labelled "unlimited".
- The **submit button says "Run review"** by default (`IntentForm.svelte:268` —
  review defaults on), which undersells the actual act: it generates a whole
  board. A judge scanning for "the button that makes the PCB" doesn't find one.
- Above all of it, the session bar shows `model · Auto · auto thinking` in mono
  before any run exists (`ConversationView.svelte:92–95`) — dashboard jargon as
  the first line of the app.

**Smallest fix.** (a) Wrap the orchestrator section in a `<details>` exactly
like the datasheets section (`:135` already establishes the pattern), summary
"orchestrator · advanced", default closed. (b) Make the submit label
"Generate board" unconditionally (the review being on is already stated by its
checkbox). (c) Relabel `NO budget` → `no limit` and drop the solid-ink active
fill in favour of the checkbox idiom the two neighbours use. (d) Hide the
sessionbar model line until a run has started (`{#if entries.length}`).
**Effort: S** (each is a small, local edit; all four together < half a day).

### SPA-4. "Guide me" is invisible as the entry point to the pointer feature

**Problem.** The guided pointer — a headline demo beat — is entered through a
button styled identically to the two inert-adjacent controls beside it: `.view`
class shared with "Show on schematic"/"Show on board"
(`FindingCard.svelte:75,142–151`). Nothing marks it as *the walkthrough*, and it
only exists on findings that name parts when the board tab is enabled. The
design system already owns a colour for this feature — `--guide-ring`
(`tokens.css:44`) — and the caption card already uses it as a left border
(`GuidePointer.svelte:230`); the button that launches it uses neither.

**Smallest fix.** Give `finding-card-guide` the feature's own identity:
`border-color: var(--guide-ring); color: var(--guide-ring)` (both themes and
both skins already define the pair), optionally a small pointer glyph. No
layout change, no new component. **Effort: S**

### SPA-5. Artifact cards claim "0 findings" for a skipped review

**Problem.** The post-run chat card grid always renders `{findings} findings →
Open details` (`ArtifactCards.svelte:24–28`). When the run was submitted with
review off, `findings` is empty and the card reads **"0 findings"** — exactly
the "clean review" misreading the rest of the app is engineered to prevent
(StatusBar says `Review · not run` at `StatusBar.svelte:24–27`; ReviewResults
has a whole skipped state at `:75–82`). The honesty rule has one hole, and it's
in the first thing shown after a run completes.

**Smallest fix.** Pass the `reviewed` derivation (already computed in
`App.svelte:85`) down through `ConversationView` into `ArtifactCards`, and
render `not run` instead of the count when false. **Effort: S**

*(Housekeeping, folded in rather than a sixth slot: `RunProgress.svelte`,
`PipelineFeed.svelte`, and `ErrorPanel.svelte` are all orphaned, and the
CLAUDE.md frontend section still describes RunProgress/PipelineFeed as the live
running view. Delete the dead files when SPA-1/SPA-2 land and update the doc in
the same PR — a judge reading the repo should not find two running views.)*

---

## B. Punch list — Kaleo overlay (`app/src/pages/{kaleo,workbench,engine}`)

### KAL-1. Red dot + disabled Generate still reads as "app is broken" — the shipped incident is only half-fixed

**Problem.** In the 600×54 bar (`src-tauri/tauri.conf.json:17–18`), Generate is
disabled whenever `canStart` is false — and in the snapshot read,
`canStart = !busy && request.intent.trim().length > 0`
(`hooks/useSilkscreenRun.ts:510`, in-flux tree), i.e. **an empty intent field
disables it**. So the first-launch state with the engine down is precisely the
incident: red dot + dead Generate, with no visible reason for either. The two
explanations that do exist are both hover-only `title` attributes
(`PromptBar.tsx:161–165` on the button, `:42` on the dot) — and the button's
tooltip is on a **disabled** element, which suppresses pointer events in
several webviews, so the one place the reason lives may never show. The
`title` also only branches on `engine.ok`, never on "type something first".

**Evidence.** `pages/kaleo/components/PromptBar.tsx:88,99,155–170,24–58`;
`hooks/useSilkscreenRun.ts:510` (snapshot); `src-tauri/tauri.conf.json:17–18`.

**Smallest fix.** One conditional 10px line inside the Card, under the input
row (the overlay already grows for busy/done/error states, so a line is cheap):
`engine.ok === false` → "Engine unreachable at {baseUrl} — click the dot to
re-check, or open Engine in the dashboard"; else if intent empty and the user
has focused the field → "Type what you want on the board". Plus: wrap the
disabled Generate in a `<span title=…>` so the tooltip survives the disabled
state. Do **not** gate Generate on `engine.ok` — clicking through to the
`offline` RunFailure card (`RunProgress.tsx:322–328`) is already a good, honest
failure with the exact start command in it. **Effort: S**

### KAL-2. The engine dot itself is illegible: 8px, colour-only

**Problem.** The health signal is a `size-2` (8px) circle inside a `size-4`
(16px) click target (`PromptBar.tsx:41,49–55`), distinguished **only by hue**
(emerald / red / 40%-grey) — sub-threshold at a glance in a 54px-tall bar, a
16px hit target well under the 24px minimum, and a WCAG 1.4.1 failure for
red-green colour-blind viewers (the incident report — "red dot read as app
broken" — is partly this: a dot that small carries no shape information, so
colour is all it says, loudly). The dashboard's `EngineStatus` solves this with
a word next to the dot (`engine/components/EngineStatus.tsx:100–118`); the bar
has no room for the word, so it needs shape instead.

**Smallest fix.** Swap the bare circle for a small icon pair at the same
position — e.g. lucide `Zap` (up) / `ZapOff` or `Unplug` (down) at `size-3.5`,
keeping the colours — and bump the button to `size-6`. The existing
`data-online` attribute and aria-label stay. **Effort: S**

### KAL-3. Duplicate `data-testid="engine-status"` with two different contracts

**Problem.** The bar's `EngineDot` uses `data-testid="engine-status"` with
`data-online="unknown|true|false"` (`PromptBar.tsx:45–46`); the dashboard's
`EngineStatus` uses the same testid with `data-state="connected|checking|never|unreachable"`
(`engine/components/EngineStatus.tsx:79–80`). Same id, different component,
different attribute vocabulary — this breaks the repo's testid convention (one
id = one role, disambiguated by identity attributes) and will bite the first
e2e that queries it. Also a maintenance smell: `EngineDot` reimplements what
`EngineStatus compact` already renders, minus the never-connected/unreachable
distinction the dashboard version deliberately draws.

**Smallest fix.** Either rename the bar's to `engine-dot`, or (better, and
enables KAL-2 for free) replace `EngineDot` with
`<EngineStatus health={engine} compact />` — the component was explicitly
designed to take a caller-owned poll for exactly this reason
(`EngineStatus.tsx:29–32`). **Effort: S**

### KAL-4. Voice is discoverable at the wrong moments, and its settings live on the wrong page

**Problem.** Three small mismatches around the wow-beat feature:
(a) the mic's only introduction is a hover tooltip (`PromptBar.tsx:105`) — fine
for daily use, invisible in a demo hand-off; (b) the mute toggle appears only
after a run completes, floated over the result card (`kaleo/index.tsx:104`) —
the first time a user can turn the spoken digest *off* is while it is already
speaking; (c) voice configuration (enable switch, ElevenLabs key) lives on the
**Engine** page (`engine/index.tsx:109`, `engine/components/VoiceSettings.tsx`)
even though the post-Pluely nav will have a dedicated **settings** page — a
user looking for voice will look in Settings, find nothing, and conclude it
doesn't exist. There is also a live-demo hazard no code can see: the OS mic
permission prompt fires on first `voice.start()`, mid-arc.

**Smallest fix.** Move `<VoiceSettings />` from the Engine page onto the
Settings page when the nav settles (it is already a self-contained component,
so this is a two-line move); keep Engine to connection + start steps + the
loopback explainer. Rehearsal note (no code): trigger one dictation before the
demo so the OS permission dialog is already answered. (a) and (b) are
nice-to-have polish, not fixes. **Effort: S**

### KAL-5. The busy overlay stacks two competing live surfaces

**Problem.** While a run is in flight the overlay shows the full six-row stage
checklist *and* the activity feed at up to `max-h-40` (`kaleo/index.tsx:90–99`),
under the prompt row — ~320px of overlay for a window whose identity is a 54px
bar. The two surfaces narrate the same thing at different granularities, and in
the overlay's constrained space the feed mostly repeats what the checklist's
spinner row already says. (Each stage row's richer summary is hover-only:
`RunProgress.tsx:84` puts it in `title`.) The dashboard is the right home for
the full trace; the overlay needs the checklist, the clock, and Cancel.

**Smallest fix.** Default the `ActivityFeed` collapsed behind a one-line
disclosure ("activity · N lines") and keep the checklist always visible. No
component changes beyond a `useState` and a trigger row in `kaleo/index.tsx`.
**Effort: S**

*(Positive findings worth keeping as-is: `RunFailure`'s kind-keyed explanations
with copyable start commands (`RunProgress.tsx:317–362`) are the best error
surface in either app; the "not reported vs zero" Stat convention
(`RunProgress.tsx:166–186`), the cancelled-state copy (`kaleo/index.tsx:128–143`),
and the workbench empty state with the real hotkey rendered as `<kbd>`
(`workbench/index.tsx:163–177`) are all exactly right. The workbench IA —
Board/Schematic/Review/Artifacts tabs with `forceMount` panes and hash-routed
tabs (`WorkbenchTabs.tsx:48–61`) — needs no change for judging.)*

---

## C. The judge's 3-minute arc

**Where the eye goes now.** SPA first paint: mono `model · Auto · auto
thinking` line, then a large "orchestrator" panel of three selects, then a
black "NO budget" chip — three power-user artifacts before the intent box wins
attention. Overlay first paint: a clean bar, but if the engine isn't up yet the
red dot + dead Generate stalls the demo cold with no on-screen explanation.

**What competes with the wow-beats.** Voice: the OS permission prompt (KAL-4)
and the mute toggle appearing only after speech starts. Pointer: a launch
button that looks like every other tertiary control (SPA-4). Board render:
nothing — the dark well with magenta courtyards is the strongest frame in the
product and needs no change; get to it fast and hover a part.

**Single highest-leverage change per surface.**
- **SPA:** SPA-3(a)+(b) — collapse the orchestrator panel and rename the CTA
  to "Generate board". It converts the first ten seconds from "what am I
  configuring?" to "type a sentence, press the red button".
- **Overlay:** KAL-1 — the inline engine-down reason line. It converts the
  worst possible demo opening ("is it broken?") into a self-explaining state
  with the recovery path on screen.
- **Dashboard:** nothing blocking; it demos well as read.

**Suggested beat order** (uses what exists, changes nothing): overlay up with
engine already green → dictate the intent (mic pre-authorised) → Generate →
stage checklist ticks in the overlay → digest speaks over the done card →
"Open the full review" → workbench Board tab → select a blocker finding in
Review, watch cross-highlight → SPA only if the guided pointer is the closer,
since "Guide me" lives there.

---

## D. Demo-critical vs nice-to-have

**Must land before judging** (each is S except SPA-1's M, all are copy/local-style level):

1. **SPA-1** — at least the `no-api-key` and `network` branches of the recovery
   card. The likeliest live failure currently shows raw 502 text.
2. **KAL-1** — inline engine-down reason + tooltip-on-wrapper. Directly
   re-litigates the shipped incident in front of judges otherwise.
3. **SPA-3 (a)(b)** — orchestrator behind a disclosure, CTA renamed.
4. **SPA-2** — the clock in the running card. A stream that takes 5 quiet
   seconds to open currently looks frozen at the exact moment attention peaks.
5. **SPA-5** — "0 findings" on a skipped review is a falsifiable honesty bug in
   a product whose pitch includes honesty rules.

**Nice-to-have** (real improvements, safe to defer): SPA-4 guide-button
identity, SPA-3(c)(d), KAL-2 dot shape, KAL-3 testid/dedupe, KAL-4 VoiceSettings
move, KAL-5 feed disclosure, deleting the three orphaned SPA components +
CLAUDE.md frontend-section update.

---

## E. Do not touch — deliberate per CLAUDE.md and in-code design notes

Things a fresh pair of eyes may flag that are load-bearing decisions:

- **The board well is dark in both themes and both skins.** `tokens.css:29–38`
  ("the board is the board"), reaffirmed in `glass.css:27–29`. Never theme
  `--well-bg`/`--board-*`.
- **Stage boxes tick only from received events; skipped stages stay empty.**
  `ActivityCard.svelte:35–41`, `run.js:374–407`, and the overlay's equivalents
  (`RunProgress.tsx:37–47,117–124`, `ActivityFeed.tsx:28–29` rendering nothing
  before the first event). An always-filling progress bar is the anti-pattern
  these exist to refuse.
- **Suggested-fix buttons are inert secondary controls.**
  `FindingCard.svelte:78–82` — nothing in the app applies a fix, so nothing may
  look like it does.
- **`data-testid`s ship in production**, on intrinsic elements, disambiguated by
  identity attributes, never index suffixes (CLAUDE.md convention; the guided
  pointer's selector engine depends on them at runtime — `guide.js:36–48`).
- **Rail and title-bar counts derive only from the response.**
  `SideRail.svelte:7–9`, `TitleBar.svelte:15–24`. The mockup's invented numbers
  must never return.
- **The SPA's MicButton is disabled by construction.** `MicButton.svelte:2–4`;
  voice-in-the-SPA is deliberately the last phase. The overlay having working
  voice while the SPA mic stays dead is intended, not an inconsistency.
- **Grounding disarms without a datasheet** in both surfaces
  (`IntentForm.svelte:53–57,254–263`; `RunOptions.tsx:44–54,86–92`) — a
  `ground: true` with no sheets is a guaranteed 400.
- **The loopback-only refusal and its blunt copy** on the Engine page
  (`engine/components/EngineConnection.tsx:17–29,87–93`) — a security stance,
  not UX friction; likewise the service's no-CORS design it protects.
- **Glass tint/canvas roles receive no backdrop filter** (`base.css:31–45`,
  `glass.css:19–21`) — preventing nested blur stacks over the drawing wells is
  the point, not an omission.
- **"Not reported" instead of zero** throughout the overlay's RunSummary
  (`RunProgress.tsx:166–186,199–203`) — an absent field and an empty list are
  different claims.
- **The overlay's Generate button disappearing while busy**
  (`PromptBar.tsx:149–171`) — the one-prompt-one-run guard on a control that
  spends money.
