# Device models for SPICE: what ships, what does not, and why

*Written 2026-09-08 with the registry it describes.*

The SPICE verifier is the only thing in this repository that checks whether a
circuit *works* rather than whether a board can be *made*. Until this change it
could only check circuits made of resistors, capacitors and inductors: a
`Device` in the Silkscreen IR is a pin map with no behaviour, so every board
that mattered — the AMS1117 LDO, the NE555 blinker, the LM358 reference, the
ATtiny85 board — came back `unsimulatable`.

The obvious fix is the one that must never be built. If a language model is
asked for a `.SUBCKT`, it will produce one, the deck will simulate, and the
clauses will **pass**. A passing verdict carries authority; a passing verdict
built on an invented model is worse than no verdict at all. So the only source
of device behaviour on this path is `engine/silkscreen/spice/registry.py`, and
the testbench JSON schema has no field a model could smuggle SPICE text
through.

## Two sources, never confused

### 1. Generic behavioural stand-ins (`spice/library.py`)

Written here, from published circuit topology, under this repository's licence.
Four of them:

| Model | Covers | Terminals |
|---|---|---|
| `opamp_subckt` | general-purpose op-amps, 1/2/4 sections (LM358, LM324, TL07x, MCP600x, …) | `IN+ IN- OUT` per section, then `V+ V-` |
| `timer555_subckt` | 555 timers | `GND TRIG OUT RESET CTRL THRES DISCH VCC` |
| `linear_regulator_subckt` | fixed-output three-terminal regulators (AMS1117-3.3, LM7805, AP2112K-3.3, …) | `GND VOUT VIN` then optional `EN` |
| `port_subckt` | connectors, headers, battery holders | one per pin |

Every one is marked `generic=True`, and that mark is load-bearing rather than
decorative. It travels onto `SubcircuitModel.generic`, into a deck warning that
names the part and quotes the model's provenance, and into
`SimulationResult.substitutions` and `generic_parts`. "The LM358 result" and "a
generic op-amp's result" support different claims, and a reader who cannot see
which one they have will assume the first.

Each entry also carries a `note` saying what it does **not** answer, in words
that reach the caller's result. The regulator's is the sharpest: it has no
control loop inside it at all, so it is silent on stability, output-capacitor
ESR, transient response, PSRR and current limit — which are most of the reasons
anyone simulates an LDO.

### 2. Operator-supplied model files

`SILKSCREEN_SPICE_MODELS` is an `os.pathsep`-separated list of directories.
Every `.lib`, `.sub`, `.cir`, `.mod` and `.spice` file under them is walked and
each `.subckt` block indexed by name, the shape KiCad's
`SIM_LIBRARY_SPICE::ReadFile` uses. A subcircuit whose name matches the part
name (exactly, or with non-alphanumerics folded to `_`, or with them removed)
is used **in preference to a stand-in**, because the whole point of a generic
is that it is what you use when you have nothing better.

Its provenance says exactly what it is: the file's path, `kind: "operator"`,
`generic: False`, and a licence field reading *"not asserted by silkscreen;
whatever the operator's file carries"*, with a note saying this repository has
not read, verified or vetted it. It is trusted exactly as far as the operator
who installed it — and no further, which is why nothing here claims otherwise.

## Why nothing is vendored

This is a licence question, and the answer is not "manufacturer models are
free". They are freely *downloadable*, which is a different thing.

* **Texas Instruments.** The TI TEXT FILE LICENSE
  (`https://dev.ti.com/gallery/assets/TI_Text_File_License.txt`) permits source
  redistribution only where derivative works are *"licensed by TI for use only
  with TI Devices"* — a field-of-use restriction, not an open licence. The older
  SPICE macro-model agreements in TI application notes sloa070/sloa071 go
  further and grant no right to *"sell, load, rent, lease or license the SPICE
  macro-model … to anyone other than the user"*.
* **Analog Devices.** Its model files carry a *"nonexclusive, nontransferable"*
  statement limiting copies to use *"within their company only"*, with no
  sell/loan/rent/lease/sublicense.
