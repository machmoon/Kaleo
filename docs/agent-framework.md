# Agent framework

## What the requirement asks for

The hackathon requires that a project use at least one Google agent framework. Four are
named as acceptable: the Agent Development Kit (ADK), the Google Gen AI SDK, the
Antigravity SDK, and Genkit. A project satisfies the requirement by building its
model-facing layer on one of those four, rather than on a third-party abstraction or a
hand-rolled HTTP client against a model endpoint.

## What Silkscreen uses

Silkscreen uses two of the four. The **Agent Development Kit**, the Python package
published as `google-adk`, drives the generation pipeline as a dynamic workflow. The
**Google Gen AI SDK**, published as `google-genai`, sits underneath it as the only model
interface. Because both packages are named on the accepted list, the claim does not depend
on which of the two a reader considers the framework: either one satisfies the requirement
on its own terms, and the project ships both.

Both are declared in `pyproject.toml` as optional dependency groups:

```toml
agents = ["google-genai>=2.19,<3", "pypdf>=4.0"]
cloud  = ["google-genai>=2.19,<3", "google-cloud-firestore>=2.16"]
adk    = ["google-adk>=2.8,<3"]
```

The base install deliberately includes neither. The engine that computes footprints, packs
parts, and writes KiCad files has no model dependency at all, and the split is stated in
`engine/silkscreen/agents/__init__.py`: "The engine below this package is deliberately
model-free so the parts that must be _correct_ can be tested without a network."

### The ADK layer

STATUS: the default engine is `adk` — flipped 2026-08-30 after the live-run gate passed
(one end-to-end run through the SPA against the live Gemini API, with an observed
provider failover). `SILKSCREEN_ENGINE=sdk` selects the straight-line driver as the
kill switch.

`generate_pcb` in `engine/silkscreen/agents/pipeline.py` is now a dispatcher. Its `engine`
parameter chooses which driver executes the run, and everything else about the function —
its signature, its return type, its `on_event` callback — is unchanged from before the
adoption.

`engine/silkscreen/agents/adk/workflow.py` expresses the read, propose, place, and review
pipeline as an ADK `Workflow` assembled from `@node` functions, with an orchestrator node
on the workflow's entry edge and one node per pipeline stage beneath it. The orchestrator
is an `async def` taking an ADK `Context`, and it awaits `ctx.run_node(...)` for each stage
in turn; the stage nodes themselves are plain synchronous functions, and each one returns
the run token it was handed rather than its results. Running ADK 2.8.0 on a development
machine established that a node's return value does pass back through `run_node`
unchanged, including the project's own dataclasses, which are not JSON-serialisable, but
the shipped workflow deliberately does not lean on that. Only the token string travels the
graph, and everything rich — the wrapped model, the extracted facts, the validated spec,
the solved board, and the emit closure — lives in the runner's registry under that token.
The reason is in `runner.py`'s own docstring: ADK state is session state, which is
serialised, persisted and echoed into ADK's event stream, so "a model object or a solved
board must never enter it". The validation repair loop was deliberately not
re-expressed as a graph cycle. It remains the bounded `for` loop inside the propose stage,
because what makes that loop converge is batching every validator error into a single
repair prompt, and that is a property of the prompt rather than of the control flow. A
graph cycle would have relocated the loop without improving it.

`engine/silkscreen/agents/adk/runner.py` runs that workflow in-process:
`Runner(node=workflow, session_service=InMemorySessionService())`, with the session created
up front and the run's parameters bound by name out of `state_delta`, which is ADK's
default parameter-binding mode for root-node arguments. Nothing on this path opens a port,
contacts an external session store, or requires credentials. ADK's own events are consumed
by the runner and dropped, while Silkscreen's events continue to flow through the
pipeline's own emit closure inside the stage bodies. An exception raised inside a node
propagates out of `run_async` as the original exception object, which is what lets existing
behaviour survive intact: a callback that raises in order to abort a run whose HTTP client
disconnected still aborts the run, and a `ModelError` still reaches `service/app.py`'s
cause-chain walk as itself rather than as a framework wrapper.

