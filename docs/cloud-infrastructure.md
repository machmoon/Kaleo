# Google Cloud infrastructure

## What the requirement asks for

`CLAUDE.md` records three Google-stack constraints on the submission, of which
the third is the subject of this document: the project must use "at least one
Google Cloud infrastructure service." The same section notes that the constraint
applies to the agentic layer rather than to the deterministic engine, because
`engine/` is deliberately key-free and offline-testable and therefore has no
network calls in it at all.

Silkscreen uses two Google Cloud services directly, and two more indirectly
through the documented deploy path.

## The services in use

### Cloud Run, for the HTTP surface

`service/app.py` is a single HTTP service that wraps the generation pipeline.
Its module docstring names Cloud Run explicitly and gives the deploy command,
and the file is written against the standard library alone so that the container
image stays small. The handler started with two routes and now exposes more than a
dozen — read `do_GET`/`do_POST` in `service/app.py` for the current list rather than
trusting a count here. The two that matter for the Cloud Run container contract are
unchanged: a `GET` on `/`, `/healthz` or `/readyz` returns a small JSON readiness body
(probe a *deployed* instance with `/readyz` or `/`, never `/healthz` — Google's edge
intercepts that path on `run.app` domains and answers 404 before the container sees it),
and a `POST` on `/generate` or `/` runs the pipeline and returns the placed board along
with the emitted `.kicad_pcb` text. The rest — `/generate/stream`, `/chat/stream`,
`/models`, `/config/status`, `/integrations`, `/transcribe`, `/desk/resolve`, the
`/steps/…` overlay routes, the `/deliver/…` Workspace routes — are additions on the same
stdlib handler and change nothing about the container contract.

Two details of that file exist specifically to satisfy the Cloud Run container
contract. The server binds to `0.0.0.0` and reads its port from the `PORT`
environment variable, defaulting to 8080, in `make_server`. Google's container
contract requires exactly this: the ingress container must listen on `0.0.0.0`
rather than `127.0.0.1`, requests go to port 8080 by default, and Cloud Run
injects `PORT` to tell the container which port to use.

`Dockerfile` at the repository root builds the image. It starts from
`python:3.11-slim`, installs the project with the `agents` and `cloud` extras,
copies `service/` in as a later layer so that a source edit does not invalidate
the dependency layer, sets `PORT=8080`, and runs `python -m service.app`.
`.dockerignore` keeps `.env`, the archived frontend, the design canvas, and
the caches out of the build context.

### Firestore, for the cross-instance fact cache

`service/cache.py` holds a Firestore-backed cache of facts extracted from
component datasheets. Its docstring states the reasoning plainly: reading a
300-page datasheet is the slowest and most expensive step in the pipeline, and
the result is a pure function of the part number, so the second run on a part
should be free across processes, across instances, and across users. That
requirement is what rules out an in-process dictionary. Google's container
contract confirms the constraint the code is designed around, since a Cloud Run
revision receiving no traffic scales in to its minimum instance count, zero by
default, idle instances can be shut down at any time after fifteen minutes
without traffic, and data written to the container file system does not persist
when the instance stops. A dictionary held in the handler process would
therefore lose every entry on the next teardown, and would in any case not be
shared with a second instance serving concurrent traffic.

The cache is keyed by part number, normalised by the `cache_key` function. That
function strips surrounding whitespace, lowercases the part number, and replaces
forward slashes with underscores. Both transformations are justified in the
docstring. Case is folded because a datasheet lookup for `stm32f103` and one for
`STM32F103` are the same lookup, and the slash is replaced because real part
numbers contain slashes, as in `LM317/NOPB`, while Firestore document
identifiers should not. Google's Firestore best-practices page confirms the
second point, advising you to avoid using forward slashes in document IDs and to
avoid the IDs `.` and `..`. An empty key raises a `ValueError` rather than
writing a document under an empty name.

