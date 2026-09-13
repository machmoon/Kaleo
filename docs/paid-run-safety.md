# Never bill the same press twice

A pipeline run is several Gemini calls and a solver budget. Nothing in this
repo can undo one. So every place a request can be *sent again* — a retry, a
fallback, a reconnect, a replay, a double click — is a place the user can be
charged twice for one board, and each of them has to be decided deliberately.

This is the audit, done 2026-09-08. It covers the web SPA, the desktop overlay,
the service's run-start surfaces and the model layer underneath them. Each row
is **CONFIRMED** (reproduced, with the reproduction named) or **REFUTED** (with
the evidence that it cannot happen). Nothing here is marked safe because it
looked safe.

## The rule, and where it comes from

> A request may be repeated only when the server cannot have acted on it.

That is not a rule invented here. It is what every well-used HTTP client does,
and the three we read say it three ways:

| Project | File | The rule |
|---|---|---|
| urllib3 | `urllib3/util/retry.py:197` | `Retry.DEFAULT_ALLOWED_METHODS` is `HEAD, GET, PUT, DELETE, OPTIONS, TRACE`. POST is **absent**, and the docstring says why: "we only retry on methods which are considered to be idempotent (multiple requests with the same parameters end with the same state)". `_is_method_retryable` refuses everything else before any status is even looked at. |
| openai-python | `src/openai/_base_client.py::_should_retry` | Retries 408, 409, 429 and `>= 500`, obeys an explicit `x-should-retry` header, and falls through to `return False`. **No 2xx is retryable**, and there is no branch that reads the body first and reconsiders. |
| stripe-python | `stripe/_http_client.py::_should_retry`, `stripe/_api_requestor.py::request_headers` | Retries 409 and `>= 500` — but only because `request_headers` puts an `Idempotency-Key` on **every** POST (`_generate_idempotency_key`: 16 random bytes, uuid-shaped). The comment on the 500 branch is explicit that the idempotency framework, not the client, is what makes the repeat safe. |

Two corollaries this repo now follows:

1. **A 200 is never a reason to send the request again.** Whatever is wrong
   with the response, the work is happening.
2. **A POST that starts work is only repeatable with a key** the server can
   recognise. Without one the honest options are "do not repeat" and nothing
   else.

---

## 1. CONFIRMED and fixed: the SPA's stream fallback ran the board twice

`frontend/src/lib/api.js::generateStream` fell back to `POST /generate` — a
whole second run — in three cases. One of them was a 404. The other two were
**200s**:

- a 200 whose `content-type` was not `application/x-ndjson` (a proxy that
  rewrites types, an error page from something in front of the service);
- a 200 whose body had no `getReader` (a shim, a transport that buffers).

`service/app.py::_generate_stream` sends its `200`, its headers and its
`run.accepted` frame **before** `self._run(payload, …)` is called. So by the
time the client is inspecting the content type, the read, the plan, the propose
and the solve are already under way. The fallback did not recover the run; it
started a second one and paid for it.

**Fix**: the fallback is now the 404 branch and nothing else. A 200 that cannot
be read raises an `ApiError('internal', …)` whose message says the run started
and was not sent again. `fallbackToOneShot` carries the three sources above in
its comment.

**Test**: `frontend/src/lib/api.test.js`, "does not send a second request when
a 200 arrives as something other than NDJSON" and its no-body twin. The
assertion is the *request count* (`fetch.mock.calls`), not the error kind,
because the count is the property that costs money. Both fail against the old
code and pass against the new.

What this does **not** do: it does not recover the run. The board is being
built and this client will never see it. There is no run id on the wire before
the first frame, so there is nothing to poll. That is a real gap, stated rather
than papered over — see §6.

## 2. CONFIRMED and fixed: cancel-then-start ran propose twice

Reproduction, in the desktop overlay:

1. Press Start. `useStepRun.start` → `POST /steps`.
2. Press Cancel before it answers. `useStepRun.cancel` aborts the fetch and
   clears the in-flight ref; `fail` sees no session id and returns the hook to
   `idle` — its own comment says "no session ever reached this client, so
   there is nothing to ask the engine about".
3. Press Start again.

