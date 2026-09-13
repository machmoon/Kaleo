# Deploying Silkscreen to Cloud Run

Silkscreen's service half (`service/app.py`) is a single stdlib HTTP server that
runs the pipeline and serves the built review UI from the same origin. This is
how it gets onto **Google Cloud Run**, how it is smoke-tested once it is there,
and — the part that is easy to forget — how it is **torn down** afterwards.

This deploy exists for private judging. It is not meant to run forever, and
[Teardown](#teardown) is a step of the procedure, not an afterthought.

Two scripts do the work:

| | |
|---|---|
| `scripts/deploy.sh` | preflight, then one `gcloud run deploy`. Never creates secrets, enables APIs, or edits IAM — it reports the exact command instead |
| `scripts/smoke.sh` | `GET /readyz` (Google's edge answers `/healthz` itself on run.app), then one real `POST /generate` against the live URL |

- [Before you start](#before-you-start)
- [One-time setup](#one-time-setup)
- [Deploying](#deploying)
- [Smoke-testing](#smoke-testing)
- [What the numbers mean](#what-the-numbers-mean)
- [Authentication: two different gates](#authentication-two-different-gates)
- [Cost controls](#cost-controls)
- [Rotating the key and the token](#rotating-the-key-and-the-token)
- [Teardown](#teardown)
- [Troubleshooting](#troubleshooting)

---

## Before you start

| | |
|---|---|
| `gcloud` | the Google Cloud SDK, authenticated (`gcloud auth login`) |
| A GCP project | with **billing enabled** — Cloud Build and Cloud Run both require it |
| A Gemini API key | the same `GOOGLE_API_KEY` the CLI uses. See [install.md](install.md#google_api_key-what-it-is-for) |
| Roles on the project | `roles/run.admin`, `roles/cloudbuild.builds.editor`, `roles/secretmanager.admin`, `roles/iam.serviceAccountUser` |

You do **not** need Docker locally, and you do not need to build anything by
hand. The image is built by Cloud Build from this repo's root `Dockerfile`.

Rehearse the whole thing first. `--dry-run` runs every read-only check, prints
the exact `gcloud run deploy` it would issue, and exits non-zero listing
everything that is still missing:

```bash
./scripts/deploy.sh --dry-run
```

## One-time setup

### 1. Enable the APIs

`deploy.sh` checks these and refuses to continue when one is off, printing the
command below. It does not enable them itself: turning on an API is a
billing-relevant change to someone else's project.

```bash
gcloud services enable \
    run.googleapis.com \
    cloudbuild.googleapis.com \
    artifactregistry.googleapis.com \
    secretmanager.googleapis.com \
    firestore.googleapis.com \
    --project YOUR_PROJECT
```

`firestore.googleapis.com` is for the datasheet-fact cache (`service/cache.py`),
which the service uses whenever `GOOGLE_CLOUD_PROJECT` is set and
`USE_FIRESTORE` is not `0` — and `deploy.sh` sets both. If you have no Firestore
database in the project, either [create one][firestore-create] (Native mode) or
deploy with `USE_FIRESTORE=0`; the service falls back to the in-memory store and
simply re-reads datasheets it has read before.

[firestore-create]: https://cloud.google.com/firestore/docs/create-database-server-client-library

### 2. Put both credentials in Secret Manager

**Neither of these may be passed with `--set-env-vars.`** An environment
variable on a Cloud Run revision is plainly readable by anyone with console
read access on the project, appears in `gcloud run services describe` output —
which people paste into chats and issues — and is copied into every subsequent
revision. Secret Manager keeps the value out of the service's configuration and
replaces it with a reference.

```bash
# The Gemini API key. Read from the environment so it never enters shell history.
printf %s "$GOOGLE_API_KEY" \
  | gcloud secrets create silkscreen-gemini-key \
        --data-file=- --replication-policy=automatic --project YOUR_PROJECT

# The application bearer token. Generate it; do not invent one by hand.
openssl rand -base64 32 | tr -d '\n' \
  | gcloud secrets create silkscreen-access-token \
        --data-file=- --replication-policy=automatic --project YOUR_PROJECT
```

Read the token back once, to give to clients — this is the only time you need
to look at it:

```bash
gcloud secrets versions access latest --secret=silkscreen-access-token --project YOUR_PROJECT
```

Use `--key-secret` / `--token-secret` if you name them something else.

### 3. Let the runtime service account read them

The account that reads the secrets at start-up is the **runtime** service
account of the Cloud Run service, not the human who deployed it. By default
that is the project's compute service account,
`PROJECT_NUMBER-compute@developer.gserviceaccount.com`; `deploy.sh` resolves it,
checks the binding, and warns loudly when it is absent — a revision missing this
binding fails to start with a message about the secret, not about IAM.

```bash
SA="$(gcloud projects describe YOUR_PROJECT --format='value(projectNumber)')-compute@developer.gserviceaccount.com"

for secret in silkscreen-gemini-key silkscreen-access-token; do
  gcloud secrets add-iam-policy-binding "$secret" \
      --member="serviceAccount:$SA" \
      --role="roles/secretmanager.secretAccessor" \
      --project YOUR_PROJECT
done
```

A dedicated service account is better practice than the default compute one, and
`--service-account` takes it if you have made one. It needs
`roles/secretmanager.secretAccessor` on both secrets and `roles/datastore.user`
if Firestore is on.

## Deploying

```bash
./scripts/deploy.sh --project YOUR_PROJECT --region us-central1
```

Flags: `--service`, `--region`, `--project`, `--service-account`,
`--key-secret`, `--token-secret`, `--no-token`, `--require-iam`, `--image`,
`--dry-run`. `SILKSCREEN_PROJECT`, `SILKSCREEN_REGION`, `SILKSCREEN_SERVICE`,
`SILKSCREEN_KEY_SECRET`, `SILKSCREEN_TOKEN_SECRET` and `SILKSCREEN_RUNTIME_SA`
set the same values from the environment.

**It builds from source (`gcloud run deploy --source .`), not from a local
image.** Three reasons:

1. Cloud Build uses this repo's own root `Dockerfile` — the same one `ci.yml`
   builds on every push. One image definition, not two that can drift.
2. It always produces `linux/amd64`. A `docker build` on an Apple Silicon laptop
   produces an `arm64` image that Cloud Run refuses to start, and the failure
   surfaces as an opaque start-up error rather than "wrong architecture".
3. Cloud Run can only pull from Artifact Registry or GCR. The `ghcr.io` images
   `docker.yml` publishes on release tags are not deployable here without a copy
   step, so "just reuse the release image" is not the shortcut it looks like.

The source upload obeys `.gitignore`, so `.venv/`, `node_modules/` and
`frontend/dist/` are not uploaded — the Node stage in the Dockerfile builds the
UI bundle inside the image. Note that the first `--source` deploy **writes a
`.gcloudignore` into the repository root**: gcloud derives it from `.gitignore`
and leaves it there. That is a real file in your working tree, so either commit
it deliberately or delete it after the deploy; do not let it show up as a
mystery untracked file in someone else's `git status`.

`--image REF` covers the redeploy case: same image, different flags, no rebuild.

At the end the script prints the service URL. **Record it in `README.md`.**
Nothing in the repo writes it down, and an unrecorded endpoint cannot be
verified by anyone who did not run the deploy — that gap is exactly the known
issue this tooling closes.

## Smoke-testing

```bash
export SILKSCREEN_ACCESS_TOKEN="$(gcloud secrets versions access latest \
    --secret=silkscreen-access-token --project YOUR_PROJECT)"

./scripts/smoke.sh https://silkscreen-xxxxxxxx-uc.a.run.app
```

Two checks, in order:

1. `GET /readyz` must answer `{"ok": true}`. Free, and needs no token —
   `service/app.py` answers it before the static bundle and before the bearer
   gate. `/readyz` rather than `/healthz` because Google's frontend intercepts
   `/healthz` on `run.app` domains at the edge and 404s it before the container
   sees it, so an external probe of `/healthz` always fails.
2. One real `POST /generate`, asserting the response carries `board_mm`,
   `status`, `parts`, `kicad_pcb` and `placements`, that `board_mm` is two
   positive numbers, that `parts` is non-empty, that `kicad_pcb` really begins
   `(kicad_pcb`, and that `placements` has part geometry. A deploy that answers
   `/readyz` and then 500s on the only route anyone uses is precisely what this
   catches, and a 200 whose body is missing `placements` is a broken deploy that
   the status line calls fine.

Any failure exits non-zero. `--health-only` runs just the free half.
`--dry-run` prints what it would request and calls nothing.

The second check **spends a Gemini call and a CP-SAT solve**. That is the point:
the model call and the solver are the parts that only break in the deployed
environment. The token is never printed and never passed in `argv` (`ps` shows
`argv` to every local user) — it reaches `curl` through a mode-600 temporary
config file removed by an `EXIT` trap.

## What the numbers mean

`deploy.sh` sets each of these deliberately. Change them there, and change the
reasoning here at the same time.

| Flag | Value | Why |
|---|---|---|
| `--port` | `8080` | The `Dockerfile`'s `PORT` default, and what `service/app.py` binds |
| `--memory` | `2Gi` | OR-Tools plus the model client. 512Mi OOMs on a real board, and an OOM-killed instance looks like a network error to the client |
| `--cpu` | `2` | CP-SAT runs with `workers=1` for determinism, so a solve pins one core for its whole time budget. The second core keeps the HTTP thread, the NDJSON stream flushes and the Gemini client responsive while that happens |
| `--concurrency` | `2` | `ThreadingHTTPServer` can accept more, but every extra in-flight request is another solve competing for those two cores. Two is one solving and one starting |
| `--max-instances` | `3` | The cost ceiling. Worst case 3 × 2 = 6 paid runs in flight |
| `--min-instances` | `0` | Scale to zero. A cold start costs seconds; a run costs minutes. Idle instances bill for nobody's benefit |
| `--timeout` | `900` | A grounded run with datasheet reads and repair rounds runs well past the 300s default, and `/generate/stream` holds the socket open for the whole run |

## Authentication: two different gates

**Cloud Run IAM (`--allow-unauthenticated`): off as a gate, deliberately.**
The intended client is a desktop app used by people who do not have Google
accounts. There is no identity for IAM to check and no principal to grant, so
requiring IAM auth would not restrict the endpoint to the right people — it
would restrict it to nobody. The service is therefore deployed **public**, and
that is a decision, not an oversight.

**The application bearer token: the actual gate.** When
`SILKSCREEN_ACCESS_TOKEN` is set in the environment, every route below needs
`Authorization: Bearer <token>` and answers `401` with a JSON body to anything
else. `deploy.sh` wires it from Secret Manager by default. The check is
`hmac.compare_digest`, so a wrong token cannot be found a character at a time
by measuring the reply. The scheme name is matched case-insensitively (a proxy
is allowed to normalise it); the token itself is compared exactly.

| Route | Gated? | Why |
|---|---|---|
| `GET /healthz`, `GET /readyz` | **no** | Cloud Run's start-up and liveness probes. They carry no header of ours, so gating them would fail every revision before it served a request. They are answered before the gate, and before the static bundle, so a build output named `healthz` cannot shadow them. |
| `GET /` and any file in the built bundle | **no** | A browser cannot put an `Authorization` header on a document navigation, so a gated bundle serves a 401 body where the app should be. The bundle is public build output containing no secret, and every API route it then calls *is* gated — so this exemption widens who can read the JavaScript, not who can spend the quota. The exemption is by shape, not by prefix: a GET that resolves to no bundle file is gated, so a 404 cannot be used to map the route table anonymously. |
| `POST /billing/webhook` | **no** | Stripe's, and Stripe cannot send our token. That request authenticates itself with an HMAC over its own raw body — a stronger check than a shared bearer — and gating it would silently drop every payment event. |
| everything else | **yes** | `POST /generate`, `/generate/stream`, `/chat/stream`, `/placement/repair`, `/transcribe`, `/desk/resolve`, `/steps…`, `/billing/checkout`, and the JSON reads `GET /models`, `/config/status`, `/integrations`, `/deliver/config`, `/billing/balance`, `/steps/<id>`. Each either spends money (a model call, a solve, a live `listModels`) or describes the deployment and its runs. |

The refusal closes the connection and never reads the request body: a rejected
caller does not get to stream a megabyte into the process.

**A consequence worth stating.** Opening the deployed URL in a browser loads
the SPA, and then its first API call gets a 401 — the bundle is open and the API
is not. That is intended: the supported client for a *gated* deploy is the
desktop app, which holds the token and sends it on every request
(`app/src/lib/silkscreen/client.ts`). Use `--no-token` behind `--require-iam` if
you want the served SPA to be usable by itself.

**With no token set there is no gate at all**, which is what local use wants:
`python -m service.app` on loopback, with the desktop app and the SPA both
same-origin. Nothing about running it locally changes.

Leaving it unset deploys an **open, quota-spending endpoint**: anyone who finds
the URL can spend your Gemini quota and your Cloud Run CPU, and Cloud Run
service URLs are predictable enough to be found. `deploy.sh` therefore requires
you to type `--no-token` to do it, prints a warning when you do, and never
arrives there by default. Do it only behind `--require-iam`, or for a short demo
you are watching.

`--require-iam` deploys without `--allow-unauthenticated` for an internal-only
service: callers then need a Google identity and
`roles/run.invoker`, and reach it with
`curl -H "Authorization: Bearer $(gcloud auth print-identity-token)"`.

## Cost controls

The ceiling is `--max-instances 3` — that flag is the whole difference between a
bounded bill and an unbounded one on a public endpoint. On top of it:

- **Set a budget alert** on the project before deploying (Billing → Budgets &
  alerts). Alerts do not stop spending; they tell you it started.
- **Watch the request count** for the first day:
  ```bash
  gcloud run services describe silkscreen --region us-central1 --format='value(status.traffic)'
  gcloud run services logs read silkscreen --region us-central1 --limit 50
  ```
- **Gemini quota is the other meter.** Cloud Run bills CPU-seconds; the model
  bills tokens, on a different account, with its own limits. Cap it in AI Studio
  as well as here.
- **Scale to zero is already on** (`--min-instances 0`): an unused service costs
  nothing but the stored image.
- **`--timeout 900` bounds a single runaway request**, and concurrency 2 bounds
  how many can pile onto one instance.

If something does go wrong, the fastest brake is to close the endpoint rather
than to debug under load. Revoking the public invoker binding takes effect
immediately and leaves the service, its revisions and its logs intact:

```bash
gcloud run services remove-iam-policy-binding silkscreen \
    --region us-central1 --member=allUsers --role=roles/run.invoker
```

Re-open it later with `add-iam-policy-binding` and the same two values, or by
re-running `deploy.sh`. (Do not reach for `--max-instances 0` — Cloud Run reads
`0` as "no explicit maximum", which is the opposite of a brake.)

## Rotating the key and the token

Both rotate the same way: add a new secret **version**, then redeploy so the
running revision picks it up. `deploy.sh` pins `:latest`, but a revision resolves
that reference at start-up — an existing revision keeps serving the old value
until it is replaced.

```bash
# 1. New version.
printf %s "$NEW_GEMINI_KEY" \
  | gcloud secrets versions add silkscreen-gemini-key --data-file=- --project YOUR_PROJECT

# 2. Redeploy so a new revision reads it. No rebuild needed if the code is unchanged:
./scripts/deploy.sh --project YOUR_PROJECT   # or: --image <the current image ref>

# 3. Verify, then retire the old version.
./scripts/smoke.sh "$URL"
gcloud secrets versions disable 1 --secret=silkscreen-gemini-key --project YOUR_PROJECT
```

Disable first and destroy later: a disabled version can be re-enabled if the
rotation turns out to have broken something, and a destroyed one cannot.

Rotating the access token is identical with `silkscreen-access-token`, plus
handing the new value to every client. Rotate it whenever it has been pasted
somewhere it should not have been, whenever someone with it leaves the project,
and once after judging ends.

**Revoke the Gemini key itself** (in AI Studio) rather than only rotating the
secret if you believe the key leaked — the secret is a copy, and deleting the
copy does not disable the key.

## Teardown

Do this when judging is over. An idle service still holds a public URL and a
live API key, and a forgotten one is how a demo turns into an incident.

```bash
PROJECT=YOUR_PROJECT
REGION=us-central1

# 1. The service, and every revision under it.
gcloud run services delete silkscreen --region "$REGION" --project "$PROJECT"

# 2. The secrets. This destroys every version -- irreversible on purpose.
gcloud secrets delete silkscreen-gemini-key   --project "$PROJECT"
gcloud secrets delete silkscreen-access-token --project "$PROJECT"

# 3. The images Cloud Build left behind. They bill for storage forever otherwise.
gcloud artifacts repositories delete cloud-run-source-deploy \
    --location "$REGION" --project "$PROJECT"
```

Then **revoke the Gemini API key in AI Studio** — deleting the secret deletes
the copy, not the key — and **remove the URL from `README.md`**, so nobody
smoke-tests an endpoint that no longer exists. If the project was created only
for this, `gcloud projects delete YOUR_PROJECT` does all of the above at once.

## Troubleshooting

**`the credentials for ... are stale`** — `gcloud auth list` reads a local file
and will happily name an account whose refresh token expired weeks ago.
`deploy.sh` mints an access token to check for real. Run `gcloud auth login`.

**`SERVICE_DISABLED` during the deploy** — an API is off and the preflight could
not see it (listing APIs needs Service Usage permission). Run the
`gcloud services enable` command from [One-time setup](#one-time-setup).

**The revision fails to start, mentioning a secret** — the runtime service
account is missing `roles/secretmanager.secretAccessor`. `deploy.sh` warns about
this before deploying; the binding command is in step 3 above.

**`/readyz` is fine but `/generate` returns 502 with a `GOOGLE_API_KEY`
message** — the container is up but has no usable key: the secret exists and is
readable, but its value is empty or wrong. Check with
`gcloud secrets versions access latest --secret=silkscreen-gemini-key`.

**`/generate` returns 401 or 403** — the service was deployed with a token and
the caller did not send it, or sent the wrong one. Export
`SILKSCREEN_ACCESS_TOKEN` and re-run. A 403 from Cloud Run *itself* (an HTML
body, not JSON) means the service is IAM-gated instead — redeploy without
`--require-iam`, or send an identity token.

**The request times out at exactly 900s** — the run genuinely took longer than
the timeout. Raise `TIMEOUT` in `deploy.sh` (Cloud Run's ceiling is 3600) or
narrow the prompt; do not retry blindly, since each attempt spends a model call.

**Logs.** Everything the service prints goes to Cloud Logging:

```bash
gcloud run services logs read silkscreen --region us-central1 --limit 100
```

A 500 response carries an `error_id`; searching the logs for it finds the
traceback that produced it.
