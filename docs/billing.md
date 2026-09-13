# Billing runbook

Usage credits over Stripe. The package is `billing/`; the HTTP surface is
`service/billing_routes.py`. Nothing here is enabled by default — with no
Stripe environment set, `billing_enabled()` is false, the service starts
normally, and `/billing/checkout` answers **503** rather than 500.

## The model

One **KCU** is one minute of attributable engine wall-clock, stored as integer
milli-KCU (mKCU). Users buy packs; runs draw down.

| | |
| --- | --- |
| Unit | 1 KCU = 1 min engine wall-clock = 1000 mKCU |
| Rate | `KALEO_RATE_CENTS_PER_KCU`, default 225¢ |
| Pack | `STRIPE_PRICE_ID` grants `STRIPE_CREDIT_MKCU`, default 8888 mKCU |
| Past zero | **Overage**, not cutoff — up to `OveragePolicy.limit_mkcu` |
| Settlement | Two-phase: `begin_settlement()` freezes a quantity, the charge is built for it, `settle_overage(intent, …)` grants exactly that |
| Safety | `TopUpPolicy.monthly_cap_cents` bounds auto-reload |

Money is integer cents and compute is integer mKCU, for the reason
`engine/silkscreen/units.py` uses integer nanometres: floats drift, and a
balance that drifts cannot be reconciled against Stripe.

## Connecting your Stripe account (the "click allow" popup)

Stripe **does** publish a browser consent flow for your own account, and an
earlier version of this runbook said it did not. Verified live on 2026-09-06:

```
GET https://mcp.stripe.com/.well-known/oauth-protected-resource
  -> authorization_servers: ["https://access.stripe.com/mcp"]
GET https://access.stripe.com/.well-known/oauth-authorization-server/mcp
  -> registration_endpoint, code_challenge_methods_supported: ["S256"],
     token_endpoint_auth_methods_supported: ["none"]
```

Dynamic client registration, PKCE S256, **no client secret** — structurally
the same flow `googleapps/auth.py` already runs for Google. `billing/oauth.py`
implements it and the Settings → Billing pane drives it:

| | |
| --- | --- |
| `POST /billing/connect` | opens Stripe's consent page, answers `202` immediately |
| `GET /billing/connect` | poll for `idle` / `running` / `connected` / `failed` |
| `POST /billing/disconnect` | revokes at Stripe, then deletes the local token |

The flow blocks for up to five minutes waiting for a human, so it runs on its
own thread and the UI polls; a second concurrent `POST` gets a `409` rather
than racing for the loopback port. The token lands at `~/.kaleo/stripe_oauth.json`
mode `0600`. The user can also revoke from the Stripe Dashboard under
*user settings → OAuth sessions*.

**What it does not do.** The consent grants the `mcp` scope for
`https://mcp.stripe.com`. It is *not* an API key. Every REST path in this
package — Checkout, webhook fulfilment, overage charges — still runs on the
restricted key below. The pane says this on screen rather than implying
otherwise, and `test_describe_does_not_claim_consent_replaces_the_api_key`
pins it.

Two details that are load-bearing rather than cosmetic:

* **RFC 8414 path insertion.** For issuer `https://access.stripe.com/mcp` the
  metadata lives at `…/.well-known/oauth-authorization-server/mcp` — the
  well-known segment goes *before* the path. Stripe 404s the naive suffix
  form, which is still tried as a fallback.
* **Every discovered URL is re-checked against the host allowlist.** Discovery
  documents arrive over the network and then receive the PKCE verifier and
  hand back the access token. `transport.OAUTH_HOSTS` is kept separate from
  `ALLOWED_HOSTS`, and a request has to opt in per call, so the guarantee
  "an API key can never travel anywhere but `api.stripe.com`" survives this
  feature.

Connect OAuth is the wrong tool here: its documented response now carries only
`stripe_user_id`, with `access_token`/`refresh_token` deprecated in favour of
the `Stripe-Account` header plus a *platform* secret key — a credential a
desktop app cannot hold. Stripe Apps OAuth needs marketplace publication.

