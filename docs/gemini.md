# Gemini models and how Silkscreen calls them

This document covers one hackathon requirement: the project must use Gemini 3.5 or newer,
called through the Gemini API or Vertex AI. It records what the requirement asks for, what
the code actually does today, and where the gaps are. The requirement is restated in the
repo's own `CLAUDE.md:39`.

## What the requirement asks for

Two things have to be true at once. The model has to be at least Gemini 3.5, so a 2.5-family
or 2.0-family model would not qualify. And the call has to go through one of Google's two
first-party surfaces: the Gemini Developer API, which authenticates with an API key, or
Vertex AI (recently rebranded by Google as the Gemini Enterprise Agent Platform), which
authenticates with Google Cloud credentials scoped to a project and a location. Both
surfaces are reachable from the same `google-genai` Python SDK, which is the package
Silkscreen depends on.

## How Silkscreen satisfies it today

The short answer is that it satisfies the model-version half cleanly and the call-surface
half only through the Gemini API, not Vertex AI.

Worker-stage generation funnels through `GeminiModel` in
`engine/silkscreen/agents/model.py`: it imports `google.genai`, constructs a client, and
issues one `generate_content` request per call. The presentation path adds a genuine ADK
`LlmAgent` in `engine/silkscreen/agents/adk/orchestrator.py`; ADK owns that root Gemini call
and exposes its model and tool callbacks. `GeminiEmbedder` in
`engine/silkscreen/agents/retrieval.py` separately calls `embed_content` for the datasheet
retrieval index, and `service/models.py` uses `models.list` for read-only UI discovery.

The agent stages sit above that seam and never see the SDK. The datasheet reader calls
`model.generate` at `engine/silkscreen/agents/datasheet.py:109`, the circuit proposer at
`engine/silkscreen/agents/propose.py:135`, and the adversarial reviewer at
`engine/silkscreen/agents/review.py:128`. All three receive a `Model` — the structural
protocol declared at `model.py:57` — rather than a concrete client, and `generate_pcb` in
`engine/silkscreen/agents/pipeline.py:65` threads one model object through the whole run.

The dependency is declared as an optional extra rather than a base requirement. In
`pyproject.toml:20` the `agents` extra pins `google-genai>=2.19,<3`, and the `cloud` extra
at `pyproject.toml:21` repeats that pin and adds Firestore alongside it. The upper bound
is deliberate: the SDK has an announced breaking 3.0, and the bound is what stops it
arriving in a fresh install without anyone deciding to take it. The base dependency list at
`pyproject.toml:13` contains only OR-Tools and kiutils, which is what keeps the deterministic
engine free of any model dependency at all.

### Keeping the tests offline

`ScriptedModel` at `model.py:175` is the reason the test suite needs no key and no network.
It satisfies the same `Model` protocol, returns canned strings either in order from
`responses` or by matching a substring of the prompt against `by_marker`, and records every
call it received in `calls` so a test can assert on what was actually asked. The
substring-matching path is what lets one model object drive several different agent stages
inside a single test.

`engine/tests/test_agents.py` uses it for all 22 of its tests and says so in its module
docstring. `service/tests/test_app.py:9` imports it for the HTTP service tests. No test in
the repository constructs a `GeminiModel` or reads `GOOGLE_API_KEY`, which is why the CI
workflow at `.github/workflows/ci.yml:20` can install the `agents` and `cloud` extras and run
`pytest -q` on three operating systems without any secret configured.

The failover layer is tested the same way. `engine/silkscreen/agents/resilience.py:74`
defines `FallbackModel`, which tries each `Provider` in order, validates the returned value
through `_validate` at `resilience.py:57`, and moves to the next provider when a response is
empty or is not a string. `engine/tests/test_resilience.py` forces each of those failure
shapes with small stub classes — one that raises, one that returns a streaming iterator, one
that returns whitespace, one that returns `None` — so every fallback branch is executed
rather than assumed.

## Which model IDs the code names

Three constants sit at the top of `model.py`, and the service names one more model of its
own.