* **ngspice's own model page** (`ngspice.sourceforge.io/modelparams.html`) ships
  nothing itself; it points outward at four third-party collections and tags the
  page GNU FDL — a documentation licence applied to netlist text, too ambiguous
  to vendor from.
* **KiCad ships no device model libraries either.** Confirmed on the KiCad
  10.0.6 install on this machine: no `.lib`, `.sub`, `.cir` or `.mod` file
  anywhere under `KiCad.app/Contents/SharedSupport`. It ships the
  `Simulation_SPICE` *symbol* library and expects the user to supply the models.
* A negative example worth naming: `dnemec/SPICE-Libraries` on GitHub has no
  LICENSE file and its README says the files were collected from *"various Yahoo
  groups and all around the internet"* with *"all rights reserved to their
  respective owners and authors … whom are unknown to me"*. That is exactly the
  provenance-free collection this registry exists not to become.

So: this repository ships models it wrote, and reads models the operator
installed. If you have a licence to use a vendor model, put it in a directory
and point `SILKSCREEN_SPICE_MODELS` at it. That is an honest answer, and it is
the only one available.

## Prior art, and where this deliberately differs

**KiCad, `eeschema/sim/`.** A symbol resolves to a model through the
`Sim.Device` / `Sim.Type` / `Sim.Library` / `Sim.Name` / `Sim.Pins` field
convention (`sim_model.h` defines those strings). `SIM_LIBRARY::FindModel`
returns `nullptr` on a miss and substitutes nothing;
`SIM_MODEL::ReadTypeFromFields` reports *"No simulation model definition found
for symbol '%s'."* through its `REPORTER` and yields a marked
`SIM_MODEL_SPICE_FALLBACK` placeholder rather than a working component. Refuse
and say so — that is the behaviour this registry copies, and it is why an
uncovered part is named in `Resolution.uncovered` with a sentence from
`ModelRegistry.why_unmatched`.

**ngspice, the counter-example.** Handed an undefined `.model` it does not
refuse: it substitutes default parameters for the primitive type and prints
`Unable to find definition of model <name> - default assumed`. The run then
produces a well-formed and entirely misleading answer. That is the failure this
package was written to prevent, and it is why a generic stand-in here is a
warning that `Testbench(strict=True)` promotes to an error.

**Where this differs: pin binding.** KiCad binds symbol pins to subcircuit
terminals through `Sim.Pins`, holding `<symbol-pin>=<model-pin>` pairs (the form
`MigrateSimModel` writes when converting a legacy schematic). This registry has
no such field: the Silkscreen IR's pin *names* were chosen by the proposing
model, and its pin *numbers* came from the same place, so neither is
authoritative. Binding is therefore by name against an alias table, and a
terminal that matches no pin — or matches two — is a **refusal naming the
terminal**. A wrong name refuses loudly; a wrong number would have simulated a
different circuit quietly. That trade is made on purpose.

## The strict rule, extended

`Testbench(strict=True)` has always promoted every deck warning to an error, so
that a run which quietly substituted a generic diode could not be presented as
evidence about the design. Devices now sit under the same rule, with one
addition: `Testbench.accept_generic_models` names the parts for which a stand-in
is **knowingly** accepted.

* Naming a part turns a *silent* substitution into a *declared* one. That is
  the only thing that stops `strict` refusing.
* The warning is still emitted, and still reaches the result. Accepting a
  stand-in silences the refusal, never the disclosure.
* Naming a part the circuit does not have is a hard error, not a no-op — the
  `edge_refs` / `rotatable_refs` convention. The acknowledgement's entire value
  is that it was checked.

`agents/simulate.py` declares exactly the parts the registry resolved as
generic, and publishes them in `SimulationResult.substitutions`, so the
declaration and the disclosure are the same act.

## What can now be verified, and what it concluded

Measured on this machine with ngspice 47. Every expectation is closed-form
theory computed inline, never a recorded output of the code under test.