The engine never heard about step 2. `service/steps.py::start` has no session
to be cancelled through — the session is only registered *after* propose
succeeds — so it reads the datasheets, plans and proposes to the end. Step 3 is
a second full start. Two runs, one board, and the first one's session id is
lost, so its cost is not merely doubled, it is stranded.

Every other step is safe from this and always was: `advance` takes
`session.lock` and raises `StepOrderError` on `step in session.done`, so a
second `POST /steps/<id>/place` waits for the first and is then refused with a
409. `POST /steps` is the one route with no such guard, because it has no
session yet.

**Fix**, in two halves, both Stripe's design:

- `service/steps.py::start_once` reads an optional `Idempotency-Key` header
  (`service/app.py::_step` passes it). A key still in flight raises
  `StepOrderError` → 409, the way Stripe answers a key whose request has not
  finished. A key that already answered replays that envelope verbatim — which
  *also* hands back the session id the client had lost. A start that **failed**
  forgets its key: nothing was produced, so it may be attempted again. With no
  header the behaviour is exactly what it was.
- `app/src/hooks/useStepRun.ts::startKeyFor` mints a key per press
  (`newIdempotencyKey`, shaped like stripe-python's
  `_generate_idempotency_key`) and **keeps** it while a start is unanswered, so
  the re-press after a cancel carries the same key. `settle` drops the key the
  moment a start answers, so "run that same intent again" is a second board and
  not a replay of the first.

**Tests**: `service/tests/test_steps.py` (replay spends no further model call,
in-flight is a 409 and leaves one session, different keys are different runs,
no key is the old behaviour, a failed start may be retried under its key, an
over-long key is a 400 before any work) and
`app/src/hooks/useStepRun.test.tsx::useStepRun start idempotency` (the reuse
test fails if `startKeyFor` always mints).

Bounds worth stating: `MAX_IDEMPOTENCY_KEYS` (64) keys are remembered, oldest
evicted, in this process only. Two Cloud Run replicas do not share them, the
same way `_SESSIONS` is not shared — the step routes have never worked across
replicas and this does not change that.

## 3. REFUTED, with evidence

| Path | Evidence it cannot double-charge |
|---|---|
| The desktop's `generateStream` (`app/src/lib/silkscreen/client.ts`) | Falls back to `generate()` only on `response.status === 404`, with the comment saying why. A 200 with no body throws; a read that dies mid-stream after a terminal frame keeps the result and never re-requests. |
| The desktop's step approvals | `useStepRun.run` refuses to start while `inFlightRef` is set (the "sends one request for a double approve" test), and the engine refuses a repeat under `session.lock` (§2). Two guards, either sufficient. |
| A step cancelled after its session exists | `cancel` aborts the fetch only; the engine finishes under the lock and records the step as done. The hook then resyncs from `GET /steps/<id>` and marks it unreceived. A re-press is a 409, not a re-run. |
| `POST /steps/<id>/view3d` | Routed around `advance` on purpose: it opens a viewer, spends no model call, and may be pressed any number of times. |
| The SPA's `chatStream` | Has no one-shot fallback at all, and says so in its docstring: replaying an agent turn can duplicate a paid tool invocation. |
| `frontend/src/lib/transport.js` | One `fetch`, no retry loop, no attempt counter. |
| The billing ledger | Not reachable from a duplicate: `reserve` raises on a `run_id` that already holds. See §5 for the larger point. |

## 4. CONFIRMED, deliberately not fixed (and why)

**`FallbackModel.generate` re-asks after a call that may already have spent
tokens** (`engine/silkscreen/agents/resilience.py`). A provider attempt that
raises — including `_validate` rejecting an empty or blocked answer, which is a
call the API has already billed — is retried up to `provider.attempts` times
and then handed to the next provider. So a failing call can cost more than one
call's worth of tokens.

