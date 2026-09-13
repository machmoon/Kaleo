# Does this pipeline design correct circuits?

*Written 2026-09-08 with `scripts/design_quality.py`, which is the harness this
document describes. Re-run it after any change to `agents/propose.py`,
`agents/plan.py` or the prompt they carry.*

Nobody had measured this. Every quality gate in the repository checks something
*downstream* of the question: DRC checks geometry, the enclosure kernel checks
the case, `audit/rules.py` measures the board, and the critic in `agents/review.py`
asks a model for an opinion about a model's output. None of them can tell a 3.3 V
supply from a 3.3 V supply wired backwards.

`spice/registry.py` changed that on 2026-09-08 by giving the repository a
trusted, licence-clean source of device behaviour for op-amps, 555 timers, fixed
linear regulators and connectors (`docs/spice-models.md`). For the first time
the *proposal* can be scored against a specification written in volts and hertz
rather than against a schema.

    python scripts/design_quality.py --offline-selftest   # no key, no cost
    python scripts/design_quality.py --trials 3 --json /tmp/dq.json
    python scripts/design_quality.py --trials 3 --plan    # with plan.py on

It lives in `scripts/` and pytest never collects it, the
`scripts/simulate_demo.py` convention: it spends real model calls and needs
`GOOGLE_API_KEY`. The tests stay offline.

Every run below is kept verbatim under `docs/measurements/`, and
`--rescore <file>` re-applies every deterministic check to a saved run without
spending a call. Use it whenever a structural rule or an oracle changes: a
scoring change applied only to new runs cannot be compared with an old number.

    python scripts/design_quality.py --rescore docs/measurements/design-quality-2026-09-08-before.json

## What is measured, and how

Nine prompts. The first six are the demo prompts at the top of `TODO.txt`; the
last three were written for this harness in the same spirit and stay inside
`board.supported_packages_text()`. Each is proposed `--trials` times through
`agents.propose.propose_circuit` with `facts=[]` and `max_repairs=3` — the
propose stage alone, not the whole pipeline.

Three results are recorded per trial and deliberately never folded into one
number.

**1. Did the IR validate, and after how many repair rounds.** This is the only
thing the repository could already measure.

**2. Structural compliance** (`structural_check`), deterministic and free:

