# The first ten minutes

Someone installs Kaleo and opens it. This is a walk of that path in code, what
it says that is not true, and two proposals where the right fix is a decision
rather than a bug.

Written 2026-09-08. The path is `app/src-tauri/src/setup.rs` (the launch gate)
→ `app/src/pages/welcome/` (six screens) → `GET /setup` (`service/setup.py`)
→ the strip (`app/src/pages/kaleo/`). Prior art read for this: OpenWhispr's
onboarding, vendored read-only at `vendor/openwhispr/` — specifically
`src/components/onboarding/flow.ts` and `src/components/onboarding/setupEligibility.ts`.
Nothing here imports from it.

---

## What the walk found

### 1. The wizard never checked the canvas — fixed

`HelloStep.tsx:12` opens with

> "Ada is a junior hardware engineer who works beside KiCad."

and then no screen asked whether KiCad exists. The machine already knew:
`app/src-tauri/src/cli.rs:314-316` resolves `kicad-cli`
(`which`, then `/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli`, then
`KICAD_CLI`), and `service/integrations.py:712` probes the same thing. That
answer surfaced only in Settings → Command-line tools
(`app/src/pages/settings/components/CliTools.tsx`), a pane a new user has no
reason to open. So the way you learned the canvas was missing was to run the
`order` or `kicad_live` step and read the failure — minute nine, not minute one.

**Fixed** by putting a KiCad row on the engine step, with three rules:

* it does **not** block Continue. Ada designs boards without KiCad; what stops
  working is showing a stage in KiCad, ERC/DRC, and the order step's 3D
  export, and the row says exactly that;
* finding the binary is reported as *found*, never as *works* — "Found on this
  Mac. Ada did not run it, so the version is not known." `/integrations`'
  rule about `ready` being a claim about configuration only, applied to a
  filesystem probe;
* a machine that could not be asked (a browser tab, a build with no CLI
  allowlist compiled in) reads "KiCad: not asked", never "install KiCad", and
  is not recorded as connected.

The row also gates the screen's 600 ms auto-advance: a missing canvas cancels
it, because a screen that dismisses itself in 600 ms cannot deliver bad news.
That rule is OpenWhispr's, at
`vendor/openwhispr/src/components/onboarding/flow.ts:159-161` — the blocking
`required-models` step is spliced into the route *only* when the dependency is
missing, and is absent entirely when it is present. Here it is applied inside
an existing screen rather than as a seventh one; see proposal A for why that
compromise, and what the fuller version would look like.

### 2. The progress dots over-counted — fixed

`progress()` returned `{ index: stepIndex(step), total: SETUP_STEPS.length }`
— six, always. But `hello` hides the dot row (`index.tsx`,
`showDots={shown !== "hello"}`) and `done` has no footer at all. So the first
screen anyone saw a counter on, Choose your look, announced **"Step 2 of 6"**:
six promised screens, second dot lit, for a row the user had never seen.

OpenWhispr names this exact bug in the comment on `COMPACT_STEPS`
(`flow.ts:84-91`): "landing on `languages` reads as '1 of N', not '3 of N' for
two steps the user never saw a counter on." Fixed the same way — `DOTTED_STEPS`
excludes the two, `stepIndex` still runs on the full list because ordering and
counting are different questions. Choose your look now reads "Step 1 of 4".

### 3. Billing is asked for before a board exists — proposal A

`AccountsStep.tsx` renders Google, then **Stripe**, then Microsoft behind a
"More accounts" disclosure. Stripe is the third thing a brand-new user is
shown, and its own copy says what it is for:

> "Stripe handles fabrication and part orders."

Ordering fabrication is not a first-ten-minutes act. It is not even a
first-session act — it needs a finished, reviewed, routed board. Asking for a
restricted API key on screen four, before the user has seen Ada place a single
part, is the classic way to lose someone: the wizard's cost is paid up front
and its value is all downstream. `ACCOUNTS_SUBTITLE` already says "Each one is
optional", which is true, but the layout does not behave as if it were —
Stripe is above the fold and Microsoft, which is at least *also* an input path
(a Teams meeting can state a requirement), is hidden below it.

### 4. Everything else the walk checked, and found honest

Worth recording so it is not re-litigated:

* the engine step cannot start Python and does not pretend to. It prints the
  terminal commands and says "Answered /healthz at {url}", never "Running";
* the connect cards' ready words are careful: Google "Signed in", Microsoft
  "Token issued" (never "Verified"), Stripe "Key verified"; the Microsoft card
  renders `meaning` verbatim underneath;
* no secret is echoed. Secrets are `type="password"`, cleared after save, and
  the Chat webhook is never shown even as a tail;
* demo mode is read from the server body (`report.report?.mode === "demo"`),
  never set by the client, and the Done screen says nothing was connected;
* `consentUrlAllowed` refuses an `auth_url` on an unexpected host before the
  browser is ever opened.

---

## Proposal A — make the route conditional, not a fixed six

**The decision:** should `SETUP_STEPS` stay a frozen array of six that every
user walks, or become a function of what this machine and this user need?

Today `machine.ts` exports `SETUP_STEPS` as a const and `reduce` walks it by
index. Every user sees every screen, including screens with nothing to say.

