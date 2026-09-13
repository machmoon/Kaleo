#!/usr/bin/env bash
# Post-deploy smoke check against a live Silkscreen service.
#
#     SILKSCREEN_ACCESS_TOKEN=... ./scripts/smoke.sh https://silkscreen-xxxx.run.app
#     ./scripts/smoke.sh --dry-run https://...        # print the plan, call nothing
#     ./scripts/smoke.sh --health-only https://...    # GET /readyz and stop
#     ./scripts/smoke.sh --help
#
# Two checks, in order: GET /readyz must answer {"ok": true}, then one real
# POST /generate must come back with every field the desktop client reads. The
# second one spends a Gemini call and a CP-SAT solve -- that is the point. A
# service that answers /readyz and then 500s on the only route anyone uses is
# exactly the failure this check exists to catch.
#
# /readyz, not /healthz: the service answers both identically, but Google's
# frontend intercepts /healthz on run.app domains at the edge and answers 404
# before the container sees the request, so probing it from outside always
# fails on a live deploy.
#
# The token is read from the environment and never printed, never logged, and
# never passed on a command line (argv is world-readable in `ps`). It reaches
# curl through a private, mode-600 temporary config file that is deleted on
# exit, including on failure.
set -euo pipefail

URL=""
TOKEN="${SILKSCREEN_ACCESS_TOKEN:-}"
INTENT="${SILKSCREEN_SMOKE_INTENT:-an stm32 blinky board with one led and a decoupling capacitor}"
HEALTH_ONLY=0
DRY_RUN=0
TIMEOUT="${SILKSCREEN_SMOKE_TIMEOUT:-900}"

say()  { printf '%s\n' "$*"; }
step() { printf '\n==> %s\n' "$*"; }
pass() { printf '  ok    %s\n' "$*"; }

die() {
    printf '\nFAIL: %s\n' "$1" >&2
    if [ $# -gt 1 ]; then
        printf 'hint: %s\n' "$2" >&2
    fi
    exit 1
}

usage() {
    sed -n '2,23p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    cat <<'OPTS'

Options:
  --health-only   run only the free GET /readyz check
  --intent TEXT   the prompt to generate (default: a small STM32 board)
  --timeout SECS  per-request timeout (default: 900)
  --dry-run       print what would be requested; make no HTTP calls
  -h, --help      this text

Environment:
  SILKSCREEN_ACCESS_TOKEN   bearer token, if the service was deployed with one
OPTS
}

while [ $# -gt 0 ]; do
    case "$1" in
        --health-only) HEALTH_ONLY=1; shift ;;
        --intent)      INTENT="${2:?--intent needs a value}"; shift 2 ;;
        --timeout)     TIMEOUT="${2:?--timeout needs a value}"; shift 2 ;;
        --dry-run)     DRY_RUN=1; shift ;;
        -h|--help)     usage; exit 0 ;;
        -*)            die "unknown option: $1" "./scripts/smoke.sh --help" ;;
        *)             URL="$1"; shift ;;
    esac
done

[ -n "$URL" ] || die "no service URL given" \
    "./scripts/smoke.sh https://silkscreen-xxxx-uc.a.run.app"

# A trailing slash would build //readyz, which the router answers with a 404.
URL="${URL%/}"

case "$URL" in
    https://*) ;;
    http://127.0.0.1*|http://localhost*)
        say "note: plain http against localhost -- fine for a local check, never for a deployed URL" ;;
    *) die "refusing to send a bearer token over '$URL'" \
           "use the https:// URL that scripts/deploy.sh printed" ;;
esac

command -v curl >/dev/null 2>&1 || die "curl is not on PATH" "install curl"
command -v python3 >/dev/null 2>&1 || die "python3 is not on PATH" \
    "install Python 3.11+ -- the response assertions are a python3 -c one-liner"

say "Silkscreen smoke check"
say "target: $URL"
if [ -n "$TOKEN" ]; then
    say "auth:   bearer token from SILKSCREEN_ACCESS_TOKEN (not printed)"
else
    say "auth:   none. If the service was deployed with a token, /generate will 401."
fi
[ "$DRY_RUN" -eq 1 ] && say "mode:   dry run (no HTTP calls)"

# ----------------------------------------------------------------- readyz ----
step "GET $URL/readyz"

if [ "$DRY_RUN" -eq 1 ]; then
    say "  would run: curl -fsS --max-time 30 '$URL/readyz'"
else
    HEALTH="$(curl -fsS --max-time 30 "$URL/readyz" 2>/dev/null || true)"
    [ -n "$HEALTH" ] || die "no response from $URL/readyz" \
        "check the service exists and is public: gcloud run services describe ... --format='value(status.url)'"
    printf '%s' "$HEALTH" | python3 -c '
import json, sys
try:
    body = json.load(sys.stdin)
except json.JSONDecodeError:
    raise SystemExit("/readyz did not return JSON")
