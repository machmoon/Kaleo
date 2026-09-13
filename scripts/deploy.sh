#!/usr/bin/env bash
# Deploy the Silkscreen service to Google Cloud Run.
#
#     ./scripts/deploy.sh --dry-run            # print the plan, change nothing
#     ./scripts/deploy.sh                      # build from source and deploy
#     ./scripts/deploy.sh --region europe-west1
#     ./scripts/deploy.sh --image REF          # redeploy an already-built image
#     ./scripts/deploy.sh --help
#
# This script never creates secrets, never enables APIs and never edits IAM. It
# checks that those prerequisites are in place and prints the exact command to
# run when one is missing -- deploying is the operator's decision, and so is
# every side effect that leads up to it. Full procedure: docs/deploy.md.
set -euo pipefail

# Quoted throughout: the repo is routinely checked out under a path with spaces
# ("Desktop/Coding/..."), and an unquoted $ROOT deploys the wrong directory.
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

# ------------------------------------------------------------- defaults ------
SERVICE="${SILKSCREEN_SERVICE:-silkscreen}"
REGION="${SILKSCREEN_REGION:-us-central1}"
PROJECT="${SILKSCREEN_PROJECT:-${GOOGLE_CLOUD_PROJECT:-}}"
KEY_SECRET="${SILKSCREEN_KEY_SECRET:-silkscreen-gemini-key}"
TOKEN_SECRET="${SILKSCREEN_TOKEN_SECRET:-silkscreen-access-token}"
SERVICE_ACCOUNT="${SILKSCREEN_RUNTIME_SA:-}"
IMAGE=""

# Sizing. Every number here is deliberate; see docs/deploy.md for the reasoning
# and change them there too if you change them here.
#
#   port 8080     the Dockerfile's PORT default, and what service/app.py binds
#   memory 2Gi    OR-Tools' CP-SAT plus the model client; 512Mi OOMs on real boards
#   cpu 2         CP-SAT runs workers=1 (determinism), so a solve pins one core
#                 for its whole budget; the second keeps the HTTP thread, the
#                 NDJSON flushes and the Gemini client responsive meanwhile
#   concurrency 2 ThreadingHTTPServer can take more, but each extra request is
#                 another solve competing for those 2 cores
#   max-instances 3   the cost ceiling: 3 x 2 = 6 paid runs in flight, worst case.
#                 An unbounded max-instances on a public endpoint is how a
#                 hackathon project produces a four-figure bill overnight
#   min-instances 0   scale to zero. A cold start costs seconds; a run costs
#                 minutes, and idle instances cost money for no one's benefit
#   timeout 900s  a grounded run with datasheet reads and repair rounds runs
#                 well past the 300s default, and the stream holds the socket open
PORT=8080
MEMORY="2Gi"
CPU="2"
CONCURRENCY="2"
MAX_INSTANCES="3"
MIN_INSTANCES="0"
TIMEOUT="900"

# The intended client is a desktop app used by people who do not have Google
# accounts, so Cloud Run IAM auth is not usable as the gate: there is no
# identity to grant. The service is therefore deployed public, and the real
# gate is the application-level bearer token (SILKSCREEN_ACCESS_TOKEN).
# --require-iam flips this for an internal-only deploy.
PUBLIC=1
# Deploying without a token is deploying an open, quota-spending endpoint. It
# has to be typed, not defaulted into.
WANT_TOKEN=1
DRY_RUN=0

REQUIRED_APIS=(
    run.googleapis.com
    cloudbuild.googleapis.com
    artifactregistry.googleapis.com
    secretmanager.googleapis.com
    firestore.googleapis.com
)