`FirestoreFactStore` writes each entry into a `datasheet_facts` collection as a
document containing the facts, a `cached_at` timestamp, and the original
unnormalised part string. It reads through `collection().document().get()` and
checks the snapshot's `exists` property before calling `to_dict()`, which matches
the Python client library, where `DocumentSnapshot.exists` indicates whether the
document existed at the time the snapshot was retrieved and `to_dict()` returns
`None` for a reference that does not exist.

The client is constructed as `firestore.Client(project=project)` with `project`
defaulting to `None`. The Firestore server client-library documentation notes
that the `project` argument is optional and that the client falls back to the
default project inferred from the environment when it is not supplied, so on
Cloud Run this resolves to the deployment's own project without any explicit
configuration.

### Cloud Build and Artifact Registry, used indirectly

The deploy command documented in `README.md` and in the `service/app.py`
docstring is `gcloud run deploy silkscreen --source . --region us-central1`.
Google's source-deployment documentation states that a `--source` deploy builds
the uploaded source with Cloud Build and stores the resulting container in
Artifact Registry, creating a repository named `cloud-run-source-deploy` in the
target region if the project does not already have one. It also states that a
Dockerfile present in the source directory is used for the build, and that
buildpacks are only used to detect the language when no Dockerfile is present.
Since this repository has a Dockerfile at its root, a source deploy will build
from it rather than guess. The APIs that must be enabled for this path are the
Cloud Run Admin API and the Cloud Build API.

### Vertex AI is not used

It is worth stating what the project does not use, because Vertex AI would also
have counted toward this requirement. `engine/silkscreen/agents/model.py`
constructs its client as `genai.Client(api_key=key)` and raises a `ModelError`
if `GOOGLE_API_KEY` is unset, which is the Gemini Developer API path rather than
the Vertex AI path. No code in the repository sets or reads
`GOOGLE_GENAI_USE_VERTEXAI`. The requirement is met by Cloud Run and Firestore,
not by the model call.

## How a request moves through the system

A client posts a JSON object containing an `intent` string and an optional
`datasheets` object mapping each part number to a PDF URL. The handler in
`service/app.py` enforces a one-megabyte body limit, parses the body, and calls
`generate`.

`generate` first asks the fact store for each part number in the request. Every
part that already has an entry is dropped from the set of datasheets to read, so
the remaining set passed to `generate_pcb` contains only parts the system has
never seen. The pipeline in `engine/silkscreen/agents/pipeline.py` then reads
those remaining datasheets, proposes a circuit into the validated intermediate
representation, repairs it against validation errors, places it with the CP-SAT
solver, and optionally runs the adversarial review pass. Afterwards, `generate`
writes an entry back to the store for every part the pipeline reported facts
about. The response body includes a `cache` object listing which parts were hits
and which were read, which is what makes the caching behaviour observable from
outside the service.

The store itself is selected at request time by `build_store`, which returns a
`FirestoreFactStore` when `GOOGLE_CLOUD_PROJECT` is set and the `USE_FIRESTORE`
environment variable is not `0`, and a `MemoryFactStore` otherwise. Errors are
classified before they reach the client. A bad request body produces a 400, an
upstream model outage produces a 502 rather than a 500, and anything else
produces a 500 with a truncated traceback. The `caused_by_model_failure` helper
walks the exception cause chain to make that distinction, because the pipeline
wraps a failed model call in a `ProposalError` that is a plain `RuntimeError`.

## Deploying and configuring it

The documented deploy is `scripts/deploy.sh` (procedure in `docs/deploy.md`), which
checks the prerequisites, then runs the command `README.md` gives in short form:

```bash
gcloud run deploy silkscreen --source . --region us-central1 \
  --set-secrets GOOGLE_API_KEY=google-api-key:latest \
  --set-env-vars GOOGLE_CLOUD_PROJECT=your-project
```