| Constant | Value | Where it is used |
|---|---|---|
| `DEFAULT_MODEL` (`model.py:34`) | `gemini-3.7-flash` | CLI default, primary provider in the service, what `auto` resolves the root to |
| `CHEAP_MODEL` (`model.py:37`) | `gemini-3.5-flash-lite` | second-tier provider in the service |
| `GEMMA_MODEL` (`model.py:43`) | `gemma-4-31b-it` | **last-resort rung only**, one attempt, after both Gemini tiers fail (see below) |
| `_PRO_ORCHESTRATOR_MODEL` (`service/models.py`) | `gemini-3.1-pro-preview` | optional root selection in the web form; **below the 3.5 floor and refused unless the operator opts in** (see below) |

The two Gemini constants — the primaries on every path — satisfy the requirement. Google's model list documents `gemini-3.7-flash` as the
current, most capable Flash model, released August 2026, accepting text, image, video, audio
and PDF input with a 1,048,576-token input window
([model page](https://ai.google.dev/gemini-api/docs/models/gemini-3.7-flash)). It documents
`gemini-3.5-flash-lite` as a stable, low-latency, cost-oriented multimodal model that also
accepts PDF input and carries the same 1,048,576-token input window
([model page](https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash-lite)). Neither
is a 2.5-family or older model, so the version floor is met with room to spare.

The tiering is a cost decision. On the paid tier Google lists `gemini-3.7-flash` at $0.75 per
million input tokens and $3.75 per million output tokens through 31 December 2026, against
$0.30 and $2.50 for `gemini-3.5-flash-lite`
([pricing](https://ai.google.dev/gemini-api/docs/pricing)). The comment at `model.py:32`
describes the cheaper tier as being for high-volume mechanical passes, which matches how
Google positions Flash-Lite for document parsing and subagent work.

Retrieval uses a third model. `EMBED_MODEL` at `retrieval.py:40` is `gemini-embedding-001`,
which is generally available in both the Gemini API and Vertex AI
([announcement](https://developers.googleblog.com/gemini-embedding-available-gemini-api/)).
It is an embedding model rather than a generative one, so it sits outside the "Gemini 3.5 or
newer" version scheme and does not affect the requirement either way.

### The Gemma last resort

`service/app.py:build_model` wires a three-rung `FallbackModel`: two attempts on
`gemini-3.7-flash`, two on `gemini-3.5-flash-lite`, then **one** attempt on the open-weights
`gemma-4-31b-it`, served through the same Gemini API. The rung exists for resilience, not
routing: it only fires after four Gemini attempts have failed (rate limit, 5xx, timeout, or
unusable text), on the reasoning that a different model family is unlikely to share the
Gemini tiers' outage or quota pool. It is honest about firing — every result carries
`served_by`, and a run that reads `gemma-open` was designed by Gemma, not Gemini, and
should not be presented as a Gemini run. On a healthy key the rung never fires; the
key-gated `test_live_gemma_rung_answers` in `engine/tests/test_live_model.py` is the only
thing that exercises it against the real API, and the offline
`test_build_model_chain_ends_on_the_gemma_rung` in `service/tests/test_app.py` pins the
order. The CLI uses a bare `GeminiModel(DEFAULT_MODEL)` and has no Gemma rung at all.

### The Gemini 3.5 floor on the root model

The worker chain meets the floor by construction, but the ADK root model is selectable from
the web form, and the fallback catalog in `service/models.py` lists `gemini-3.1-pro-preview`
as a "deeper reasoning" option — a model numbered below 3.5. Rather than hide it,
`select_model` enforces the floor at the point of selection: `meets_version_floor` parses
`gemini-<major>.<minor>` and an explicit choice below `(3, 5)` is refused with a 400 naming
the floor, unless the service runs with `SILKSCREEN_ALLOW_LEGACY_MODELS=1`. `auto` (the
default, resolving to `gemini-3.7-flash` or `SILKSCREEN_ORCHESTRATOR_MODEL`) is never
gated, and every catalog entry carries a `legacy` flag so a client can grey the option out
before the request is made. Ids without a version prefix pass through, so a naming change
on Google's side cannot turn into an outage here. `service/tests/test_models.py` covers the
parse, the refusal, and the opt-in. The web form still lists 3.1 Pro today; without the
opt-in, choosing it returns the 400 rather than running a sub-floor root.

### Changing the model

Passing `--model` on the command line overrides the worker model for one CLI run.
`DEFAULT_MODEL` and `CHEAP_MODEL` define the service's worker fallback chain. The web chat
starts in **Auto**: `GET /models` calls `client.models.list()`, keeps models advertising
`generateContent`, and the service accepts only one of those advertised IDs for an explicit
or retry selection. `SILKSCREEN_ORCHESTRATOR_MODEL` controls which root model Auto resolves
to without changing the worker chain. If discovery is unavailable, the response is clearly
marked as a fallback catalog containing the configured worker IDs.

## Gemini API versus Vertex AI

The `google-genai` SDK is a single client that can point at either backend, and which one it
picks depends entirely on how `genai.Client` is constructed.

For the Gemini Developer API you pass an API key, either explicitly as
`genai.Client(api_key=...)` or implicitly by setting `GOOGLE_API_KEY` or `GEMINI_API_KEY` in
the environment. The SDK's own key resolution reads `GOOGLE_API_KEY` first and falls back to
`GEMINI_API_KEY`, warning if both are set
([`_api_client.py`](https://raw.githubusercontent.com/googleapis/python-genai/main/google/genai/_api_client.py),
lines 130-141).

For Vertex AI you pass `enterprise=True` together with `project` and `location`, or set
`GOOGLE_GENAI_USE_ENTERPRISE=true`, `GOOGLE_CLOUD_PROJECT` and `GOOGLE_CLOUD_LOCATION` and
construct the client with no arguments
([SDK README](https://github.com/googleapis/python-genai)). The older spelling still works:
the `vertexai` keyword is documented in the client source as a legacy alias for `enterprise`,
and the SDK raises if the two are set to conflicting values
([`client.py`](https://raw.githubusercontent.com/googleapis/python-genai/main/google/genai/client.py),
lines 271-380). The same holds for the environment variables, since `GOOGLE_GENAI_USE_VERTEXAI`
is still read alongside `GOOGLE_GENAI_USE_ENTERPRISE`, with the enterprise variable winning a
conflict (`_api_client.py`, lines 656-676). Google began rebranding Vertex AI to the Gemini
Enterprise Agent Platform in 2026 while leaving the `google-genai` package name and proto
namespaces unchanged, which is why both spellings coexist.

Silkscreen uses the API-key path only. `GeminiModel.__init__` reads `GOOGLE_API_KEY` at
`model.py:120`, raises `ModelError` if it is absent, and constructs `genai.Client(api_key=key)`
at `model.py:126`. `GeminiEmbedder` does the same at `retrieval.py:135` and `retrieval.py:138`.
Neither class accepts a `project` or `location` argument, and neither passes `vertexai` or
`enterprise`, so there is no code path in the repository that reaches Vertex AI.

The environment file is misleading on this point. `.env.example` lists `GOOGLE_API_KEY`,
`GOOGLE_CLOUD_PROJECT` and `GOOGLE_CLOUD_LOCATION`, which reads like Vertex AI configuration.
It is not. `GOOGLE_CLOUD_PROJECT` is consumed at `service/app.py:75`, where its presence is
the signal to use Firestore instead of an in-memory fact cache. `GOOGLE_CLOUD_LOCATION` is not
read anywhere in the codebase at all; grepping the repository finds it only in
`.env.example:4` and in a prose sentence in `CLAUDE.md:43`. The CLI loads `.env` into the
process environment at `cli.py:53` using a small hand-rolled reader that calls
`os.environ.setdefault`, so an already-exported variable wins over the file.

One consequence is worth stating plainly. Because `GOOGLE_CLOUD_PROJECT` and
`GOOGLE_CLOUD_LOCATION` land in the environment but `GOOGLE_GENAI_USE_ENTERPRISE` is never
set, the SDK still selects the Developer API. Setting the project and location alone does not
switch backends.

### What a Vertex AI path would need

Adding one is small. `GeminiModel.__init__` would need to accept a project and location, and
to build the client as `genai.Client(vertexai=True, project=..., location=...)` when those are
present instead of requiring `GOOGLE_API_KEY`. The provider chain in `resilience.py` would
then let a Gemini API provider and a Vertex AI provider sit in the same list, which would make
the "no single point of failure" framing in `resilience.py:1` true across surfaces rather than
only across model tiers. Vertex AI also authenticates through Application Default Credentials
rather than a key, which matters on Cloud Run because the service account is already attached
and no secret would need to be set.

## Things worth flagging

The earlier `media_resolution="high"` bug is resolved. Google accepts
`MEDIA_RESOLUTION_LOW`, `MEDIA_RESOLUTION_MEDIUM`, `MEDIA_RESOLUTION_HIGH` and
`MEDIA_RESOLUTION_UNSPECIFIED`
([media resolution docs](https://ai.google.dev/gemini-api/docs/generate-content/media-resolution));
`GeminiModel` now sends `MEDIA_RESOLUTION_HIGH` for document requests. An offline regression
test captures the generated request config, specifically covering the document-only branch
that exposed the bug.

The live path now has a key-gated smoke test that calls the cheap model and verifies a marker
in its response. It skips unless `GOOGLE_API_KEY` is set, so the default suite remains offline
and free. The document request-config test is fully offline, which lets CI catch invalid enum
values without uploading a PDF or spending a model call.

The `generate_content` surface is being repositioned. Google's current documentation labels
several `generate_content` pages as "Gemini Generate Content API (Legacy)" and shows a newer
`client.interactions.create` surface in the Gemini 3 developer guide
([Gemini 3 guide](https://ai.google.dev/gemini-api/docs/gemini-3)). The `generate_content`
method Silkscreen calls at `model.py:163` is still documented and still shown in current model
pages, so nothing is broken today, but the naming suggests the surface will eventually move.
Because every call is behind the `Model` protocol at `model.py:57`, migrating would touch one
method body.

There is no retry or backoff inside `GeminiModel` itself. The class wraps any SDK exception in
`ModelError` at `model.py:166` and re-raises. Retry lives one layer up in `FallbackModel`,
which is a reasonable split, but it means a bare `GeminiModel` — which is what the CLI
constructs at `cli.py:63` — has no retry at all. The CLI is therefore less resilient to a
transient 5xx than the HTTP service is.

The README and DEVPOST claims match the code. DEVPOST describes the tiering as
`gemini-3.7-flash` for datasheet vision and reasoning dropping to `gemini-3.5-flash-lite`
behind it, with Gemma as the named last resort, which is exactly what
`service/app.py:build_model` wires up. The README's setup
instructions at `README.md:102` and `README.md:205` ask for `GOOGLE_API_KEY` and nothing else,
which is honest about the Developer-API-only reality even though `.env.example` is not.

## Verdict

The requirement is met on both counts as written. The primary models on every path are
`gemini-3.7-flash` and `gemini-3.5-flash-lite`, both newer than the 3.5 floor, and they are
called through the Gemini API using the official `google-genai` SDK. The two exceptions are
stated rather than hidden: Gemma is a one-attempt last resort that a result names in
`served_by` when it fires, and the sub-floor `gemini-3.1-pro-preview` root option is refused
by the service unless an operator opts in with `SILKSCREEN_ALLOW_LEGACY_MODELS=1`. The requirement offers
Vertex AI as an alternative rather than an addition, so not supporting it is not a failure.
The stale `GOOGLE_CLOUD_LOCATION` entry in `.env.example`, which implied a Vertex AI
configuration path that does not exist, was removed on 2026-09-06. The invalid `media_resolution`
string previously noted here has been corrected and covered by a regression test.