say()  { printf '%s\n' "$*"; }
step() { printf '\n==> %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }

die() {
    printf '\nerror: %s\n' "$1" >&2
    if [ $# -gt 1 ]; then
        printf 'fix:   %s\n' "$2" >&2
    fi
    exit 1
}

# A missing prerequisite is fatal for a real deploy, but during a rehearsal it
# should not hide the rest of the plan: the operator ran --dry-run precisely to
# find out everything that is missing, not the first thing.
missing() {
    if [ "$DRY_RUN" -eq 1 ]; then
        warn "$1"
        [ $# -gt 1 ] && warn "fix: $2"
        PREREQS_MISSING=1
        return 0
    fi
    die "$@"
}
PREREQS_MISSING=0

run() {
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '  would run:'
        printf ' %q' "$@"
        printf '\n'
        return 0
    fi
    "$@"
}

usage() {
    sed -n '2,13p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    cat <<'OPTS'

Options:
  --project ID         GCP project (default: $SILKSCREEN_PROJECT, then
                       $GOOGLE_CLOUD_PROJECT, then `gcloud config`)
  --region REGION      Cloud Run region (default: $SILKSCREEN_REGION or us-central1)
  --service NAME       Cloud Run service name (default: silkscreen)
  --image REF          deploy this prebuilt image instead of building from source
  --service-account SA runtime service account (default: the project's compute SA)
  --key-secret NAME    Secret Manager secret holding GOOGLE_API_KEY
  --token-secret NAME  Secret Manager secret holding SILKSCREEN_ACCESS_TOKEN
  --no-token           deploy with NO bearer token: an open, quota-spending
                       endpoint. Must be typed; it is never the default
  --require-iam        deploy without --allow-unauthenticated (IAM-gated)
  --dry-run            print the plan and the exact gcloud command; change nothing
  -h, --help           this text
OPTS
}

while [ $# -gt 0 ]; do
    case "$1" in
        --project)         PROJECT="${2:?--project needs a value}"; shift 2 ;;
        --region)          REGION="${2:?--region needs a value}"; shift 2 ;;
        --service)         SERVICE="${2:?--service needs a value}"; shift 2 ;;
        --image)           IMAGE="${2:?--image needs a value}"; shift 2 ;;
        --service-account) SERVICE_ACCOUNT="${2:?--service-account needs a value}"; shift 2 ;;
        --key-secret)      KEY_SECRET="${2:?--key-secret needs a value}"; shift 2 ;;
        --token-secret)    TOKEN_SECRET="${2:?--token-secret needs a value}"; shift 2 ;;
        --no-token)        WANT_TOKEN=0; shift ;;
        --require-iam)     PUBLIC=0; shift ;;
        --dry-run)         DRY_RUN=1; shift ;;
        -h|--help)         usage; exit 0 ;;
        *) die "unknown option: $1" "./scripts/deploy.sh --help" ;;
    esac
done

[ -f "$ROOT/Dockerfile" ] || die \
    "no Dockerfile at $ROOT -- this script must stay in the repo's scripts/ directory" \
    "clone the repository again and run scripts/deploy.sh from inside it"

say "Silkscreen -> Cloud Run"
say "repository: $ROOT"
[ "$DRY_RUN" -eq 1 ] && say "mode:       dry run (nothing will be created or deployed)"

# -------------------------------------------------------------- preflight ----
# Everything in this section is read-only. It runs in --dry-run too: knowing
# which prerequisite is missing is the entire point of the rehearsal.
step "Preflight"

command -v gcloud >/dev/null 2>&1 || die \
    "gcloud is not on PATH" \
    "install the Google Cloud SDK: https://cloud.google.com/sdk/docs/install"

ACCOUNT="$(gcloud auth list --filter=status:ACTIVE --format='value(account)' 2>/dev/null | head -n 1)"
[ -n "$ACCOUNT" ] || die \
    "no active gcloud credentials" \
    "gcloud auth login"

# `gcloud auth list` only reads a local file: it happily names an account whose
# refresh token expired weeks ago. Minting an access token is the cheap,
# side-effect-free way to find out whether those credentials still work, and
# finding out here beats finding out halfway through a Cloud Build.
gcloud auth print-access-token >/dev/null 2>&1 || missing \
    "the credentials for $ACCOUNT are stale -- gcloud cannot refresh them, so every check below is unreliable" \
    "gcloud auth login"
say "account:    $ACCOUNT"

if [ -z "$PROJECT" ]; then
    PROJECT="$(gcloud config get-value project 2>/dev/null | grep -v '^(unset)$' || true)"
fi
[ -n "$PROJECT" ] || die \
    "no GCP project resolved" \
    "./scripts/deploy.sh --project YOUR_PROJECT_ID   (or: gcloud config set project ...)"
say "project:    $PROJECT"
say "region:     $REGION"
say "service:    $SERVICE"

# Required APIs: reported, never enabled. Enabling an API on someone's project
# is a billing-relevant change and belongs to whoever owns the project.
# A plain string, not an array: bash 3.2 (still /bin/bash on macOS) errors on
# ${#arr[@]} for an empty array under `set -u`.
MISSING_APIS=""
ENABLED="$(gcloud services list --enabled --project "$PROJECT" --format='value(config.name)' 2>/dev/null || true)"
if [ -z "$ENABLED" ]; then
    warn "could not list enabled APIs on '$PROJECT' (no permission, or the Service Usage API is off)."
    warn "Skipping the API check -- if the deploy fails with SERVICE_DISABLED, run:"
    warn "  gcloud services enable ${REQUIRED_APIS[*]} --project '$PROJECT'"