The load-bearing design decision is that neither driver owns the work. Both the SDK path
and the ADK nodes call the same stage bodies in `engine/silkscreen/agents/stages.py`
(`read_stage`, `propose_stage`, `place_stage`, `review_stage`). The two engines therefore
emit identical event streams by construction rather than by careful maintenance, and a
parity suite in `engine/tests/test_adk.py` pins that property by driving the same input
through both and comparing the event sequences. Parity is not cosmetic here.
`service/app.py` streams these exact event names as NDJSON frames from
`POST /generate/stream`, and the Svelte SPA ticks its live stage list from them, so a
renamed, reordered, or dropped event is a user-visible break two layers away.

Model access did not move. The nodes call the `Model` protocol in
`engine/silkscreen/agents/model.py`, the same seam every stage already used, so
`ScriptedModel` keeps the whole test suite offline and keyless under either engine, and
`FallbackModel`'s failover semantics survive unchanged — the `served_by` field the service
reports and the `model.retry` events the UI shows come from the code they always did.

The web presentation path adds a separate root above that workflow.
`engine/silkscreen/agents/adk/orchestrator.py` is a genuine ADK `LlmAgent`: it decides
between asking one electrically essential clarification and calling one high-level
`generate_board` tool. That tool wraps the existing validated pipeline rather than
replacing its stages, so placement, repair, routing, and review retain their typed
contracts. ADK's model and tool callbacks emit correlated request, response, start, done,
and error events to `POST /chat/stream`; raw payloads are present only on the debug stream.
The orchestrator itself uses ADK's native Gemini model adapter, while the worker stages
continue to use the tested `Model` protocol and fallback chain.

### Where the SDK is called

The worker pipeline has two direct Gen AI SDK call sites inside
`engine/silkscreen/agents/`. The ADK root described above uses ADK's native Gemini adapter
instead of those wrappers, and `service/models.py` uses the SDK's read-only `models.list`
operation to populate the UI's model choices.

The first is `engine/silkscreen/agents/model.py`. The class `GeminiModel` imports
`from google import genai`, constructs a `genai.Client(api_key=key)` from `GOOGLE_API_KEY`,
and calls `self._client.models.generate_content(model=self.model, contents=parts,
config=config)`. The `contents` list is assembled from an ordered sequence of document
parts followed by the prompt text. A document with a URL becomes a `file_data` part
carrying a `file_uri` and MIME type; a document supplied as raw bytes becomes an
`inline_data` part. The `config` dictionary carries `temperature`, `max_output_tokens`,
an optional `system_instruction`, and `media_resolution` whenever documents are attached.
Any exception from the SDK is re-raised as the module's own `ModelError`, and an empty
response text is treated as a failure rather than an empty answer.

The second is `engine/silkscreen/agents/retrieval.py`. The class `GeminiEmbedder` builds
the same `genai.Client` and calls `self._client.models.embed_content(...)` against
`gemini-embedding-001`, passing a `task_type` of `RETRIEVAL_QUERY` for questions and
`RETRIEVAL_DOCUMENT` for passages, plus an `output_dimensionality`. The asymmetry is
deliberate and commented as such: using one task type for both sides measurably degrades
retrieval quality.

