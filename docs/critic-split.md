# The adversarial critic: the specialist split, the premise filter, the merge

TODO.txt feature 4 had two open rows. This is what was measured, what was
built, and what was deliberately left alone.

The code is `engine/silkscreen/agents/review.py`; the offline tests are
`engine/tests/test_review.py`.

## 1. What the critic actually found before this change

Measured 2026-09-08 against three of the demo prompts at the top of TODO.txt
(LDO, 555 blinker, LM358 buffered reference), one call per board, `gemini-3.6-
flash`, temperature 0, no datasheet facts supplied. The circuits were the
validated `CircuitSpec`s the pipeline's own propose stage produced for those
prompts.

**Three findings across three boards, and one board got nothing at all.**

| board | findings | judged |
| --- | --- | --- |
| LDO (AMS1117-3.3, 10 µF in, 22 µF out) | 1 | blocker: "+3V3 is not connected to any load". Real, though the prompt forbade an output connector, so it is a consequence of the request rather than a design error. |
| 555 blinker (8 passives) | **0** | — |
| buffered 2.5 V reference | 2 | marginal: the divider node has no bypass capacitor (real, and a good catch). note: the unused amplifier's non-inverting input is grounded on a single supply (real, and a genuinely expert observation). |

Precision was not the problem — none of the three was invented. **Coverage
was.** The most complex board in the set produced silence, and the critic
missed the one finding the "reviewer demo" prompt in TODO.txt exists to
provoke: a 22 µF *ceramic* output capacitor on an AMS1117, whose loop needs
output-capacitor ESR to be stable.

This is the failure mode `audit/judgment.py` already names for the other
reviewer: "A whole-board prompt reliably finds two or three things and stops."
There it is answered with `per_part_focus`. Here the same answer is applied to
domains instead of parts.

## 2. Three calls or one? — measured, not assumed

The TODO row names three specialists: power, signal integrity,
manufacturability. The design question is whether they have to be three model
calls. They cost money, and three sequential calls take the critic out of the
window where it overlaps the placement solver and costs the engineer no wall
clock at all.

Two runs, each with the *same* prompt content presented two ways, same model,
same specs, same temperature:

| model | one call, three lenses | three calls, one lens each |
| --- | --- | --- |
| `gemini-3.6-flash` | 3 calls → **8 findings** | 9 calls → **2 findings** |
| `gemini-3.5-flash-lite` | 3 calls → **7 findings** | 9 calls → **6 findings** |

**One call wins on both models: the same or better yield at a third of the
cost.** The margin on `gemini-3.6-flash` is large enough that the direction is
not in doubt; the margin on `gemini-3.5-flash-lite` is within noise on a
three-board sample, which is the honest way to read it, and it still does not
favour three calls.

The mechanism, watching the answers: a prompt that says "you are the power
critic, ignore everything else" makes it easy for the model to conclude its
own domain is fine and return `{"findings": []}`. All three narrow prompts did
that on the blinker in the `gemini-3.6-flash` run. A single prompt carrying
the adversarial framing once and then three lenses gives the model one
instruction to find something wrong and three places to look.

### Where the measurement is weak, stated rather than hidden

* Three boards, one run each, two models. That is enough to reject "three
  calls are worth 3× the money" and not enough to put a number on the gap.
* The first `gemini-3.6-flash` three-call run gave each specialist prompt the
  domain checklist but not the "assume it contains at least one real error"
  framing the whole-board prompt carries. The rerun that fixed that could not
  be completed: both `gemini-3.6-flash` and `gemini-3.5-flash` hit their
  free-tier daily request quota. The `gemini-3.5-flash-lite` pair *is* framed
  the same way on both sides and is the controlled one; it is also the run
  where the gap is smallest.
* In the `gemini-3.5-flash-lite` pair the anti-boilerplate rule (below) was
  present on the three-call side and absent on the one-call side, so that
  comparison is if anything biased *against* the design that won.

### What the split buys, in findings

On `gemini-3.6-flash`, the split found both of the two best findings in the
set, and the undifferentiated prompt found neither:

* **power**, LDO: "unspecified output capacitor ESR and dielectric... low-ESR
  ceramic capacitors can cause control-loop oscillation." This is the finding
  the reviewer-demo prompt was written for.
* **manufacturability**, blinker: the 10 µF timing capacitor is specified with
  no dielectric, and a Class 2 ceramic's DC bias coefficient would move the
  oscillator frequency. A real defect that neither of the other two lenses is
  looking for.

It also produced one recurring false positive, which is now answered in the
prompt: the manufacturability lens files "the parts are not specified
precisely enough to order" on every board, once as a *blocker*. It is true —
the IR carries no packages, part numbers, tolerances or voltage ratings, by
design, because the sourcing stage chooses them — which makes it a remark
about the notation rather than a defect in the circuit. The prompt now says so
and `test_the_prompt_forbids_the_finding_that_is_true_of_every_netlist` pins
it.