else
    for api in "${REQUIRED_APIS[@]}"; do
        printf '%s\n' "$ENABLED" | grep -qx "$api" || MISSING_APIS="$MISSING_APIS $api"
    done
    MISSING_APIS="${MISSING_APIS# }"
    if [ -n "$MISSING_APIS" ]; then
        missing "these APIs are not enabled on '$PROJECT': $MISSING_APIS" \
            "gcloud services enable $MISSING_APIS --project '$PROJECT'"
    else
        say "APIs:       all ${#REQUIRED_APIS[@]} required APIs enabled"
    fi
fi

# ---------------------------------------------------------------- secrets ----
# Credentials go to Secret Manager, never to --set-env-vars: an env var on a
# Cloud Run revision is plainly visible to anyone with console read access on
# the project, and shows up in `gcloud run services describe` output that
# people paste into chats and issues.
step "Secrets"

secret_exists() {
    gcloud secrets describe "$1" --project "$PROJECT" >/dev/null 2>&1
}

SECRET_FLAGS=()

secret_exists "$KEY_SECRET" || missing \
    "Secret Manager has no secret named '$KEY_SECRET' in '$PROJECT'" \
    "printf %s \"\$GOOGLE_API_KEY\" | gcloud secrets create '$KEY_SECRET' --data-file=- --replication-policy=automatic --project '$PROJECT'"
SECRET_FLAGS+=("GOOGLE_API_KEY=${KEY_SECRET}:latest")
say "GOOGLE_API_KEY         <- $KEY_SECRET:latest"

if [ "$WANT_TOKEN" -eq 1 ]; then
    secret_exists "$TOKEN_SECRET" || missing \
        "Secret Manager has no secret named '$TOKEN_SECRET' in '$PROJECT'" \
        "openssl rand -base64 32 | tr -d '\\n' | gcloud secrets create '$TOKEN_SECRET' --data-file=- --replication-policy=automatic --project '$PROJECT'   (or pass --no-token to deploy an OPEN endpoint)"
    SECRET_FLAGS+=("SILKSCREEN_ACCESS_TOKEN=${TOKEN_SECRET}:latest")
    say "SILKSCREEN_ACCESS_TOKEN <- $TOKEN_SECRET:latest"
else
    say "SILKSCREEN_ACCESS_TOKEN  NOT SET (--no-token)"
    warn "Every route -- /generate, /chat/stream, /steps, /models -- will accept"
    warn "every caller. Each accepted request spends Gemini quota and CPU on your"
    warn "project's bill. Only do this behind --require-iam, or for a short,"
    warn "watched demo. With the token set, the gate is enforced in"
    warn "service/app.py (Handler._authorized); docs/deploy.md lists what stays"
    warn "open and why."
fi

# The runtime service account -- not the deploying user -- is what reads the
# secrets at start-up. Cloud Run defaults it to the project's compute SA.
if [ -z "$SERVICE_ACCOUNT" ]; then
    PROJECT_NUMBER="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)' 2>/dev/null || true)"
    if [ -n "$PROJECT_NUMBER" ]; then
        SERVICE_ACCOUNT="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
    fi
fi

if [ -n "$SERVICE_ACCOUNT" ]; then
    say "runtime SA: $SERVICE_ACCOUNT"
    CHECK_SECRETS="$KEY_SECRET"
    [ "$WANT_TOKEN" -eq 1 ] && CHECK_SECRETS="$CHECK_SECRETS $TOKEN_SECRET"
    for secret in $CHECK_SECRETS; do
        BINDING="$(gcloud secrets get-iam-policy "$secret" --project "$PROJECT" \
            --flatten='bindings[].members' \
            --filter="bindings.role:roles/secretmanager.secretAccessor AND bindings.members:${SERVICE_ACCOUNT}" \
            --format='value(bindings.members)' 2>/dev/null || true)"
        if [ -z "$BINDING" ]; then
            warn "$SERVICE_ACCOUNT cannot read secret '$secret'. The revision will"
            warn "fail to start. Grant it with:"
            warn "  gcloud secrets add-iam-policy-binding '$secret' \\"
            warn "      --member='serviceAccount:${SERVICE_ACCOUNT}' \\"
            warn "      --role='roles/secretmanager.secretAccessor' --project '$PROJECT'"
        fi
    done