This is left alone on purpose, and it is not the bug class this document is
about: the cost of a retry here is *one model call*, bounded by
`provider.attempts` and by the provider list, and it is what openai-python and
stripe-python both do for a 5xx. What the audit was looking for is a repeat of
a **whole pipeline run**, and this is not one. (`resilience.py` is also another
lane's file.) Recorded here so the trade is on the record rather than assumed.

**The SPA has no in-flight guard of its own.** `frontend/src/App.svelte`'s
`submit()` calls `chatStream` with no equivalent of `useStepRun`'s
`inFlightRef`. What actually prevents a second submit is the render:
`ConversationView` shows `IntentForm` only while the transcript is empty, and
the retry buttons only in `phase === 'error'`, and `startRun` moves the phase
synchronously. That holds for real clicks, and it is a property of the markup
rather than a guarantee the code makes. `App.svelte` belongs to the overlay
lane at the time of writing, so the guard is proposed, not applied.

**`POST /generate` and `POST /generate/stream` take no idempotency key.** Two
identical posts are two runs. No client in this repo sends the same one twice
(the SPA runs through `/chat/stream`, the overlay through `/steps`), so this is
a gap rather than a live defect, and closing it would mean holding a whole
board in memory per key. Stated, not built.

## 5. Billing: what reserve-then-commit does and does not cover

`billing/ledger.py` has the right shape — `reserve` before a run, `commit` to
the metered cost after, `run_id` refused if it already holds a reservation, the
whole balance a fold over an append-only ledger. **None of it is wired to a
run.** Nothing in `service/app.py` or `service/steps.py` calls `reserve`,
`commit` or `release`; the ledger is reached only from the Stripe webhook and
the balance routes.

So, plainly:

- A duplicated run is **not** double-charged against the ledger, because no run
  is charged against the ledger.
- It **is** double-charged in the only currency currently spent: Gemini calls
  against the project's key.
- If metering is wired up later, a duplicated run would take a *new* `run_id`
  and therefore reserve and commit twice. The `run_id` guard protects one run
  from being reserved twice; it is not a duplicate-run defence, and it must not
  be mistaken for one.

## 6. What is still open

- A run started by `POST /generate/stream` is unaddressable: no id is sent
  before the first frame, so a client that loses the stream cannot rejoin it,
  poll it, or cancel it. §1 stops the double spend and leaves the single spend
  invisible. The step routes do not have this problem (`session` is on every
  envelope), which is the model to copy if it is ever fixed.
- The idempotency keys live in one process. Two service replicas are two sets
  of keys, exactly as they are two sets of sessions.
- The web and desktop front ends still have nothing like the event-id memory
  `slackbot`, `zoombot`, `teamsbot` and `meetings` keep. Those guard against a
  *platform* re-delivering an event; the browser and the overlay have no such
  delivery layer, and after §2 the one route that could be re-delivered by a
  human has a key. If a queue or a webhook ever fronts a run, it needs the same
  memory those packages have and this document should stop saying otherwise.

---

## 7. A run you can name: addressable runs and wired metering (2026-09-08)

§6 listed two things left open. The first is closed and the second is now
possible, because they were one problem: **you cannot meter, rejoin or cancel a
run you cannot name.**

### 7.1 What a run id is, and where it appears

A run id is a string that names one execution of the pipeline. It is
**registered before any work starts** and it is **the caller's own if the
caller sent one**:

| Where | What |
|---|---|
| Request header `X-Kaleo-Run-Id` | Optional, on `POST /generate` and `POST /generate/stream`. Validated (`[A-Za-z0-9_.:-]`, ≤128), because it is echoed into a header, a JSON body and a URL path. Malformed is a **400**; already registered is a **409**. |
| Response header `X-Kaleo-Run-Id` | Always, on both routes, on success and on failure. |
| First NDJSON frame | `{"event": "run.accepted", "run_id": …}`. |
| Terminal frames | `run.done`, `run.error`, `run.cancelled` all carry it. |
| One-shot body | `run_id` on the 200 and on every error body. |
| `GET /runs/<id>` | The poll. |
| `POST /runs/<id>/cancel` | The cancel. |
| `GET /runs` | Everything this process still remembers. |

The header is the load-bearing half. `api.js::startedButUnreadable` exists
precisely for a 200 whose body this client cannot read; a run named only inside
that body would be unnameable in exactly the case that needs it.

Letting the *caller* name it goes one step further, and that step is upstream's:
LiteLLM's proxy reads `x-litellm-call-id` off the request and generates only as
a fallback (`litellm/proxy/common_request_processing.py`,
`ProxyBaseLLMRequestProcessing.common_processing_pre_call_logic`:
`self.data["litellm_call_id"] = request.headers.get("x-litellm-call-id",
str(uuid.uuid4()))`), and returns it on the same header
(`ProxyBaseLLMRequestProcessing.get_custom_headers`). Temporal makes it
mandatory (`temporalio/client/_client.py::Client.start_workflow`, `id: str`,
keyword-only). Celery is the counter-example — `celery/app/base.py::
Celery.send_task` does `task_id = task_id or uuid()` server-side — and the
difference matters here: a client that chose the id knows the run's name before
the request is sent, so a request that gets **no response at all** is still
pollable and cancellable. Both front ends now choose their own
(`api.js::newRunId`, `client.ts::newRunId`).

Reusing an id is refused (409), **not** replayed. That is deliberately
different from `steps.start_once`'s `Idempotency-Key`, which replays because it
*has* the envelope to replay. This registry keeps no result, so a "replay"
would report some other run's state as this caller's.

### 7.2 Rejoin, and what it honestly is not

`GET /runs/<id>` answers state (`running` | `done` | `failed` | `cancelled`),
the last stage seen, the event count, elapsed time, and what the run cost. It
does **not** hand back the board, and every response says so
(`result_available: false` plus a sentence).

That boundary is real, not laziness. Frames are not buffered and results are
not kept: a board is megabytes, and holding one per run turns a bounded
registry into a memory leak — which is exactly the objection §4 raised against
putting an idempotency key on `/generate`. Celery draws the same line:
`AsyncResult.get` needs a result *backend*, and with none configured you learn
a task's state and nothing more. `/steps` remains the surface that keeps
artifacts.

So the honest summary: **a lost stream is now visible and stoppable, not
recoverable.** The board still has to be built again. What changed is that the
user can see the first one finish, see what it cost, and stop it before it
costs more.

### 7.3 Cancel

`POST /runs/<id>/cancel` sets a flag. `RunRecord.check` reads it from inside
the run's own event callback and raises `RunCancelled`, so the run is abandoned
through **the mechanism the pipeline already had** — a callback exception
abandons the run, which `_generate_stream`'s `emit` already relied on to stop a
run whose client hung up. No second mechanism, no thread killing. This repo
already had one of these: `service/amend.py::check_cancelled` does it for a
*step* session, and the answer here reuses that module's vocabulary
(`cancelled`, `already_cancelled`, `in_flight`, `aborts_at`, `not_stoppable`,
`headline`) so two cancels cannot come to disagree about what they stopped.

Celery's `Control.revoke` is the same promise: a flag the worker consults, not
a kill (stopping in-flight work needs `terminate=True` and a signal). What the
answer therefore says is *requested*, never *stopped* — a run inside a long
solve or a long model call keeps going until it next emits, and that work is
still billed. `not_stoppable` says so.

Cancelling is idempotent, cancelling a finished run is not an error (the caller
learned the outcome late, which is the case the route exists for), and a cancel
never rewrites a terminal state.

### 7.4 Metering: what is wired

§5 said `billing/` was called from nothing. Re-checked before touching it:
`reserve`, `commit` and `release` still appeared nowhere outside `billing/` and
its own tests. `service/metering.py` is now the one module that knows both
sides — the `slackbot/runner.py` convention.

Around each `/generate` and `/generate/stream` run:

1. **`begin`** reserves an estimate (`KALEO_RUN_ESTIMATE_MKCU`, default 2000
   mKCU = two minutes) **before the 200 and before any model call**. A refusal
   here is a **402** and has cost nothing, which is the only thing that makes
   refusing honest.
2. **`finish`** commits the measured wall-clock (`billing.units.Meter`), so a
   run that came in under its estimate is charged the real number and the rest
   of the hold is released.
3. **`fail`** ends a run that errored, was cancelled, or whose client
   disconnected, charging `consumed_mkcu` — **what it used, not zero.**

Every rule the package already decided is respected: integer cents, integer
mKCU, overage rather than cutoff (a run that passes zero finishes and is billed
afterwards), and `OveragePolicy.limit_mkcu` as a real credit line that only
refuses *before* a run starts.

The closest live prior art is LiteLLM, not Stripe, and it independently reached
the same three invariants:
`litellm/proxy/spend_tracking/budget_reservation.py::reserve_budget_for_request`
holds a worst-case estimate at auth time, `reconcile_budget_reservation`
settles it to actual, and `litellm/proxy/auth/user_api_key_auth.py` states the
failure the hold prevents in the warning it prints when you switch it off —
"concurrent requests can each pass the spend check before their cost is
recorded". The cancel rule is upstream's too, verbatim:
`release_budget_reservation_on_cancel` says to "reconcile to the request's
input-token cost rather than refunding to zero… Refunding to zero would let a
caller abort pre-token to dodge that charge." LiteLLM also bills a broken
stream rather than losing it
(`hooks/proxy_track_cost_callback.py::_ProxyDBLogger.async_post_call_failure_hook`,
and `_bill_partial_streamed_spend_on_disconnect` under
`anyio.CancelScope(shield=True)`).

Stripe stays the source of the *vocabulary*, and the release of an over-hold is
stated inversely in stripe-python:
`stripe/params/_payment_intent_capture_params.py` documents `amount_to_capture`
as "must be less than or equal to the original amount" and `final_capture`
(default true) as the flag you set to false "to not release the remaining
uncaptured funds" — so the default *does* release the remainder.
`PaymentIntent.cancel` says the same for the abandoned case.

### 7.5 Metering: what it deliberately does not do

- **It is off unless `KALEO_METERING` is set**, and when off, every run reports
  `{"enabled": false, "state": "off", "reason": …}` — a sentence, not a silent
  zero. Misconfigured metering degrades to off *and names the reason*, rather
  than failing every run or billing against a ledger nobody meant to open.
- **`/steps` is not metered.** A step session's spend is spread over many
  requests with no defined end, so "one run" is not a thing the step routes
  have. Billing per session (at which step?) versus per step is a product
  decision, not an implementation one. Left open, deliberately.
- **`/chat/stream` is not metered.** The orchestrator turn calls the pipeline
  through `generate_board`, so its spend would need attributing at the tool
  boundary rather than at the HTTP boundary.
- **A ledger that cannot record does not fail the run.** The board exists and
  the customer has it; an unrecorded charge is a reconciliation problem, loud
  on stderr, reported as `state: "unrecorded"`.
- **Nothing calls Stripe.** No charge, no settlement, no network. The ledger is
  the same one `/billing/balance` reads (`billing_routes._ledger()`), so the
  two cannot disagree.
- **A duplicated run is still two runs.** §5's warning stands: a duplicate
  takes a *new* run id and therefore reserves and commits twice. The `run_id`
  guard protects one run from being reserved twice; it is not a duplicate-run
  defence.

### 7.6 Decisions a human should overrule if they disagree

1. **Metering defaults to OFF.** There is no account system
   (`billing/accounts.py::SingleAccountResolver` is the honest default), so
   defaulting to on would invent an answer to "who is billed". The alternative
   — on by default against the `local` account — makes every dev run draw down
   a ledger nobody set up.
2. **Who is billed: `SingleAccountResolver` ("local"), overridable with
   `KALEO_ACCOUNT`.** The alternative in the package is
   `StripeCustomerResolver`, which needs someone who has already paid once.
3. **The estimate is a flat 2000 mKCU.** LiteLLM computes a worst case from
   token counts; nothing here can, before a spec exists. The alternative is a
   rolling average of recent runs, which is more accurate and less
   predictable.
4. **The registry is in-process and bounded at 32**, like `_SESSIONS` and the
   idempotency keys. Two Cloud Run replicas are two registries; a run started
   on one cannot be polled or cancelled through the other. Making that work
   needs a shared store, which is a deployment decision.
5. **Rejoin is status-only.** Keeping the last N results would make a lost
   stream genuinely recoverable, at a memory cost per run. Worth revisiting if
   the board text is ever moved out of the response body.
6. **A reused run id is a 409, not a replay.** See §7.1.