## 3. Filtering findings against the actual spec

The old row read "unknown part refs dropped; findings retained". Everything
else a finding asserted went through unchecked, and a finding that argued
about a 10 µF output capacitor on a board whose output capacitor is 22 µF
reached the engineer looking exactly like one that did not.

Two things changed.

**Net names are filtered like part names.** `Finding.nets` exists, unknown
nets are stripped, and a finding naming *only* invented parts and nets is
dropped rather than kept unlocatable. `audit/judgment.py` already did both
halves; the engine critic did one.

**The critic must state its premises, and the premises are checked.** Each
finding carries `claims`, and each claim is one of four kinds, chosen because
each is decidable from the validated `CircuitSpec` with no judgement at all:

| kind | says | refuted when |
| --- | --- | --- |
| `value` | this passive has this value | the spec gives it another |
| `connected` | this terminal is on this net | the spec puts it elsewhere, or nowhere |
| `floating` | this terminal is on no net | the spec puts it on one |
| `pin_count` | this device has this many pins | the spec declares another number |

A finding resting on a refuted premise is dropped, and
`ReviewReport.dropped` carries the refutation in words — "the finding assumes
`U2.nSLEEP` is floating, but the circuit puts it on net `SLEEP`". This filter
can throw away a well-argued blocker, which is exactly why it may never be
silent.

Two rules keep it from over-reaching:

* **An undecidable premise never drops a finding.** A claim of an unknown
  kind, or about a part the index cannot resolve, is recorded as
  `uncheckable` and the finding survives. Treating "I could not check it" as
  "it is false" would make the filter's silence into a verdict.
* **A finding with no checkable premise says so** on `Finding.checks`, rather
  than presenting the same empty `checks` as a finding that was checked and
  passed.

This is the shape `meetings/intent.py` already uses on a transcript: the model
must supply a verbatim quote, and the code checks the quote against the
source. In both places the point is to make the model say something that can
be *wrong*, not merely something that can be unconvincing.

Measured end to end with the shipped prompt: on the LDO the ESR finding comes
back carrying three confirmed premises — `C_out is 22uF`, `C_out.1 is on
+3V3`, `C_out.2 is on GND` — so the reader can see what it was checked
against and not merely that it survived.

## 4. The merge, and where its design came from

Three sources, read rather than recalled, each cited at the function that uses
it.

**semgrep — `src/reporting/Core_json_output.ml`, `dedup_and_sort`.** Sort,
keep the first of each key, sort again. The first sort is the ranking, so
"first" is a choice rather than an accident; the second is because the
hashtable order is not deterministic. `_merge` does exactly this, and
`_sort_key` is total for the reason `Semgrep_output_utils.compare_match`'s own
comment gives: findings that compare equal fall through to iteration order,
and semgrep's autofix then picks a different fix run to run. The analogous
hazard here is that `_merge` keeps the first of each group and folds the rest
into it, so an unstable order changes which wording, fix and citation the
engineer is shown.

**semgrep again, on what belongs in the key.** It pulled `validation_state`
*out* of `core_unique_key` and added a preference function
(`should_report_instead`) so a confirmed-valid match replaces an unconfirmed
one with the same key, rather than the two failing to dedup because they
disagreed. Severity is handled the same way here: it is not part of the key,
and the survivor keeps the **harshest** severity any specialist gave it. Not
an average and not a vote — a blocker is a claim that the board will not work,
and a second critic filing the same defect as a note has not made the board
work.

**microsoft/sarif-sdk — `src/Sarif/Baseline/V2/WhatComparer.cs`,
`MatchesWhat`**, documented as "true if *any* 'What' property matches", with a
sharpening that forces a non-match when both sides carry fingerprints and none
agree. `_same_claim` is that shape: two findings must name **exactly** the same
parts and nets, and then any overlap of title content words is enough. The
asymmetry is deliberate — an under-merge shows the reader a near-duplicate,
which they can dismiss; an over-merge deletes a real defect, which they
cannot. And the sharpening: a finding naming no parts and no nets has no
strict half left, so it falls back to requiring identical signatures.
`ResultMatchingBaselinerFactory`'s rule that exact matchers run before
heuristic ones is why `_merge` makes two passes.

**GitHub code scanning — `github/codeql-action`, `src/fingerprints.ts`.** Its
`primaryLocationLineHash` hashes the *characters* of the line, whitespace
skipped, deliberately not the line number. `_signature` is the same choice:
content words rather than position.

**golangci-lint — `pkg/result/processors/max_same_issues.go`, `Finish`**,
which reports how many findings it hid rather than only counting them. Every
merge here produces a sentence in `ReviewReport.merged` and a `review.merged`
event, naming both specialists and both severities when they disagreed.