else
    warn "could not resolve the runtime service account; skipping the IAM check."
    warn "It needs roles/secretmanager.secretAccessor on '$KEY_SECRET'."
fi

# ----------------------------------------------------------------- deploy ----
# Built from source (`--source .`) rather than from a locally built image:
#
#   * Cloud Build uses this repo's own root Dockerfile, so the deployed image is
#     the image CI already builds -- one definition, not two.
#   * It builds linux/amd64 regardless of the operator's machine. A `docker
#     build` on an Apple Silicon laptop produces an arm64 image that Cloud Run
#     refuses to start, and that failure surfaces as an unhelpful start-up error.
#   * Cloud Run can only pull from Artifact Registry / GCR. The ghcr.io images
#     that docker.yml publishes are not deployable here without a copy step, so
#     "reuse the release image" is not the shortcut it looks like.
#
# --image stays available for the redeploy case: same image, changed flags.
step "Deploy"

DEPLOY_ARGS=(
    run deploy "$SERVICE"
    --project "$PROJECT"
    --region "$REGION"
    --platform managed
    --port "$PORT"
    --memory "$MEMORY"
    --cpu "$CPU"
    --concurrency "$CONCURRENCY"
    --max-instances "$MAX_INSTANCES"
    --min-instances "$MIN_INSTANCES"
    --timeout "$TIMEOUT"
    --set-env-vars "GOOGLE_CLOUD_PROJECT=${PROJECT},USE_FIRESTORE=1"
    --set-secrets "$(IFS=,; printf '%s' "${SECRET_FLAGS[*]}")"
    --quiet
)

if [ -n "$IMAGE" ]; then
    DEPLOY_ARGS+=(--image "$IMAGE")
    say "source:     prebuilt image $IMAGE"
else
    DEPLOY_ARGS+=(--source "$ROOT")
    say "source:     $ROOT (Cloud Build, root Dockerfile)"
fi

[ -n "$SERVICE_ACCOUNT" ] && DEPLOY_ARGS+=(--service-account "$SERVICE_ACCOUNT")

if [ "$PUBLIC" -eq 1 ]; then
    DEPLOY_ARGS+=(--allow-unauthenticated)
    say "access:     public (--allow-unauthenticated)"
    say "            The client is a desktop app whose users have no Google"
    say "            accounts, so Cloud Run IAM has no identity to check. The"
    say "            bearer token is the real gate, not an extra layer."
else
    DEPLOY_ARGS+=(--no-allow-unauthenticated)
    say "access:     IAM-gated (--require-iam)"
fi

say "sizing:     ${CPU} vCPU / ${MEMORY} / concurrency ${CONCURRENCY} / max ${MAX_INSTANCES} instances / ${TIMEOUT}s timeout"

run gcloud "${DEPLOY_ARGS[@]}"

if [ "$DRY_RUN" -eq 1 ]; then
    say ""
    if [ "$PREREQS_MISSING" -eq 1 ]; then
        say "Dry run complete. Nothing was built, created or deployed."
        say "Prerequisites are missing -- see the warnings above. A real run stops"
        say "at the first one instead of continuing."
        exit 1
    fi
    say "Dry run complete. Nothing was built, created or deployed."
    exit 0
fi

# -------------------------------------------------------------------- url ----
URL="$(gcloud run services describe "$SERVICE" \
    --project "$PROJECT" --region "$REGION" \
    --format='value(status.url)' 2>/dev/null || true)"

[ -n "$URL" ] || die \
    "the deploy reported success but no URL could be read back" \
    "gcloud run services describe '$SERVICE' --region '$REGION' --project '$PROJECT'"

step "Deployed"
say "  $URL"
cat <<NEXT

Next steps:

  1. Smoke-test it (readyz, then one real board):

       SILKSCREEN_ACCESS_TOKEN=... ./scripts/smoke.sh "$URL"

  2. Record the URL in README.md. Nothing in the repo writes it down, and an
     unrecorded endpoint cannot be verified by anyone who did not deploy it.

  3. When judging is over, tear it down -- an idle service still holds a public
     endpoint and a live API key:

       gcloud run services delete "$SERVICE" --region "$REGION" --project "$PROJECT"

  Full procedure, rotation and teardown: docs/deploy.md
NEXT