| Board | Clause | Measured | Expected | Margin |
|---|---|---|---|---|
| AMS1117-3.3 LDO, 5 V in, 330 Ω load | rail = 3.3 × 330/(330+R<sub>out</sub>) | 3.29950 V | 3.29950 V | −0 V |
| same, 3.6 V in | rail ≥ 3.2 V | 2.49962 V | ≥ 3.2 V | **−0.7004 V** (fails; below dropout) |
| NE555 blinker, 5 V | period = ln 2 ·(R1+2R2)·C | 2.08496 ms | 2.07944 ms | −5.52 µs |
| | duty = (R1+R2)/(R1+2R2) | 0.664912 | 0.666667 | −0.00176 |
| | timing node swings V+/3 | 1.66667 V | 1.66667 V | −3.7e−7 V |
| LM358 non-inverting ×2, 12 V | divider sets input | 1.09091 V | 1.09091 V | −2.0e−8 V |
| | gain = 1 + R<sub>f</sub>/R<sub>g</sub> | 2.18177 V | 2.18182 V | −4.4e−5 V |
| | corner = GBW/(1+R<sub>f</sub>/R<sub>g</sub>) | 498.547 kHz | 500.000 kHz | −1.45 kHz |

The 555's residual error is the discharge transistor's on-resistance in series
with a 10 kΩ timing resistor, which is a modelled effect, not noise.

Still refused, each with its own reason:

* **DRV8837, and any IC not in a recognised family** — no model here and none
  read from a file; the reason names `SILKSCREEN_SPICE_MODELS` as the fix.
* **ATtiny85 and every microcontroller** — behaviour is firmware; no SPICE
  model of it exists, here or anywhere.
* **LM317 and other adjustable regulators** — the output is set by external
  resistors, so a fixed-output stand-in would simulate a different rail than the
  board produces, and pass.
* **A fixed regulator whose output voltage is a manufacturer order code**
  (`MCP1700-3302`, `XC6206P332`) — the coding schemes differ between
  manufacturers and a wrong guess simulates a rail the board does not have.
  Only `3.3`, `3V3` and two digits meaning tenths are decoded; the refusal names
  the spelling that works.
* **Crystals** — a motional RLC network is four numbers the IR does not carry,
  and a stand-in built from the nominal frequency would *decide* the
  oscillator's frequency instead of measuring it.
* **A pin map the stand-in cannot read**, or an operator subcircuit whose
  terminal matches no pin — named, and never silently downgraded to a generic.

## The honest limits

Read this part before quoting a passing clause anywhere.

1. **A generic stand-in supports a claim about a class of part, not about a
   part number.** `SS_OPAMP_LM358` is not an LM358; it is a single-pole op-amp
   whose subcircuit name says which part it was substituted for. Every result
   says so, but a number lifted out of a result and pasted into a report does
   not carry its warning with it.
2. **The op-amp is exactly first-order, so it has no phase margin to measure.**
   Any stability answer it gives is a property of the model. It also has ideal
   inputs — no offset, no bias current, no common-mode limit, no slew-rate
   limit — so an input near a rail behaves better than any real part would.
3. **The regulator has no control loop.** It answers headroom and load
   regulation. A clause about its transient response or its output capacitor is
   a clause about nothing.
4. **The 555's timing is right and everything else is approximate.** Supply
   current, edge rates, comparator delay and CMOS variants are not modelled; the
   output drop and discharge resistance are bipolar-NE555 numbers baked in as
   constants.
5. **A connector is modelled as open terminals.** Whatever it connects to must
   come from the testbench; the model invents no source, and a battery holder
   gets the same treatment for the same reason — its voltage is not in the IR.
6. **An operator-supplied model is unvetted by this repository.** It is not
   generic, so it does not warn, and `strict` does not question it. That is
   correct — it is the operator's model — but it means the trust boundary moved
   to whoever installed the file.
7. **`ngspice` remains the authority for the numbers**, and the closed-form
   expectations above are the authority for `ngspice`. Neither is an authority
   for whether the *specification* was the right one to write; that is what the
   `critical` flag and a human reviewer are for.