Three pieces of configuration matter. `GOOGLE_API_KEY` is what
`engine/silkscreen/agents/model.py` requires in order to construct a Gemini
client. `GOOGLE_CLOUD_PROJECT` is what `build_store` checks in order to decide
whether to use Firestore at all, so leaving it unset silently downgrades the
deployed service to an in-memory cache. Credentials for the Firestore client are
not configured at all, and that is deliberate. Application Default Credentials,
the strategy the Google client libraries use to find credentials from the
environment rather than from code, searches for the
`GOOGLE_APPLICATION_CREDENTIALS` environment variable first, then a local
`gcloud auth application-default login` credential file, and finally the
attached service account via the metadata server. Google describes that last
route as the preferred method in a production environment on Google Cloud. On
Cloud Run the client library requests an access token from the instance metadata
server for the service account configured as the service identity, so no key
file is needed. Google's service-identity documentation warns explicitly that
you should never set `GOOGLE_APPLICATION_CREDENTIALS` as an environment variable
on a Cloud Run service.

By default a Cloud Run service runs as the Compute Engine default service
account, which Google recommends replacing with a dedicated user-managed service
account so that its permissions can be reduced to the minimum the service needs.
For this service that minimum is read and write access to Firestore documents,
which is granted by the predefined `roles/datastore.user` role.

Running the service locally requires none of this. `python -m service.app`
starts the same handler on port 8080, and with `GOOGLE_CLOUD_PROJECT` unset it
uses the in-memory store.

## Staying testable without a Google Cloud project

Both cloud dependencies sit behind seams, and the tests exercise the code on the
near side of each seam.

Firestore is hidden behind the `FactStore` protocol in `service/cache.py`, which
declares only `get` and `put`. `MemoryFactStore` implements that protocol over a
plain dictionary and additionally counts hits and misses. The docstring states
the motive directly, which is that the protocol makes every caller testable with
no network and no Google Cloud project, for the same reason the model sits
behind its own protocol in `engine/silkscreen/agents/model.py`.

`FirestoreFactStore` also accepts an injected `client`, so the Firestore code
path is itself covered rather than merely bypassed. `service/tests/test_cache.py`
supplies a fake client, collection, document reference, and snapshot, then
asserts that a value round-trips, that the collection name is `datasheet_facts`,
and that the stored document carries the original part string and a non-zero
`cached_at`. The remaining tests in that file cover key normalisation, the
rejection of an empty part number, and the fact that stored dictionaries are
copied rather than aliased.

`service/tests/test_app.py` starts the real HTTP server on an ephemeral port in a
background thread and drives it over a real socket with `urllib`, substituting a
`ScriptedModel` for the Gemini client and a `MemoryFactStore` for Firestore
through the `Handler.model_factory` and `Handler.store` class attributes. Its
tests cover the health check, an unknown route, a successful generation, the
three input-validation failures, the assertion that a cache hit skips the
datasheet read, and the assertion that an upstream model outage is reported as
502 rather than 500. `.github/workflows/ci.yml` runs the whole suite, including
these tests, on Ubuntu, macOS, and Windows with the `dev`, `agents`, and `cloud`
extras installed and no credentials of any kind.

## Gaps and honest caveats

The requirement is met, but several things around it are incomplete or
undocumented, and it is better to record them here than to leave them to be
discovered.

The cache used to store a stub rather than the extracted facts; that was
resolved in `4249c5b`. `service/cache.py` now persists the real `PartFacts`
and a hit feeds them into `generate_pcb` as `preloaded_facts`, so a cached part
skips the datasheet read without losing what the read would have produced.
Service tests cover the write-back and the feed-in offline.

A new Firestore client is constructed on every request in production. `do_POST`
falls back to `build_store()` whenever `Handler.store` is `None`, and nothing on
the deployed path ever sets that attribute; only the test fixture does. The same
is true of `build_model`. This works, but it means the client setup cost is paid
per request rather than once per instance.

The environment variables were inconsistent between code and documentation;
both halves were fixed on 2026-09-06. `USE_FIRESTORE` is read by `build_store`
and is now documented in `.env.example`, so the supported way to disable
Firestore is discoverable. `GOOGLE_CLOUD_LOCATION` was listed in `.env.example`
with a default of `us-central1` while no code in the repository read it, and
has been removed; the region is supplied to
`gcloud` on the command line instead.