Two SDK capabilities are load-bearing for this project specifically. Native PDF vision is
what makes datasheet reading possible, because a pinout table and a package drawing are
pictures and text extraction discards exactly the information the pipeline needs; the
Gemini API documents support for PDFs up to 50 MB or 1000 pages at 258 tokens per page,
including interpretation of diagrams, charts, and tables
([ai.google.dev](https://ai.google.dev/gemini-api/docs/document-processing)). The
`media_resolution` parameter, documented on the same page as a Gemini 3 control accepting
low, medium, or high, is set to the API enum value `MEDIA_RESOLUTION_HIGH` by default in
`GeminiModel.__init__` because datasheet pin tables are set in small type.

### Where the agent structure lives

The Gen AI SDK is the transport and ADK is the orchestration. The domain-specific agent
behaviour sits between them, in the stage bodies, and it is worth describing because it is
the substance of the claim that this is an agentic project rather than a project that
happens to call a model.

`engine/silkscreen/agents/model.py` defines a `Model` protocol with a single `generate`
method. Every stage talks to that protocol and never to the vendor SDK directly. Three
types satisfy it: `GeminiModel` for the live path, `ScriptedModel` for tests, and
`FallbackModel` in `engine/silkscreen/agents/resilience.py` for provider failover. Because
the seam is a protocol rather than a base class, a caller can substitute any of the three
without the calling code knowing which it has.

`engine/silkscreen/agents/stages.py` holds the stage bodies that both drivers execute: read
each supplied datasheet into structured facts, propose a circuit from those facts, build and
place a board with the constraint solver, and run an adversarial review pass. Each stage is
a separate model call with its own prompt and its own output contract, and the output of one
stage is the typed input to the next.

`engine/silkscreen/agents/datasheet.py` is the extraction stage. `read_datasheet` puts a
PDF in front of the model with a prompt that demands a page number on every requirement and
every recommended auxiliary component, and that instructs the model to omit anything the
document does not state. The result is parsed into `PartFacts`, and the function raises
rather than continuing if no pins were extracted. When the reported package pin count
disagrees with the number of pins actually extracted, a warning is appended to the notes,
because choosing a footprint from a wrong pin count produces a dead board.

`engine/silkscreen/agents/propose.py` is the propose-and-repair loop, and it is the part
that most clearly makes this an agent rather than a single completion. The model's proposal
never reaches the board builder directly. It goes through
`silkscreen.netlist.parse_circuit_spec`, which collects structural problems into a list and
then calls `CircuitSpec.validate()` to collect semantic ones. `ValidationError` carries the
entire list, one human-readable message per problem, and the loop feeds all of them back at
once:

```python
problems = "\n".join(f"  - {e}" for e in exc.errors)
prompt = (
    ...
    f"Your previous proposal was rejected. Fix ALL of these and "
    f"return the corrected JSON object:\n{problems}\n\n"
    f"Your previous proposal was:\n{raw}\n"
)
```

Batching every failure into one repair prompt is a design decision with a reason behind it.
Returning only the first error would make each round fix one problem and often introduce
another, so the loop would burn its budget without converging. The loop is bounded by
`max_repairs`, every round is informed by the previous one, and the full attempt history is
returned so a caller can report how many rounds it took. That count is exposed as
`PipelineResult.repair_rounds` and is a genuine quality signal about the proposal.

The loop also distinguishes two failure modes that a naive implementation would conflate.
A transport failure raises `ModelError` and is deliberately not wrapped in `ProposalError`,
because "the model was unreachable" and "the model could not produce a valid circuit" call
for different responses, and the Cloud Run service in `service/app.py` walks the exception
cause chain in `caused_by_model_failure` precisely so an upstream outage is not reported as
an internal error.

`engine/silkscreen/agents/review.py` is the critic. The reviewer is given the accepted
circuit and the datasheet facts and is prompted to refute the design rather than assess it,
on the stated reasoning that a model asked "is this correct?" says yes. Findings come back
as JSON with a severity of blocker, marginal, or note. The parser then filters the model's
own output against the circuit: any part reference in a finding that does not name a device
or passive actually present in the spec is dropped, so a finding never points at a component
that does not exist. Findings are sorted most severe first.

`engine/silkscreen/agents/resilience.py` handles failover. `FallbackModel` walks a list of
named `Provider` entries, retries each with exponential backoff capped at `max_backoff_s`,
and validates every response before accepting it — a provider that returns a non-string or
an empty string is treated as a failure and the chain moves on. `service/app.py` uses this
to put a cheaper model tier behind the primary one. Every attempt is logged, so a caller
can see which provider actually served a request.

### What keeps it testable

`ScriptedModel` in `model.py` is a deterministic stand-in that returns canned responses,
either in order or matched by a substring marker in the prompt, and records every call.
Because the whole package speaks to the `Model` protocol, `engine/tests/test_agents.py`
drives the entire prompt-to-PCB pipeline — including the repair loop and the failure paths
that only fire against a badly behaved model — with no network and no API key.

Adopting ADK did not weaken that guarantee, because ADK was adopted above the protocol
rather than across it. The workflow runs against `ScriptedModel` exactly as the SDK path
does, with an in-memory session service and no credentials, which is what makes the parity
suite in `engine/tests/test_adk.py` runnable in CI at all. The same approach applies to
retrieval, where `HashEmbedder` is a deterministic offline stand-in that hashes token
trigrams into a fixed-width bag of counts. Its docstring is explicit that it is not a
semantic model, and nothing in the codebase pretends otherwise. CI installs the extras and
runs `pytest` on Linux, macOS, and Windows, so both frameworks are present in CI while no
test requires a live call.

### Tool exposure over MCP

`engine/silkscreen/mcp/server.py` implements a Model Context Protocol server as JSON-RPC 2.0
over stdin and stdout, speaking `initialize`, `ping`, `tools/list`, and `tools/call` at
protocol version `2024-11-05`. It exposes seven engine operations as tools with JSON
schemas: `validate_circuit`, `build_board`, `emit_kicad_pcb`, `place_parts`,
`generate_footprint`, `simulate_circuit` and `spice_capabilities` — read the count off
`TOOLS` in that file rather than trusting a number in prose; this one had already drifted
once (it said five while `TOOLS` held seven), which is why `server.py`'s own docstring no
longer states a count. Note also that **nothing in this repository is configured as an MCP
client** — no `.mcp.json`, no `mcpServers` block, no sidecar — so these tools are a surface
an external client could drive, not a path the pipeline takes. In particular the two SPICE
tools are the only shipped door to `engine/silkscreen/spice/` besides
`scripts/simulate_demo.py` and direct library calls. The entry point is registered in
`pyproject.toml` as
`silkscreen-mcp`. The transport is separated from the dispatch — `handle` maps one request
dictionary to one response dictionary and never touches a stream — which is what makes the
protocol testable without spawning a process.

This is not itself one of the four named frameworks, and it should not be presented as one.
It matters here because tool exposure is the interoperability surface those frameworks
consume. ADK lists MCP tools among its supported tool types
([adk.dev](https://adk.dev/)), and the Antigravity SDK documents integration with external
MCP servers ([antigravity.google](https://antigravity.google/docs/sdk/overview/)). Any of
those frameworks could call Silkscreen's engine today without a line of adapter code.

## Is the Gen AI SDK an "agent framework"?

This deserves a direct answer rather than a deflection, because a judge could reasonably
raise it. It is also the question that produced the ADK adoption, so the answer is kept
here in full rather than replaced by its conclusion.

On the narrow reading, the question does not arise: the requirement names the Gen AI SDK as
one of four acceptable frameworks, and Silkscreen uses it as its only model interface. That
is the requirement met on its own terms.

On the broader reading — whether an SDK counts as an *agent* framework — the honest answer
is that the Gen AI SDK is a model-access SDK with agent-shaped primitives, not an
opinionated agent runtime. It provides a client, content generation, embeddings, file
handling, multi-turn chat, a live streaming API, and function calling, against either the
Gemini Developer API or Google Cloud
([github.com/googleapis/python-genai](https://github.com/googleapis/python-genai)). Its
function-calling support does include an autonomous loop: the Python SDK reference states
that "You can pass a Python function directly and it will be automatically called and
responded by default," with an automatic-function-calling config exposing a `disable` flag
and a `maximum_remote_calls` limit that defaults to 10
([googleapis.github.io/python-genai](https://googleapis.github.io/python-genai/)). What it
does not provide is the layer above that: agent hierarchies, session services, workflow
composition, and an evaluation harness. ADK provides those, which is why ADK exists as a
separate package that depends on the Gen AI SDK rather than replacing it.

That concession is now the stated reason for the adoption rather than a caveat attached to
its absence. The missing piece was the runtime, and the runtime is precisely what ADK was
brought in to supply. The pipeline's stage sequencing is a `Workflow` of nodes executed by
an ADK `Runner` over a session service, while the domain-specific parts — the validator, the
repair prompt, the adversarial critic, the solver — stayed where they were.

An earlier version of this document doubted that "wrapping ADK behind the existing `Model`
protocol ... would be difficult to defend as a real adoption." The doubt was correct, and
the shipped design is the opposite arrangement. ADK is not behind the protocol; it is above
it. The workflow owns the control flow and its nodes call the `Model` protocol, which is
the direction that makes the adoption load-bearing rather than decorative: remove ADK from
that path and there is no orchestration left on it, whereas removing a hypothetical
ADK-backed `Model` implementation would have changed nothing but the vendor of one
`generate` call.

There is one thing worth being candid about, and it did not change with the adoption.
Because Silkscreen's engine is deterministic and its correctness checks are exact, the model
is never given a tool-calling loop over the engine within a single generation. The workflow
nodes are Python functions the runtime calls in a fixed order, not tools the model may
choose to invoke, and validation happens in Python between calls rather than through the
SDK's automatic function calling. That is a deliberate choice — a solver result should not
depend on whether the model decided to invoke the solver — but it does mean the project
still does not exercise a model-driven agentic loop. The agent loop is the one in
`propose.py`.

## The four options, compared for this project

| Framework | Package | Languages | Status | What it gives you |
| --- | --- | --- | --- | --- |
| ADK | `google-adk` | Python, TypeScript, Go, Java, Kotlin | **In use.** 2.8.0 installed and running here; `requires_python >=3.10`; declares `google-genai>=2.19,<3` | Agent hierarchies, sessions, workflows, evaluation, deployment |
| Gen AI SDK | `google-genai` | Python (and other language SDKs) | **In use, underneath ADK.** 2.20.0 installed, released 25 Aug 2026, Python ≥3.10 | Model access, embeddings, files, chat, function calling |
| Genkit | `genkit` | TypeScript and Go stable; Python and Dart in preview | Not adopted; Python announced as Alpha in April 2025 | Flows, tools, plugins, browser dev UI |
| Antigravity SDK | `google-antigravity` | Python, with TypeScript and Go planned | Not adopted; preview, announced 19 May 2026, docs show v0.1.15 | Stateful agent runtime, safety policies, subagent delegation |

**ADK** is Google's dedicated agent framework, described on its site as "the open-source
agent development framework that lets you build, debug, and deploy reliable AI agents at
enterprise scale" ([adk.dev](https://adk.dev/)). It was announced at Google Cloud Next in
April 2025 as a framework for "the full stack end-to-end development of agents and
multi-agent systems"
([developers.googleblog.com](https://developers.googleblog.com/en/agent-development-kit-easy-to-build-multi-agent-applications/)).
It supports Python, TypeScript, Go, Java, and Kotlin, is model-agnostic — the site states
that "ADK can work with almost any generative AI model" — and provides agents, tools
including MCP and OpenAPI tools, session management, graph and multi-agent workflows,
criteria-based evaluation, and one-command deployment to Google Cloud. Silkscreen uses the
dynamic-workflow and session-service parts of that surface and, for now, none of the rest.

The version facts were checked against the installed package rather than only against the
documentation. `google-adk` 2.8.0 reports `requires_python >=3.10` and declares
`google-genai>=2.19,<3` as a direct dependency
([adk-python pyproject.toml](https://raw.githubusercontent.com/google/adk-python/main/pyproject.toml)),
which the installed `google-genai` 2.20.0 satisfies; Silkscreen's own `agents` and `cloud`
extras were tightened from the old unbounded `>=1.0` to the same `>=2.19,<3`, so the two
constraints cannot disagree. ADK's bundled `api_server` was evaluated and deliberately kept
off the request path: the service already owns a stdlib-only, same-origin HTTP surface whose
NDJSON streaming contract and static-asset behaviour the SPA and the Cloud Run image are
built around, so ADK runs in-process behind that handler rather than adding a second web
application in front of it.

**The Gen AI SDK** remains the model layer beneath ADK. It fits because the pipeline's
model-facing requirements are narrow and specific: native PDF vision at controllable
resolution, deterministic JSON output at temperature zero, and asymmetric retrieval
embeddings. All three are direct SDK calls.

An earlier version of this document argued that "adopting a heavier framework would add a
dependency without removing any code." Half of that is still true and should be stated
plainly: the adoption happened at the orchestration layer and has not yet deleted anything.
The repair loop and the review pass — the parts encoding the domain knowledge that makes the
project worth anything — were never candidates for replacement and still are not. What the
argument got wrong was the implied conclusion that nothing could ever be removed. The
planned `LlmAgent` re-expression described below is what retires the roughly ninety-line
`_EventingModel` wrapper in `pipeline.py`, whose whole job is timing model round-trips and
surfacing failover retries as events — bookkeeping an agent runtime does natively. Until
that lands, the dependency is paid for by the runtime it supplies, not by the code it
removed.

**Genkit** describes itself as "Google's open-source framework for building full-stack,
AI-powered and agentic applications for any platform" ([genkit.dev](https://genkit.dev/)),
offering flows, tools, plugins, and a browser-based developer UI for tracing and debugging
runs. Its centre of gravity is TypeScript and JavaScript, with Go also stable; Python and
Dart are marked preview on the site. Google announced Genkit for Python as Alpha in April
2025 and was explicit that both new SDKs were "in versions <1.0.0 and may have breaking
changes" ([firebase.blog](https://firebase.blog/posts/2025/04/genkit-python-go/)).
Silkscreen is a Python project whose engine depends on OR-Tools and kiutils, so the
strongest part of Genkit is in the wrong language, and the Python surface carries a
stability warning that a project shipping deterministic geometry should not take on.

**The Antigravity SDK** is a Python SDK "for building autonomous AI agents powered by
Antigravity and Gemini," providing "a secure, stateful runtime harness that handles tool
execution, context management, safety policies, and subagent delegation"
([antigravity.google](https://antigravity.google/docs/sdk/overview/)). It was announced on
19 May 2026 as a preview that gives "programmatic access to Google's premier Antigravity
coding agent," Python-only for now with TypeScript and Go on the roadmap
([antigravity.google blog](https://antigravity.google/blog/introducing-google-antigravity-sdk)).
Architecturally, the Python layer acts as a control plane over a bundled Go harness that
runs the agentic loop and sandboxes tool execution, and it can consume external MCP servers.
The fit question is about shape rather than quality. Its built-in toolset is oriented around
filesystem and terminal work for coding agents, and its value proposition is a managed
runtime for open-ended autonomous work. Silkscreen's pipeline is closed-ended: known stages,
a solver in the middle, and a file at the end. The documentation version shown is v0.1.15,
which is early enough that a demo-critical path should not depend on it.

## What the second framework added, and what it cost

This section previously argued the adoption in the conditional. It is kept here in the
indicative, because its central prediction held.

The prediction was that if the team added a second Google framework, ADK would be the
candidate and the seam in `agents/` — the `Model` protocol, and later the `on_event`
callback — would be where it attached. That is what happened. `on_event` was designed in
advance as an ADK-shaped seam, mirroring ADK's callback and event model without taking the
dependency, and when the dependency arrived the seam did not have to move: the ADK nodes
emit through the same closure the SDK path does. The `Model` protocol turned out to be the
attachment point in the opposite direction from the one first sketched, with ADK above it
calling through it rather than an ADK-backed implementation sitting inside it, and that
inversion is what let the offline test story survive the adoption unchanged.

ADK adds four things Silkscreen did not have. Its evaluation harness would let the
propose-and-repair loop be scored against a fixture set of intents, turning `repair_rounds`
from a number printed at the end of a run into a tracked regression metric. Its session
service would give the review stage memory across runs, so a finding raised once about a
part could be recalled when that part appears again. Its deployment integration could
replace the hand-rolled `http.server` handler in `service/app.py` with a managed runtime.
Its multi-agent workflow support would matter if the review stage were split into several
specialised reviewers — a power reviewer, a signal-integrity reviewer, a manufacturability
reviewer — arguing separately and having their findings merged. Of those four, only the
workflow runtime is in use today; the other three are available and unused.

The costs were the predicted ones, and they were paid rather than avoided. ADK is a large
dependency with its own transitive tree, including FastAPI, Starlette, uvicorn, and
OpenTelemetry, against an engine whose runtime dependencies are two packages — which is why
it is an optional extra and why the base install and the deterministic engine still pull
none of it. The `ScriptedModel` testing story did have to hold under the new driver, and it
does, because the driver calls the same protocol. And ADK's `google-genai>=2.19,<3` forced
the version decision the project had so far avoided: Silkscreen's own pins were unbounded
above and are now bounded to match.

The claim this section no longer makes is that orchestration was not worth a framework.
Orchestration is what ADK sells, the pipeline is orchestration, and running it on the
framework built for it is easier to defend than a hand-rolled sequencer doing the same
thing.

## Shipped root agent and remaining stage migration

The conversational root `LlmAgent` and its `generate_board` tool are implemented. The
remaining feature-12 work is narrower: re-express individual worker stages as ADK
`LlmAgent`s rather than workflow nodes that call the `Model` protocol by hand. That change
could eventually replace `_EventingModel` in `pipeline.py` with runtime callbacks, but only
after equivalent scripted-LLM and provider-failover adapters preserve the current offline
and resilience guarantees. Until then, the root uses ADK-native model access and the
workers deliberately retain `ScriptedModel` and `FallbackModel`.

The evaluation harness and the cross-run session memory described in the previous section
are the other two candidates, in that order. Both are additive, and neither is started.

The root's model is selectable from the web form, and the choice is bounded by the same
requirement the workers meet: `auto` resolves to `gemini-3.7-flash` (or
`SILKSCREEN_ORCHESTRATOR_MODEL`), and `service/models.py:select_model` refuses an explicit
selection below Gemini 3.5 — the listed `gemini-3.1-pro-preview` included — unless the
service runs with `SILKSCREEN_ALLOW_LEGACY_MODELS=1`. The worker chain the `generate_board`
tool drives ends on a one-attempt open-weights Gemma rung after four Gemini failures;
`docs/gemini.md` states both exceptions in full.

## What this document does not establish

Several things were checked in the code, against published documentation, or by running the
package locally, and a few were not. Stating which is which matters more than a clean-looking
claim.

The ADK behaviours described above — offline import, workflow construction, in-process
execution through `Runner` with an in-memory session service, state-based parameter binding,
pass-through of return values that are not JSON-serialisable, and unwrapped exception
propagation out of `run_async` — were established by running ADK 2.8.0 on a development
machine and observing the results, not by reading them off the published documentation. That
is strong evidence for this version and weaker evidence for ADK in general: a later 2.x
release could change any of it without contradicting anything cited here, which is what the
`<3` upper bound is for.

The ADK path has been exercised offline against `ScriptedModel`, including the parity suite,
and once end to end against the live Gemini API (2026-08-30: an AMS1117-3.3 intent through
the SPA, streamed stage events, an `optimal` placement, and a `FallbackModel` failover to the
cheap tier surfacing in `served_by` — the semantics the parity suite exists to protect). That
run is what flipped the default engine to `adk`.

At runtime, `GET /models` asks the Gemini API for the current key's models, filters for
`generateContent`, and caches the result for fifteen minutes. `Auto` resolves to
`SILKSCREEN_ORCHESTRATOR_MODEL` when set and otherwise to the configured default. If the
key is absent or discovery fails, the service labels its two configured IDs as a fallback
catalog rather than claiming they were verified live. The embedding model
`gemini-embedding-001` appears in the Gen AI SDK's own `embed_content` example.

No additional live model call was made for the conversational-root change. Its behaviour
is pinned with an offline `BaseLlm` fake, while the earlier workflow path has the live run
recorded above.

The `google-genai` pin was previously flagged here as unbounded above, with PyPI showing
2.20.0 and a notice of breaking changes planned for 3.0.0. That observation is resolved
rather than outstanding: the pin is now `>=2.19,<3`, matching ADK's own constraint. The note
is kept because the resolution is recent, and a reader comparing this document against an
older checkout would otherwise see a contradiction.

Genkit's Python stability was cross-checked in two places that agree in substance but not in
wording: genkit.dev marks Python as preview in its language selector, while Google's own
April 2025 announcement calls Genkit for Python Alpha. Either label supports the conclusion
drawn above, which is that it is not stable.

## Sources

- Agent Development Kit documentation — <https://adk.dev/>
- ADK announcement, Google Developers Blog, 9 April 2025 — <https://developers.googleblog.com/en/agent-development-kit-easy-to-build-multi-agent-applications/>
- `google-adk` on PyPI — <https://pypi.org/project/google-adk/>
- `adk-python` dependency declaration — <https://raw.githubusercontent.com/google/adk-python/main/pyproject.toml>
- Google Gen AI Python SDK repository — <https://github.com/googleapis/python-genai>
- Gen AI Python SDK reference, including automatic function calling and `embed_content` — <https://googleapis.github.io/python-genai/>
- `google-genai` on PyPI — <https://pypi.org/project/google-genai/>
- Gemini API document processing, page limits and `media_resolution` — <https://ai.google.dev/gemini-api/docs/document-processing>
- Gemini API function calling — <https://ai.google.dev/gemini-api/docs/function-calling>
- Genkit — <https://genkit.dev/>
- Genkit for Python and Go announcement, Firebase Blog, April 2025 — <https://firebase.blog/posts/2025/04/genkit-python-go/>
- Google Antigravity SDK overview — <https://antigravity.google/docs/sdk/overview/>
- Google Antigravity SDK announcement, 19 May 2026 — <https://antigravity.google/blog/introducing-google-antigravity-sdk>