## Setting it up

1. **Create a restricted key**, not a secret key. Dashboard → Developers →
   API keys → *Create restricted key*. It needs exactly two permissions:
   Checkout Sessions **write**, PaymentIntents **write**. Nothing else.
   `BillingConfig` accepts `sk_` but `describe()` reports `key_kind` so you can
   see at a glance which one is deployed.

2. **Create the product and price.** One-time payment, not recurring.
   `STRIPE_PRICE_ID` is a `price_…` id — a `prod_…` id is rejected at startup.

3. **Create the webhook endpoint** pointing at `POST /billing/webhook`, and
   subscribe to all six of these — the three that grant compute:

   - `checkout.session.completed`
   - `checkout.session.async_payment_succeeded`
   - `checkout.session.async_payment_failed`

   and the three that take it back (`fulfillment.py:34-36` `REVERSAL_EVENTS`,
   dispatched at `:67-68` into `_reverse`):

   - `charge.refunded`
   - `charge.dispute.created`
   - `charge.dispute.closed`

   **Subscribing to only the first three is a money bug, not an omission.** Money
   goes back out through Stripe whether or not you asked to hear about it; if these
   events never arrive, the compute you granted for that payment is never clawed
   back, and a refund becomes free compute. This list used to say "exactly these"
   and name only the checkout events, which is how an operator following the
   runbook ended up with a one-way ledger.

   Subscribing to only the first is the classic bug: with a delayed-notification
   payment method it arrives while the session is still `unpaid`, so fulfilling
   on it alone grants compute for payments that later fail *and* never fulfils
   the ones that succeed.

4. **Copy that endpoint's own signing secret** into `STRIPE_WEBHOOK_SECRET`.
   It is per-endpoint; the one from a different endpoint fails every check.

5. **Install the pre-commit hook** so a key cannot be committed:

   ```sh
   ln -sf ../../scripts/check_no_keys.sh .git/hooks/pre-commit
   ```

Store the key in your platform's secrets vault — Secret Manager on GCP, which
is where this service already runs. Environment variables are the fallback,
not the goal.

## Testing locally

```sh
stripe listen --forward-to localhost:8081/billing/webhook   # prints a whsec_
stripe trigger checkout.session.completed
```

`stripe listen` prints a signing secret for the forwarded session — use that
one locally, not the Dashboard endpoint's.

The offline suite needs none of this:

```sh
python -m pytest billing/tests service/tests/test_billing_routes.py -q
```

## Status codes, and why

Stripe retries any non-2xx for days, so the code the route returns *is* the
behaviour.

| Situation | Code |
| --- | --- |
| Genuine paid event | 200, granted |
| Replayed event | **200** — a non-2xx retries the same event for days |
| Completed but `unpaid` | 200, the async success event follows |
| Unhandled event type | 200 |
| Forged or stale signature | 400, nothing parsed, no reason given |
| Signed but unapplicable | 400 — retrying cannot fix a malformed grant |
| Our own failure | **500** — so Stripe retries; the payment is real |

## Why settlement is two-phase

`begin_settlement()` freezes the quantity being billed; the charge is built
for exactly that; `settle_overage(intent, …)` grants exactly that.

The single-phase version re-read the balance when the money came back, and an
off-session charge is a network round trip that can stretch through an
authentication step. Anything landing in that window changed the amount being
settled, in both directions:

* a run finished mid-flight → its compute was written off as settled and
  became permanently unbillable, with no entry left that remembered it;
* a top-up landed mid-flight → nothing was owed any more, `settle_overage`
  raised, the `charge_id` was never marked applied, and a **successful charge
  ended up with no ledger entry at all** — money taken, nothing given, and
  every retry raising the same error forever.

Freezing the quantity first removes the window. Usage that accrues during the
charge stays owed for the next settlement; a purchase that lands becomes
ordinary credit. A successful charge is always recorded.

## Where the ledger lives