The Gemini key reaches the container through Secret Manager. `scripts/deploy.sh`
passes it with `--set-secrets` (alongside the application bearer token) and
refuses to run until the service identity holds
`roles/secretmanager.secretAccessor` on both secrets, printing the exact
`gcloud` command when it does not; the README's short-form command uses
`--set-secrets` too. The script also checks that the five required APIs are
enabled and decides the public-versus-IAM question explicitly (`--require-iam`)
rather than letting `gcloud` prompt, so an unattended deploy does not stall.
`docs/deploy.md` records the rest of the procedure: the Firestore database in
Native mode, the runtime service account and `roles/datastore.user`, and the
sizing every flag in the script carries.

CI still does not deploy. `.github/workflows/ci.yml` installs dependencies, runs
`ruff`, runs `pytest`, builds the web bundle and the Docker image; it has no
deploy job and no Google Cloud credentials, and that is deliberate — deploying
is an operator's decision made by running the script. One deploy is recorded:
2026-08-31, `us-central1`, the URL in `README.md`, with `/`, `/models` and one
real `POST /generate` verified live that day (`TODO.txt` feature 7). A recorded
deploy is not a running one: on 2026-09-05 that URL answered 503 on every route,
so the URL must be re-verified — `curl -s -o /dev/null -w '%{http_code}'
<url>/readyz` prints `200` — before it is shown to anyone, and redeployed with
`scripts/deploy.sh` if it does not. Every claim above about what the code does
comes from reading the code, and every claim about what Cloud Run and Firestore
do comes from the Google documentation cited below.

The container does not handle `SIGTERM`. Cloud Run sends `SIGTERM` and then
waits ten seconds before sending `SIGKILL`, and the `Dockerfile` comment
acknowledges this, but `service/app.py` installs no signal handler and calls
`serve_forever` directly, so an in-flight request is not drained on shutdown.

## Sources

The Google Cloud behaviour described above was checked against these pages:

- Cloud Run source deployment, including Dockerfile detection, Cloud Build, and
  Artifact Registry: <https://docs.cloud.google.com/run/docs/deploying-source-code>
- Cloud Run container contract, covering `PORT`, binding to `0.0.0.0`,
  `SIGTERM`, scale-to-zero, and the ephemeral in-memory file system:
  <https://docs.cloud.google.com/run/docs/container-contract>
- Cloud Run deployment flags, including `--allow-unauthenticated`,
  `--set-env-vars`, and the prompt shown when neither authentication flag is
  given: <https://docs.cloud.google.com/run/docs/deploying>
- Cloud Run service identity, covering the default Compute Engine service
  account, the metadata server, and the warning against
  `GOOGLE_APPLICATION_CREDENTIALS`:
  <https://docs.cloud.google.com/run/docs/securing/service-identity>
- Cloud Run secrets and the Secret Manager recommendation:
  <https://docs.cloud.google.com/run/docs/configuring/services/secrets>
- Application Default Credentials search order:
  <https://docs.cloud.google.com/docs/authentication/application-default-credentials>
- Firestore database creation and the Python server client library, including
  the optional `project` argument:
  <https://docs.cloud.google.com/firestore/docs/create-database-server-client-library>
- Firestore Python quickstart for `set()` and document reads:
  <https://docs.cloud.google.com/firestore/docs/quickstart-servers>
- Firestore best practices, including the advice to avoid forward slashes in
  document IDs: <https://docs.cloud.google.com/firestore/docs/best-practices>
- `DocumentSnapshot.exists` and `to_dict()` in the Python client reference:
  <https://docs.cloud.google.com/python/docs/reference/firestore/latest/google.cloud.firestore_v1.base_document.DocumentSnapshot>
- Firestore IAM and the `roles/datastore.user` predefined role:
  <https://docs.cloud.google.com/firestore/docs/security/iam>