Two of its other decisions were looked at and **not** taken.
`uniq_by_line.go`'s key is `(file, line)` alone, first-wins, and its sort runs
*after* the dedup — so which linter survives depends on registration order.
That is the bug `_sort_key` exists to avoid.

**Not taken: majority voting.** inspect_ai
(`src/inspect_ai/scorer/_reducer/reducer.py`) has the best open-source judge
panel I found, and its default reducer is a strict `majority`. That is right
for N judges answering *one* question and wrong here, because these three
specialists answer three different questions: only the power critic is looking
for an ESR problem, so requiring two of three to see it would discard every
finding the split exists to buy. What does carry over is that panel's other
discipline — `_with_panel_metadata` records who voted for what — which is what
`Finding.agreed_by` is. It is agreement rather than a self-reported number
because a model's confidence in its own answer is uncalibrated and a second
specialist reaching the same conclusion is not.

## 5. The refutation round, and why it is off by default

Measured after the split and the premise filter were in, on
`gemini-3.5-flash-lite` (the weakest model available, and the only one with
free-tier quota left that day), no datasheet facts supplied: four findings on
three demo boards, **every stated premise confirmed by the spec**, and three
of the four reasoning to a wrong conclusion anyway.

* blinker, blocker: "the astable timing network locks the timer state",
  quoting eight net memberships, all of them correct, on a textbook-correct
  555 astable.
* blinker, marginal: "missing bulk capacitance on the input rail", whose own
  confirmed premises name `c_bulk_vcc` on `VIN` and `GND`.
* reference, blocker: "op-amp channel 2 configured with positive feedback
  resulting in latch-up", on a standard unused-amplifier follower.

**The premise filter cannot reach this class, because its premises are true.**
Only a second look at the inference can. So `run_review(refute=True)` spends
one more call in which every surviving finding must defend itself —
`audit/effort.py`'s `refute_rounds` at `deep`, batched into a single call
rather than one per finding, because one call per finding is a cost worth
paying only where a person asked for it. Survival is an allow-list: a finding lives only on an explicit JSON
`false`, and a missing verdict, a malformed one or an unreadable answer all
refute, which is `audit/judgment.py`'s stated rule.

Run against the same three boards it killed exactly the two false blockers,
with correct reasoning in both cases — "the discharge pin is connected between
both timing resistors... allowing it to properly discharge the timing
capacitor", "the standard, recommended termination technique for an unused
op-amp channel".

**It also killed the true one.** The LDO ESR finding was refuted as relying on
"generic assumptions about older LDO architectures and unstated capacitor
properties not present in the netlist" — which is a fair reading of a netlist
with no datasheet attached, and wrong about the AMS1117. That is the cost the
prompt's "default to refuting when you are unsure" instruction buys on
purpose, and it is why the round is **off by default**: it trades a real
finding for two false ones, which is the right trade for some readers and not
for others, and it is a model call nobody pressed for. That measurement was
taken with no datasheet facts supplied; the reviewer-demo path supplies the
AMS1117 datasheet, and whether a cited ESR requirement survives refutation is
untested — free-tier quota ran out.

## 6. Provenance, and why this does not reopen the simulation question

`Finding` still carries no provenance field in the `audit/findings.py`
`Origin` sense. `Domain` says which specialist raised a finding; every one of
its values means "a model argued this", and none of them means "something
measured this". So a simulation verdict — which is a measurement — still does
not belong on `PipelineResult.findings`, for exactly the reason it did not
before.

`Finding.checks` is the nearest thing to evidence here, and it is deliberately
weaker than `audit`'s `evidence`: it records what the *spec* said about a
premise the model stated, never what a measurement said about the board.

## 6. Left undone

* ~~**No refutation round.**~~ Built (§4) and, since the integration pass of
  2026-09-08, reachable. This section said it was left out because "the engine
  critic has no effort slider to hang the cost on; `audit` has one and the
  pipeline does not" — which was true when this document was written and
  stopped being true in the same working tree, when `agents/effort.py` landed.
  `run_review(refute=True)` is now `EffortProfile.refute`, on at `thorough`
  and off below it, reported on `EffortReceipt` because it is the one axis
  that makes a *higher* effort level report *fewer* findings and a shorter
  list with no explanation reads as a cleaner board.
* **Contradictions between specialists are only half detected.** A severity
  disagreement on one claim is detected and reported. A semantic contradiction
  — one specialist saying a value is too large and another saying it is too
  small — is not, and nothing here pretends it is.
* **The premise vocabulary is four kinds.** Several defect classes have no
  checkable premise at all (dielectric, ESR, thermal), so those findings are
  kept on the strength of the ref filter alone and say so in `checks`.
* **No measurement of how often the premise filter fires on real answers.**
  It was exercised end to end and every premise the model stated on the LDO
  was confirmed; free-tier daily quota ran out before a large enough sample
  could be taken to say how often it drops something.
