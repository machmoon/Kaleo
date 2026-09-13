# Silkscreen Placement Agent

Silkscreen repairs PCB placement faults while following an explicit company
profile. The same damaged board can produce two different legal layouts because
compact-control and thermal-first teams optimize different preferences.

The end-user entry point is simpler than the verifier lab: name a chip and the
job the board should do. Silkscreen turns that request into a circuit proposal,
retrieves any supplied manufacturer evidence, validates the circuit, places the
parts, verifier-gates that placement, routes it, runs the independent review,
and returns a KiCad board. The placement lab remains an inspectable proof
surface underneath that flow.

## What runs today

Deterministic repair is the default product path and needs no model call. Gemini
is a stable comparison policy when the configured key advertises it. Ollama,
Tinker, hybrid, automatic experimental selection, and trace capture appear only
after the user turns on **Experimental features**. Provider-specific choices are
selectable only when their server-side credentials or endpoint are configured.
Backend names remain visible in the run receipt.

Generated boards cross an explicit adapter boundary: canonical placement stays
in integer nanometres, verifier geometry uses bounded millimetres, and accepted
coordinates are converted back before routing. CP-SAT may produce an outline
too tight for a stricter company margin or clearance; the adapter minimally
grows and translates that frame so a legal repair is geometrically possible.
If bounded repair still cannot prove `H = 0`, no verifier coordinates are
written back and the response says `applied: false`.

On the private-GPU stopgap, base Gemma 3 4B can run through a configured Ollama
endpoint.
Deterministic search produces a bounded candidate set and Gemma selects a short
ordered batch. The geometry verifier evaluates each action and commits only the
longest improving prefix. This is speculative placement execution, not
token-level speculative decoding.

The visible score has two axes. `H` is hard penetration in millimetres from
overlap, clearance, boundary, and keepout faults. A legal placement has `H = 0`.
`P` is the weighted company-preference cost for grouping, connector access,
compactness, and thermal separation. Lower is better. The old combined internal
ranking scalar is deliberately not exposed.

## Agent, policy, verifier, and training

The **agent** owns the multi-turn loop. It assembles the board and company
profile, asks a policy for candidate actions, applies verifier feedback, and
decides whether another turn is needed.

The **policy** proposes actions. Today that can be deterministic search, Gemini,
or base Gemma through a configured Ollama endpoint. Hybrid mode tries the fast
policy first and uses Gemini only when no proposed action is accepted.

The **verifier** is authoritative. It recomputes `(H, P)` after each action and
accepts an action only if that pair improves lexicographically. Hard geometry
therefore always outranks preference. Neither Gemini nor the small policy can
override it.

**Supervised fine-tuning** teaches a small policy the action grammar and repair
patterns from deterministic traces plus accepted engineer corrections. Tinker
does not currently list Gemma in its
[supported training-model catalog](https://tinker-docs.thinkingmachines.ai/tinker/models/),
so the included Tinker recipe targets Qwen 3.5 4B. The project ships the data
exporter, training recipe, and checkpoint loader. No hosted checkpoint is
claimed without a successful training run and held-out promotion gate.

The training helper uses Tinker's Qwen 3.5 non-thinking renderer because the
runtime sampler also disables thinking and expects action lines directly. Install
the isolated dependency set and generate deterministic conversations before a
training run:

```bash
pip install -e ".[training]"
python scripts/export_placement_sft.py artifacts/placement-sft.jsonl --count 100
python scripts/train_placement_tinker.py artifacts/placement-sft.jsonl \
  --test-size 20 --log-dir artifacts/tinker-placement-sft
```

Training refuses malformed JSONL, a split with no training examples, or a
non-empty log directory. After evaluation and promotion, configure only the
resulting `tinker://...` sampler checkpoint as `TINKER_PLACEMENT_MODEL`; the
service deliberately refuses an untrained base-model name.

**Reinforcement learning** comes only after SFT. Its portable reward is outcome
for a legal board, normalized progress in `H`, then a small preference reward
for legal boards. RL may improve the policy, but it never replaces the runtime
verifier. A promoted policy must beat the SFT checkpoint on a frozen held-out
set without reducing legality.

## Failure memory for post-training

Every rejected Qwen or local-policy proposal can become a verifier-backed
training example. The trace stores the exact board, company profile, prompt,
raw proposal, accepted prefix, per-action verifier receipts, and the run's
reward. Its preferred target is the successful Gemini recovery when one
exists, otherwise the deterministic repair oracle. Terminal boards that remain
illegal at the turn limit are recorded too.

Failure traces are never recorded by default. Recording requires both the
experimental feature gate and a separate explicit `record_trace: true` consent.
When enabled, local runs append JSONL under `artifacts/`; a deployment may inject
a different trace store. The export script produces both chat messages for SFT
and chosen/rejected pairs for later preference training. Collection does not
mean automatic training or promotion. A new checkpoint still has to beat the
frozen held-out legality and preference gates.

Hard rules include boundaries, clearance, keepouts, and fixed components. Soft
preferences include grouping, connector access, compactness, and thermal
separation. Engineer corrections are structured profile updates. The lab stores
them in `sessionStorage`, scoped to one browser tab, so anonymous visitors never
share a server record and the correction disappears with the tab session. The
public API applies feedback to one request only. Durable team memory requires
authentication and a real tenant ownership boundary.

## Honest claim

The placement proof is deterministic rectangle geometry integrated before the
project's existing router. It is not electrical validation, fabrication
readiness, or proof that an SFT or RL policy outperforms the deterministic
baseline.