* the part the request named is actually in the proposal;
* the prohibitions in the request are obeyed ("do not add connectors, headers,
  switches, buttons, or test points");
* a power entry exists where the request implies one (`propose.py` rule 10);
* every supply pin the registry can identify has a capacitor to ground.

The decoupling rule is only applied to devices the registry bound, because those
are the only ones whose supply pin can be identified without guessing. A part it
cannot read is **not** scored as compliant — it is not scored, and the run says
so. That is `registry.why_unmatched`'s discipline applied to a rule.

**3. Does the circuit work**, by simulation, against clauses written from the
*English request*:

| Case | The specification, in words |
|---|---|
| `ldo` | driven at 5 V, the rail sits at 3.3 V ±5% into 330 Ω and never exceeds 3.63 V |
| `blinker` | on 9 V the output oscillates between 0.5 and 2 Hz and swings half the supply |
| `ref25` | on 5 V one section is a unity-gain follower sitting at 2.5 V ±5% |
| `attiny` | structural only — no SPICE model of a microcontroller exists |
| `barrel_ldo` | driven at 9 V on the jack, the rail sits at 3.3 V ±5% into 330 Ω |
| `usbc_ldo` | driven at 5 V on VBUS, the rail sits at 3.3 V ±5% into 330 Ω |
| `ldo5v` | driven at 12 V, the rail sits at 5 V ±5% into 500 Ω |
| `blinker10` | on 5 V the output oscillates between 5 and 20 Hz |
| `halfrail` | on 3.3 V one section is a follower sitting at 1.65 V ±5% |

Every clause restates the *request*. A clause of the other kind — "the period is
`ln2·(R1+2R2)·C`" — would restate whatever the model happened to choose and
would pass a blinker that blinks once an hour. `test_spice.py` is where that
kind of clause belongs, and it is there; it is not what this measures.

### The oracle never guesses a net name

`spice/registry.py` binds each device's pins to its subcircuit's terminals, so
`SubcircuitModel.pins` says which of the *model's own* pin names is VOUT, which
is VCC and which is IN−. `net_for_terminal` reads a probe point out of that
binding plus `CircuitSpec.nets_of`. A board that calls its rail `VOUT_3V3`
instead of `+3V3` is scored on its electrons, not its vocabulary.

Loads and supplies are added by the harness. The IR says nothing about what
drives a board — `spice/deck.py`'s stated boundary, *"a circuit is not a
testbench"* — so the fixture belongs to the bench, never to the design under
test.

### The instrument is checked before it is trusted

Two things had to be built into the harness because the instrument is not
uniformly reliable, and both are worth knowing independently of this
measurement.

**`.op` is not usable on the op-amp stand-in.** ngspice's DC operating point on
a unity-gain follower built from `library.opamp_subckt` reports `gmin stepping
failed` / `source stepping failed` and then writes a well-formed rawfile holding
a **non-solution**: a 2.5 V follower came back at 0.053 V, self-inconsistent
with its own inputs (inputs 2.500 V and 0.053 V into a ×100000 stage clamped to
the rails cannot produce 0.053 V). `verify()` does not refuse on those warnings,
so an `.op` verdict would have scored a correct circuit as broken. `settled_dc`
therefore runs a short transient and samples the last decile; the same circuit
then reads 2.49997 V. This is exactly the ngspice behaviour `docs/spice-models.md`
names as its counter-example — *exits zero and produces a well-formed and
entirely misleading answer* — reaching the agent path through a different door.

**The 555 stand-in cannot hold its latch for a second.** `calibrate_astable`
builds the textbook astable (R1 = R2 = 100 kΩ, C solved from
`T = ln2·(R1+2R2)·C`) for the target rate and simulates it on the same bench. If
a circuit that is correct *by construction* does not measure within 10 % of its
closed-form rate, the fault is in the model or the analysis and the oracle
refuses rather than reporting a design failure. It refuses at 1 Hz and passes at
10 Hz. See "A bug found in `spice/library.py`" below.

## The measurement, 2026-09-08

**Model: `gemini-3.5-flash-lite`, not the default.** The intended subject was
`gemini-3.7-flash` (`agents/model.DEFAULT_MODEL`). Twelve trials in, the key hit
`GenerateRequestsPerDayPerProjectPerModel-FreeTier, limit: 20` on that model and
on `gemini-3.1-pro-preview`, and the day's quota does not come back. `flash-lite`
is `agents/model.CHEAP_MODEL` and the second rung of the production failover
chain (`service/app.py`), so it is a model the product really runs — but it is
the lesser one, and the numbers below are its numbers. The partial 3.7-flash
data agreed with it on the one failure mode that mattered.

Two trials per prompt, nine prompts, eighteen proposals, `--trials 2`.

### Before

```
proposals attempted            18
validated (IR accepted)        18/18
clean on the first round       10/18
named the part requested        2/18
reached a simulated verdict     0/18
scored on function             16
CORRECT (structure + spec)      0/16
broke an explicit prohibition    8   (reported, not scored)
```

**Zero.** Not one of the eighteen proposals could be checked for function, and
the reason was the same every time.

### The failure: an IC named `U1`

Sixteen of eighteen proposals keyed their integrated circuit by **reference
designator** rather than by part number — `U1` every time — even when the
request said *"an AMS1117-3.3 in SOT-223"*, *"an NE555D in SOIC-8"*, *"an LM358D
in SOIC-8"*. The two exceptions were both `barrel_ldo`, which wrote
`AMS1117-3.3` correctly.

The IR accepted every one of them, because `netlist.CircuitSpec.validate` only
requires a device key to be a unique valid identifier. Nothing downstream can
survive it:

| Consumer | What it does with the key | With `U1` |
|---|---|---|
| `sourcing.bom_rows` → `distributor.verify_mpns` | the manufacturer part number to look up | nothing to order |
| `models3d.model_for` | matches a KiCad 3D model | no model; `pcb export glb` draws a bare substrate |
| `spice/registry.ModelRegistry._builtin` | matches the op-amp / 555 / regulator families | `unsimulatable` |
| `schematic.py` | drawn on the symbol | a schematic that does not say which chip |
| `agents/review.py` | the part the critic checks against a datasheet | nothing to check |

Five silent losses, and the run still reports success. This is precisely the bug
class `kicad.py`'s `_placer_ref` identity rules exist to raise on rather than
no-op — a name that looks fine and means nothing.

Example, `ldo` trial 1 (unedited):

```json
"devices": {"U1": {"pins": {"GND": "1", "OUT": "2", "IN": "3"}}, ...}
```

The circuit is otherwise right: input cap on VIN, output cap on the rail, ground
common. It is a correct regulator board that nobody can build, order, simulate
or check, because it does not say what the regulator is.

### The change

`agents/propose.py`:

* **`part_number_errors(spec)`** — a new rule shaped exactly like
  `board.package_errors` and merged into the same batched list, so one repair
  round addresses both. It rejects an `ic` whose key is a bare reference
  designator (`U1`, `IC2`, `REG`), a category word (`opamp`, `regulator`), or
  carries no digit at all — and only an `ic`, because a connector, battery,
  switch or test point is identified by its `package` (rule 9), so `J1` is the
  right name for a barrel jack.
* **Rule 11 in `PROPOSE_PROMPT`**, and `"<part number>"` in the JSON template
  became `"<MANUFACTURER PART NUMBER>"`.

Each error message names what to write instead, not only what is wrong — that
half is copied from instructor's `instructor/v2/core/validators.py::Validator`,
whose fields are `is_valid` / `reason` / **`fixed_value`**. The loop itself was
already the right shape and was extended, not replaced: instructor's
`instructor/v2/providers/genai/handlers.py::reask_genai_structured_outputs`
appends *"Validation Error found:\n{exception}\nRecall the function correctly,
fix the errors in the following attempt:\n{...}"*, and LangChain's
`langchain_classic/output_parsers/prompts.py::NAIVE_FIX` has the same three
parts under delimiters (instructions / completion / error / try again).
`propose.py`'s repair prompt already carried all three in that order.

### After

Identical harness, identical prompts, identical model.

```
proposals attempted            18
validated (IR accepted)        18/18
clean on the first round       12/18
named the part requested       18/18      (was 2/18)
reached a simulated verdict     6/18      (was 0/18)
scored on function              6
CORRECT (structure + spec)      5/6
broke an explicit prohibition    7   (reported, not scored)
```

**Read the denominators, not just the fractions.** 0/16 and 5/6 are not the same
kind of number. Before the change, sixteen trials were *scored* because a
structural failure is a failure whatever the simulator says; after it, every
structural check passes, so scoring falls through to the verdict and a trial
with no verdict drops out of the denominator entirely. The two figures that are
directly comparable are the two that moved for one reason: **the proposal names
the part, 2/18 → 18/18**, and **a functional verdict was reached at all, 0/18 →
6/18**.

Of the six verdicts, five passed and one failed.

### The one genuine design failure

`blinker10` trial 2, asked for *"a roughly 10 Hz LED flasher"*, proposed
`Ra = 1 kΩ`, `Rb = 68 kΩ`, `C = 10 µF`. The astable rate those imply is
`1.44 / ((Ra + 2·Rb)·C)` = **1.05 Hz** — a factor of ten slow — and the
simulation says so from the other direction: over a window sized for 10 Hz the
output never leaves its high rail, `peak_to_peak` measures 0 V against a
required 2.5 V, margin −2.5 V. Everything else about the board is right: the
decoupling, the control-pin capacitor, the LED resistor, the trigger/threshold
tie. It is the arithmetic that is wrong, and no schema check could have caught
it. One failure of one scored trial is not a rate; it is a demonstration that
the harness detects the thing it was built to detect.

### With the plan stage on

`--plan --trials 1` (nine proposals) reached 5/9 verdicts with 4/5 correct,
against 6/18 and 5/6 without. At this sample size those are indistinguishable
and no conclusion about `agents/plan.py` should be drawn from them; `plan.py`
was not changed. The one qualitative difference: the planned `barrel_ldo`
dropped the test pad the request asked for, which the structural check does not
currently require and so did not score.

### Why twelve of eighteen still have no verdict

None of these is a design failure, and none is scored as a pass. Every one is a
limit of the instrument, in a file this lane does not own.

| Count | Cause | Where |
|---|---|---|
| 2 | the 555 stand-in's latch drifts at 1 Hz | `spice/library.py::timer555_subckt` — see below |
| 2 | a microcontroller has no SPICE model anywhere | `spice/registry.py::_REFUSALS`, correct and permanent |
| 2 | no port model for a **test point** | `spice/registry.py::_builtin` ports only `connector`/`battery` |
| 2 | no port model for a **switch** | same |
| 2 | a **0 Ω link** is refused as a zero-valued resistor | `spice/deck.py` value parser |
| 1 | the pin spelled `THRESH` does not read as the 555's THRES | `spice/registry.py` alias table |
| 1 | the pins spelled `VCC+` / `VCC-` do not read as op-amp supplies | `spice/registry.py::_SUPPLY_POSITIVE` |

The last four are one-line additions and would raise the scorable count from 6
to about 12 of 18 without touching `propose.py` at all. They are listed as
findings for the SPICE lane rather than fixed here.

## A bug found in `spice/library.py`

`timer555_subckt` holds its latch state in

```
BQD QD 0 V = (V(RESET,GND) < 0.7) ? 0 : ( (V(TRIG,GND) < V(REF13,GND)) ? 1 :
             ( (V(THRES,GND) > V(CTRL,GND)) ? 0 : V(Q) ) )
RQ QD Q 1k
CQ Q 0 1p
```

The hold branch is the literal expression `V(Q)`, fed back through a 1 ns RC.
There is no restoring nonlinearity, so the stored state is a pure integrator of
numerical error and drifts across a long hold. Measured on ngspice 47:

| Network | Closed-form rate | Measured | Timing node swing (should be V+/3 → 2V+/3) |
|---|---|---|---|
| 10 k / 10 k / 100 nF | 480.8 Hz | 479.6 Hz | 3.00 – 6.00 V |
| 100 k / 100 k / 1 µF | 4.808 Hz | 4.807 Hz | 3.00 – 6.00 V |
| 470 k / 470 k / 1 µF | 1.024 Hz | **1.904 Hz** | 3.00 – **5.08** V |
| 10 k / 10 k / 47 µF | 1.022 Hz | **2.486 Hz** | 3.00 – **5.10** V |

The last two are the same rate through completely different impedances and fail
identically, so it is the hold *duration* and not the source impedance. The
threshold comparator fires early because `V(Q)` has drifted below 0.5 before the
timing capacitor reaches 2/3 of the supply. Gear integration makes it worse, not
better (2.968 Hz), which rules out trapezoidal ringing.

The suggested one-line fix is to make the hold branch regenerative —
`(V(Q) > 0.5) ? 1 : 0` instead of `V(Q)` — so the latch snaps to a state rather
than remembering a float. **Not applied here**: `spice/**` belongs to another
lane. Until it is, `calibrate_astable` refuses a verdict at 1 Hz and says why,
which is the honest behaviour and is why the headline 1 Hz blinker demo is
currently unmeasurable.

## Two other findings

**`.op` on the op-amp stand-in returns a non-solution.** Covered above under
"The instrument is checked". Worth repeating because `agents/simulate.py` may be
exposed to it: `verify()` does not refuse on a run whose warnings say
`gmin stepping failed` / `source stepping failed`, and such a run can carry a
number that contradicts its own inputs. A model asked to propose a testbench
will reach for `"op"` — it is the obvious analysis for a DC reference — and get
a confident wrong answer.

**`74HC595` cannot be expressed in the IR at all.** `netlist.CircuitSpec.validate`
requires a part name to be a valid Python identifier, so no 74-series logic part
— the most common family there is — can be named. Recorded rather than worked
around, and pinned by
`engine/tests/test_propose_part_numbers.py::test_a_part_number_starting_with_a_digit_cannot_be_expressed_at_all`,
because a prompt rule telling the model to write a part number the IR then
refuses would be a repair loop that cannot converge.

## A second defect, a change that failed, and why it was reverted

Across all runs, every 555 proposal fixed `C` at **10 µF** and varied only the
timing resistors:

| Request | R1 / R2 / C | Implied rate `1.44/((R1+2R2)C)` |
|---|---|---|
| ~1 Hz | 10 k / 100 k / 10 µF | 0.686 Hz |
| ~1 Hz | 10 k / 68 k / 10 µF | 0.986 Hz |
| ~1 Hz (with plan) | 100 k / 68 k / 10 µF | 0.610 Hz |
| **~10 Hz** | 1 k / 47 k / 10 µF | **1.52 Hz** |
| **~10 Hz** | 1 k / 68 k / 10 µF | **1.05 Hz** |
| **~10 Hz (with plan)** | 1 k / 68 k / 10 µF | **1.05 Hz** |

The model is not solving the astable equation. It anchors on a capacitance and
nudges the resistors, which spans well under a decade — so the 1 Hz request is
met by luck of the anchor and the 10 Hz request is missed by a factor of ten,
three times out of three. `blinker10` is 0/3.

**The change tried.** A design rule in `PROPOSE_PROMPT`: *when the request
states a number, compute the part values that meet it*, with the governing
expression and two worked examples of the arithmetic — one of which used
`C = 100 nF`.

**The result, three fresh trials of `blinker10`, identical harness:**

```
reached a simulated verdict     3/3
CORRECT (structure + spec)      0/3      (was 0/3)
```

measuring 58.7 Hz, 78.8 Hz and 58.7 Hz against a 5–20 Hz specification. In all
three the timing capacitor was **exactly 100 nF** — the number in the worked
example — with resistors of 47 k/100 k, 47 k/68 k and 47 k/100 k. The rule did
not make the model compute anything. It replaced one anchor with another, and
the anchor it supplied was mine. The error changed direction (10× slow became
6–8× fast) and the measured number did not move.

**Reverted.** A change that does not move the measured number gets reverted and
reported as such; that is the whole reason the harness was built before the
change. The finding is more useful than the fix would have been: *a worked
example in a prompt becomes the next anchor*, so the next attempt at this should
carry no numeric example at all, or should move the arithmetic out of the prompt
entirely — a deterministic post-check that recomputes the rate a proposed timing
network implies and returns the difference through the existing batched repair
loop, the way `part_number_errors` does. That check needs a topology recogniser,
which is a larger piece of work than this lane had budget to measure.

## The prohibition contradiction

Seven of eighteen proposals added a connector or a battery holder to a request
that said *"do not add connectors, headers, switches, buttons, or test points"*.
That is **reported and not scored**, because `propose.py` rule 10 tells the model
in capitals that *"THE BOARD MUST HAVE A POWER INPUT ... as a real part"*, and
`TODO.txt`'s prompt list says the connector ban "is gone" — while the demo
prompts underneath it still carry the ban verbatim. The prompt and the request
contradict each other and the prompt wins. Which should win is a product
decision; scoring the pipeline for obeying its own instructions would be a
measurement of the contradiction, not of the pipeline. It is raised as a
`TODO.txt` row instead.

## What this does not prove

Read this before quoting a number from here anywhere.

1. **A simulation passing is not a board working.** Every clause is checked
   against a *generic stand-in* — `SS_OPAMP_LM358` is not an LM358,
   `SS_LDO_AMS1117_3_3` has no control loop at all. `docs/spice-models.md`
   §"The honest limits" is the full list and is short enough to read.
2. **This scores one stage.** Nothing here checks the schematic, the placement,
   the copper, the case or the BOM. A proposal that passes every clause can
   still fail DRC, fail to route, or be unmanufacturable.
3. **A case with no oracle is in neither the numerator nor the denominator.**
   The ATtiny board is scored structurally and is not a pass.
4. **A circuit can meet every clause and still be a bad design** in ways nobody
   wrote a clause for — thermal, EMC, protection, sequencing, ESR, footprint
   choice, test access. The clauses here are the ones in this file.
5. **The sample is small.** Nine prompts times a handful of trials is enough to
   find a systematic failure mode and much too small to distinguish 80 % from
   90 %. Treat a moved number as evidence only when the *failure mode* moved
   with it, which is why every failure is reported in full.
6. **One change was tried and reverted**, and its measurement is in
   `docs/measurements/design-quality-2026-09-08-reverted-timing-rule.json`. The
   pipeline still gets a stated frequency wrong by a factor of ten.
7. **Temperature is 0.0 and the model still varies.** `propose_circuit` calls
   with `temperature=0.0`; the answers still differ between trials, so a single
   trial is not a measurement.