if body.get("ok") is not True:
    raise SystemExit("/readyz returned %r, expected {\"ok\": true}" % (body,))
' || die "/readyz did not answer {\"ok\": true}" "curl -i '$URL/readyz'"
    pass '{"ok": true}'
fi

if [ "$HEALTH_ONLY" -eq 1 ]; then
    step "Smoke check passed (health only)"
    exit 0
fi

# --------------------------------------------------------------- generate ----
# One real board. This spends a model call and a solve, so it is the last thing
# the script does and the one it reports loudest about.
step "POST $URL/generate"
say "  intent: $INTENT"

REQ="$(python3 -c 'import json,sys; print(json.dumps({"intent": sys.argv[1]}))' "$INTENT")"

if [ "$DRY_RUN" -eq 1 ]; then
    say "  would run: curl -sS --max-time $TIMEOUT -X POST '$URL/generate'"
    say "             -H 'Content-Type: application/json' -H 'Authorization: Bearer <redacted>'"
    say "             --data '$REQ'"
    say ""
    say "Dry run complete. Nothing was requested."
    exit 0
fi

# The token goes in through a curl --config file rather than a -H argument:
# argv is world-readable via `ps` on a shared machine, and a CI runner would
# echo it into the job log. mktemp creates the file mode 600; the trap removes
# it on every exit path, including the failure ones below.
CURL_CONFIG="$(mktemp)"
trap 'rm -f "$CURL_CONFIG"' EXIT
chmod 600 "$CURL_CONFIG"
{
    printf 'header = "Content-Type: application/json"\n'
    if [ -n "$TOKEN" ]; then
        printf 'header = "Authorization: Bearer %s"\n' "$TOKEN"
    fi
} > "$CURL_CONFIG"

BODY_FILE="$(mktemp)"
trap 'rm -f "$CURL_CONFIG" "$BODY_FILE"' EXIT

HTTP_CODE="$(curl -sS --max-time "$TIMEOUT" -X POST "$URL/generate" \
    --config "$CURL_CONFIG" \
    --data "$REQ" \
    -o "$BODY_FILE" -w '%{http_code}' || true)"

case "$HTTP_CODE" in
    200) ;;
    401|403)
        die "POST /generate returned $HTTP_CODE" \
            "the service requires a bearer token; export SILKSCREEN_ACCESS_TOKEN and re-run" ;;
    ""|000)
        die "POST /generate produced no HTTP status (connection failed or timed out after ${TIMEOUT}s)" \
            "gcloud run services logs read <service> --region <region> --limit 50" ;;
    *)
        # The body may carry an error_id the logs can be searched by. It never
        # carries the token, so printing it is safe.
        say "  response body:"
        head -c 2000 "$BODY_FILE" >&2 || true
        printf '\n' >&2
        die "POST /generate returned HTTP $HTTP_CODE, expected 200" \
            "gcloud run services logs read <service> --region <region> --limit 50" ;;
esac
pass "HTTP 200"

# Every field the desktop client actually reads. Missing one is a broken
# deploy even when the status line says 200.
python3 - "$BODY_FILE" <<'PY' || die "POST /generate returned 200 but the body is not a usable board" "re-run with the body printed: curl ... | head -c 2000"
import json
import sys

with open(sys.argv[1], "rb") as handle:
    try:
        body = json.load(handle)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"/generate did not return JSON: {exc}")

if not isinstance(body, dict):
    raise SystemExit("/generate returned %s, expected a JSON object" % type(body).__name__)

problems = []
for field in ("board_mm", "status", "parts", "kicad_pcb", "placements"):
    if field not in body:
        problems.append(f"missing field {field!r}")

board = body.get("board_mm")
if not (isinstance(board, list) and len(board) == 2 and all(
        isinstance(v, (int, float)) and v > 0 for v in board)):
    problems.append(f"board_mm is {board!r}, expected two positive numbers")

parts = body.get("parts")
if not (isinstance(parts, list) and parts):
    problems.append("parts is empty -- a board with no footprints is not a board")

pcb = body.get("kicad_pcb")
if not (isinstance(pcb, str) and pcb.lstrip().startswith("(kicad_pcb")):
    problems.append("kicad_pcb is not a KiCad board file")

placements = body.get("placements")
if not isinstance(placements, dict) or not placements.get("parts"):
    problems.append("placements carries no part geometry -- the board well would render empty")

status = body.get("status")
if not isinstance(status, str) or not status:
    problems.append(f"status is {status!r}")

if problems:
    raise SystemExit("\n".join("  - " + p for p in problems))

print(f"  ok    board_mm  {board[0]} x {board[1]} mm")
print(f"  ok    status    {status}")
print(f"  ok    parts     {len(parts)} footprints")
print(f"  ok    kicad_pcb {len(pcb)} bytes")
print(f"  ok    placements {len(placements['parts'])} placed parts")
PY

step "Smoke check passed"
say "  $URL is live and generating boards."
say "  Record the URL in README.md if it is not there yet."