`billing/sqlite_ledger.py`. `~/.kaleo/ledger.sqlite3` by default, or
`KALEO_LEDGER_PATH`. It is a **subclass** of `MemoryLedger` that overrides the
storage seam and nothing else — every rule (append-only, reserve/commit/
release, two-phase settlement, the monthly window) stays in one place, because
a durable ledger that reimplements those rules will drift from the tested one
and then the two disagree about money. `test_the_invariants_hold_on_disk_too`
re-runs the 250-op fuzz against a file to keep that honest.

Why it had to be durable, in one sentence: **Stripe retries a webhook for
days**, so a restart between the first delivery and the retry meant the replay
guard had forgotten the event and one Checkout Session granted the pack twice.

Four schema decisions, each preventing a specific failure:

- `applied` has a UNIQUE key, so the replay guard is a **constraint**, not just
  a dict check. A dict check is only correct while one process owns the ledger,
  and the desktop app plus `stripe listen` is already two.
- `synchronous=FULL` with WAL. `NORMAL` can lose the last transactions on power
  loss — fine for a cache, not for "did we already grant this payment".
- Money and compute are `INTEGER`. `REAL` would reintroduce the float drift the
  package exists to avoid.
- No UPDATE or DELETE against `entries` anywhere in the file. The balance is a
  fold and assumes it.

If the file cannot be opened (read-only home, full disk) the service falls back
to `MemoryLedger` and says so on stderr, because "your balance resets on
restart" is the operator's to know. This still does **not** make the service
safe as two replicas — SQLite on a shared filesystem is not a distributed
database, and multi-instance remains a real deployment gate.

## Settling overage

`billing/settle.py`. Everything for this existed and nothing was joined,
because the two ids an off-session charge needs — a `cus_` and a `pm_` — had
nowhere to live. `billing/paymethods.py` is that store (on the ledger's own
connection and lock, so recording a card and granting credit from one webhook
cannot half-commit), and `settle_account` is the runner.

The ordering *is* the module:

1. **Freeze the quantity first** with `begin_settlement`. Re-reading the
   balance after the charge means a run finishing during the round trip has its
   compute written off unbillable, or a top-up landing during it leaves a
   *successful* charge with no ledger entry.
2. **Idempotency keys on `settle_id`, not the clock.** A retried settlement
   without a stable key bills the same overage twice, and nobody is present to
   notice.
3. **A decline abandons the intent, it does not consume it.** The compute was
   delivered; the debt stays owed and a working card settles it later.
4. **`authentication_required` is not a retry.** The bank wants the customer
   present, so retrying off-session fails identically forever. The outcome
   distinguishes `needs_customer` from `declined` for exactly that reason.
5. **A 2xx with no charge id keeps the debt.** Money may have moved and we
   cannot name the movement; leaving it owed means the retry reuses the
   idempotency key, so Stripe returns the original charge instead of a second.

What this stores is a **token, not a card**. `pm_...` is a Stripe handle,
useless without the API key, and the SetupIntent flow means the app never sees
a number. `SavedMethod.__repr__` truncates both ids anyway, since the object
lands in the frame of every settlement error.

One bug worth recording: `handle_event` ignored setup-mode sessions entirely.
Ignoring the *credit* was right — they grant none. Ignoring the *payment
method* threw away the card the customer had just saved, which is the only
event that carries it. Recording now happens before the mode gate, for both
modes, and never raises into the webhook path.

## Known gaps

- **`MemoryLedger` is process-local.** It is the offline stand-in. Durable
  storage is the prerequisite for more than one Cloud Run replica — two
  replicas today have two different balances.
- **Overage settlement is not wired end to end.** `create_setup_session` and
  `charge_overage` exist and are correct, but nothing persists the `customer`
  and `payment_method` ids an off-session charge needs. Blocked on deciding
  what identifies an account (`billing/accounts.py` is the seam).
- **Never run against real Stripe.** Every test uses a recorded transport.
  (The reversal-event subscription this note used to add here is now part of
  step 3 of the setup, where an operator will actually read it.)
