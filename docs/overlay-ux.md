# The step panel, read in a glance

The overlay is a 600 px strip over KiCad. In step mode the engineer is looking
at the canvas, not at the strip, and glances at it to answer three questions:
what just happened, what is the engine doing without me, and what can I
approve. The panel is laid out in that order and nothing else is allowed to
push those three above the fold.

This note records the reading order, the states, and what is collapsed by
default and why. The implementation is `app/src/pages/kaleo/components/`
(`StepPanel.tsx`, `DeliverPanel.tsx`) over the pure helpers in
`app/src/lib/silkscreen/steps.ts`.

## Reading order

1. **Headline** (`step-headline`) — one sentence, always first. While a step
   runs: "Routing copper… 12 s" with the Cancel button beside it. While the
   engine waits: what just landed and where ("Placement is in KiCad."), or
   the failure when a step failed. When nothing is left: "Every stage has
   run." It is computed by `headline()` in `steps.ts`, so the sentence is
   tested, not eyeballed.
2. **Checklist spine** (`step-row`, one per step in pipeline order) — the
   seven steps with their status glyph, label, a "KiCad" chip when
   the engine pushed the result there, and the one-line summary the step's
   own response supports. Rows the engine started on its own (`background`)
   say so: "Looking up parts in the background…" — a spinner nobody pressed
   for is otherwise a mystery.
3. **Step detail** (`step-detail`, under its row) — the part that used to
   stack: order issues, the BOM table, the 3D model, the case margins, the
   review findings, the unrouted nets. Each lives under the row it belongs
   to, behind its row's disclosure, and is **open only for the latest
   step** the engineer has not yet read. A row with nothing
   worth expanding (a schematic that proposed parts and nets, an unreceived
   step) has no chevron rather than an empty section.
4. **Action row** (`step-approve`, `step-dismiss`) — the approvals, pinned
   at the bottom of the panel outside the scroll area, so they are visible
   whatever is expanded above. The first available step is the primary
   button; the rest are outlined. "Stop here" closes the run without
   spending anything.
5. **Board path** — the routed (or placed) board's path, truncated, last.

The outcomes — the step details — sit in one scroll area (`step-scroll`,
capped at 14 rem); it wraps the outcomes only. The window never widens: the
overlay's height follows its content up to `OVERLAY_MAX_HEIGHT`, and above
that the outcomes scroll inside themselves. The headline, the spine and the
action row sit outside that area, so the two things the glance needs — what
happened, what to press — never scroll away.

## States

The panel renders `useStepRun`'s state machine directly; there is no second
copy of it.

| `run.status` | Headline                                     | Spine                                           | Actions                         |
| ------------ | -------------------------------------------- | ----------------------------------------------- | ------------------------------- |
| `running`    | "*Action*… *n* s" + Cancel                   | the running row spins; background rows say so   | none                            |
| `waiting`    | what landed and where, or what is preparing  | done rows carry summaries; available rows quiet | one button per `run.available`  |
| `error`      | the engine's message, detail underneath      | as last known                                   | the same buttons (a retry) + Stop |
| `done`       | "Every stage has run."                       | every row done                                  | "New run"                       |

Per row, `StepRow.status` is `pending` (dimmed), `running`, `preparing`
(the engine started it by itself; still approvable), `available`, `done`.

### Step detail contents

| Step       | Detail                                                                                   |
| ---------- | ---------------------------------------------------------------------------------------- |
| `propose`  | nothing — the schematic is in KiCad; the summary carries the counts                      |
| `place`    | the parts placed, one line each (ref, footprint), and any solver warnings                |
| `route`    | **every unrouted net by name with the router's reason** — the honesty contract, kept     |
| `review`   | the findings, worst first, each with a severity badge, the parts named, the suggested fix |
| `sourcing` | the BOM table, the counts line, the BOM file, the engine's warnings                      |
| `order`    | the gate's issues worst first, the package, the 3D model (open + inline preview), "nothing is submitted" |
| `case`     | the fit margins per axis, the SCAD parameters, the file, warnings                        |

The BOM table appears **once**: under the sourcing row when that step ran,
otherwise under the order row (the order step collects the background
sourcing job and carries the same block). `bomHost()` in `steps.ts` decides.

## What is collapsed by default, and why

- **Every finished step but the latest.** The engineer has read those — in
  KiCad, mostly — and the row's summary is the reminder. Opening one is one
  click. When a new step lands the defaults reset: the new latest opens, the
  one before it closes (the list is keyed on the history length, so no effect
  synchronises state).
- **The 3D preview** inside the order detail. A WebGL canvas is the tallest
  and heaviest thing the panel can show; it mounts only behind "Preview
  here" (`model-preview-toggle`), next to "Open 3D model" which hands the
  `.glb` to the OS. If the viewer cannot load the file it says so in a
  sentence (`model-preview-error`) and the OS button remains.
- **The delivery panel** ("Send it on"). It is not part of the run; it is
  what to do with the routed board. It renders as one header line with the
  readiness in it ("Chat, email and calendar ready", or the count of things
  the engine says are not configured) and opens on click
  (`deliver-disclosure`). The engine's config is still fetched once on mount
  so the header can say that much.

Nothing is hidden that changes a decision: a blocker count is in the order
row's summary whether or not its detail is open, an unrouted net is in the
route summary, a background job is named in its row.

## Status badges: one component, one vocabulary

Every status badge in the panel is rendered from one `BadgeSpec`
(`label`, `tone`) that `statusBadge(kind, value)` in `steps.ts` produces.
Four tones — `ok`, `warn`, `bad`, `muted` — cover all four vocabularies:

| Kind        | Values → tone                                                                 |
| ----------- | ----------------------------------------------------------------------------- |
| `issue`     | blocker → bad, warning → warn, note → muted                                   |
| `finding`   | blocker/error → bad, marginal/warning → warn, note/info → muted               |
| `datasheet` | verified → ok "PDF verified", not_pdf → warn, unreachable → bad, none → muted |
| `mpn`       | verified → ok "Verified", proposed → muted "Proposed", none → muted "No MPN"  |

An unknown value renders as itself in the muted tone: the engine's
vocabulary is additive and a status this build has never seen must show,
not crash or vanish.

`mpn: verified` is the distributor confirmation lane S adds. The BOM's MPN
cell shows the badge and, **only when the status is `verified` and the entry
carries an http(s) `distributor_url`**, a "Distributor · browser" link
(`distributor-open`). A proposed MPN never gets a link, whatever else the
entry carries — a link is the panel vouching for the part.

## Every "opens something" says where

Buttons that leave the overlay carry the destination in their label:
"Reveal package · OS", "Open 3D model · OS", "Preview here" (the inline
viewer), "Open datasheet · browser", "Distributor · browser", "Reveal BOM ·
OS", "Reveal case · OS". A row the engine pushed into KiCad wears
that chip. When the OS takes nothing (no application claims the file) the
button's note shows the path instead of doing nothing.

## data-testid

Ids sit on intrinsic elements, identity by attribute: `step-row`/
`step-detail` carry `data-step`; `order-issue` and
`review-finding` carry `data-sev`; `bom-row`, `datasheet-badge`, `mpn-badge`,
`datasheet-open` and `distributor-open` carry `data-ref`; every `status-badge`
carries `data-kind`, `data-value` and `data-tone`.