OpenWhispr does the other thing. `getOnboardingRoute(context)`
(`vendor/openwhispr/src/components/onboarding/flow.ts:120-166`) *builds* the
step list from context — `authPath`, `setupMode`, `agentAllowed`,
`requiredModelsPending` — so a guest walks five steps and an account user
seven or more. Three pieces of that design are worth copying:

1. **Expensive things are appended on demand.** `SETUP_ROUTES` (`flow.ts:59-62`)
   adds the provider-key steps only once the user has picked BYOK or local on
   the `setup-choice` screen. Choosing the default appends nothing at all.
2. **A blocking step exists only when it must.** `required-models` is spliced
   in at `flow.ts:159-161` when the dependency is missing on disk, and is not
   in the route otherwise.
3. **The counter follows the live route and is allowed to grow.** The comment
   on `getOnboardingProgress` (`flow.ts:255-265`) states it outright: picking
   BYOK "appends two steps and the row grows by two dots at that moment, which
   is the flow honestly getting longer." A counter that lies about length to
   look shorter is worse than one that changes.

**Applied here, the route would be:**

| Screen | When |
|---|---|
| Hello | always |
| Choose your look | always |
| Start the engine | always (Continue gated on a healthy, keyed engine) |
| **Install KiCad** | **only when `list_cli_tools` says the binary is absent** |
| Connect accounts | always — Google only |
| Permissions | always |
| You're all set | always |

with Billing moved off the wizard entirely (proposal B).

**Alternatives considered.**

* *Keep six, put KiCad on the engine step.* What is shipped today, and the
  reason is scope: a real route function touches `WelcomeLayout` (not this
  lane's file), `index.tsx`, and every wizard test, and the owner has looked
  at only two of the six screens on a real display. The compromise keeps the
  honesty (the row exists, the auto-advance is cancelled) and defers the
  restructuring.
* *A seventh screen, unconditional.* Rejected. A machine that already has
  KiCad gets a screen that says "yes, you have it" — which is exactly the
  padding OpenWhispr's conditional splice avoids.

**Recommendation:** take it, but only after someone has looked at all six
current screens on a display. `DOTTED_STEPS` is already the seam a route
function would replace, so the counter half is done.

## Proposal B — Billing leaves the wizard

**The decision:** does the Stripe card belong in first-run at all?

**Recommendation: no.** Move it to Settings, where `BillingSetup` already
renders (`app/src/pages/settings/components/BillingSetup.tsx`, the same
component the wizard embeds with `variant="setup"`), and have the wizard's
Done screen name where it lives in one sentence rather than ask for a key.

Reasons, in order:

1. **The cost is paid before the value.** A restricted API key on screen four
   buys the user nothing until a board is finished, reviewed and ordered.
2. **The "Skipped: Billing" line reads as a failure.** `skippedSentence`
   builds the Done screen's sentence from cards the user walked past. Today a
   completely successful first run — engine up, Google connected, board about
   to be designed — ends on "Skipped: Billing", which frames the correct
   choice as an omission.
3. **It is the one card that is not about doing the work.** Google delivers
   results, Microsoft is an input path, notifications and the mic are how Ada
   talks to you. Stripe is a payment method for a thing that has not been
   designed yet.

**Cheaper alternative if the card must stay:** demote it to the "More
accounts" disclosure with Microsoft, so Connect accounts is Google above the
fold and everything else one click down, and remove `stripe` from
`SETUP_CARDS` so the Done screen stops reporting it as skipped. This is one
line in `AccountsStep.tsx` plus two in `machine.ts`, and captures most of the
benefit.

**Not recommended:** leaving it as-is with softer copy. The problem is
position, not wording.

---

## Two claims checked, both settled

**`app/src-tauri/src/setup.rs` is real and compiled.** The standing note that
it exists only as an uncompiled patch at `~/Desktop/Coding/kaleo-shell-setup.patch`
is stale in every part: that file does not exist on disk; `setup.rs` was
committed in `d9cad3d` (2026-09-07); it is `mod setup;` at
`app/src-tauri/src/lib.rs:4` with its five commands registered at
`lib.rs:96-100` and `apply_launch_policy` called at `lib.rs:115`; and TODO.txt
feature 26 records `cargo check` clean plus a `tauri dev` run on 2026-09-07
where the gate was watched to fire. `docs/setup.md` has been corrected.
**CLAUDE.md still carries the stale sentence** ("a Rust gate in
`app/src-tauri/src/setup.rs` that exists only as an uncompiled patch,
`~/Desktop/Coding/kaleo-shell-setup.patch`, until someone builds it") and
should be updated by whoever next holds that file.

**`docs/guided-cursor.md` was already corrected and needs nothing.** The
standing note says it cites `app/src-tauri/src/capture.rs` in four places as
evidence a screen-capture skeleton exists. It does cite the path four times —
at `:24`, `:231`, `:304` and `:424` — but every one of them is a *correction*:
"has since been deleted", "deleted in `376ee57` … nothing replaced it. Budget
this as new work", "**It is gone** … plan the overlay as new work", and "cited
in earlier revisions, was deleted in `376ee57`; read it in git history, not in
the tree." It also states that `xcap` is absent from `Cargo.toml`, which is
true. `376ee57` is "Remove the legacy Pluely product from Kaleo" (2026-08-31)
and `git log --diff-filter=D` confirms it is the commit that deleted the file.
No capture path was built, and none should be on the strength of that doc.
**CLAUDE.md's summary of this doc is the thing that is stale**, not the doc.
