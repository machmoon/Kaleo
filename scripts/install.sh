#!/usr/bin/env bash
# One-command install for macOS and Linux:
#
#     ./scripts/install.sh              # venv + engine + web bundle
#     ./scripts/install.sh --no-web     # skip the frontend build
#     ./scripts/install.sh --dry-run    # print the plan, change nothing
#
# Re-running is safe: an existing .venv is reused and pip/npm are both
# idempotent. The script never uses sudo -- everything it writes lives inside
# the repository, so a failed run leaves nothing behind on the system.
set -euo pipefail

# Every path below is quoted because the repo is routinely checked out under a
# directory with spaces ("Desktop/Coding/..." today, "My Documents" tomorrow);
# an unquoted $ROOT silently installs into the wrong place instead of failing.
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$ROOT/.venv"
PY="$VENV/bin/python"

MIN_PY_MINOR=11   # pyproject: requires-python >= 3.11
MIN_NODE_MAJOR=22 # CI pins 22; Vite 8 refuses to start on anything older

WANT_WEB=1
DRY_RUN=0

say()  { printf '%s\n' "$*"; }
step() { printf '\n==> %s\n' "$*"; }

# Fail loudly and actionably. A half-installed tree is worse than no tree at
# all, because the next command fails somewhere far from the real cause.
die() {
    printf '\nerror: %s\n' "$1" >&2
    if [ $# -gt 1 ]; then
        printf 'try:   %s\n' "$2" >&2
    fi
    exit 1
}

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
    sed -n '2,9p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

for arg in "$@"; do
    case "$arg" in
        --no-web|--skip-frontend) WANT_WEB=0 ;;
        --dry-run)                DRY_RUN=1 ;;
        -h|--help)                usage; exit 0 ;;
        *) die "unknown option: $arg" "./scripts/install.sh --help" ;;
    esac
done

[ -f "$ROOT/pyproject.toml" ] || die \
    "no pyproject.toml at $ROOT -- this script must stay in the repo's scripts/ directory" \
    "git clone the repository again and run scripts/install.sh from inside it"

say "Silkscreen installer"
say "repository: $ROOT"
[ "$DRY_RUN" -eq 1 ] && say "mode:       dry run (nothing will be written)"

# ---------------------------------------------------------------- python ----
step "Locating Python >= 3.$MIN_PY_MINOR"

# Ask each candidate for its own version rather than trusting the name: on
# macOS `python3` is whatever the last installer won, and a 3.9 shim called
# python3.11 is exactly the kind of thing that breaks halfway through pip.
python_ok() {
    "$1" -c "import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, $MIN_PY_MINOR) else 1)" \
        >/dev/null 2>&1
}

PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 && python_ok "$candidate"; then
        PYTHON="$(command -v "$candidate")"
        break
    fi
done

[ -n "$PYTHON" ] || die \
    "no Python 3.$MIN_PY_MINOR or newer on PATH" \
    "install it with 'brew install python@3.12' (macOS) or your distribution's package manager"

say "found: $PYTHON ($("$PYTHON" -c 'import platform; print(platform.python_version())'))"

# ------------------------------------------------------------------ venv ----
step "Virtual environment"
if [ -x "$PY" ]; then
    say "reusing $VENV"
elif [ -e "$VENV" ]; then
    # A directory that exists but has no interpreter is a half-created venv
    # from an interrupted run; guessing whether to reuse it is how you get an
    # environment that imports one Python's stdlib with another's packages.
    die "$VENV exists but has no bin/python (interrupted install?)" \
        "rm -rf '$VENV' && ./scripts/install.sh"
else
    say "creating $VENV"
    run "$PYTHON" -m venv "$VENV" || die \
        "could not create a virtual environment" \
        "on Debian/Ubuntu: sudo apt install python3-venv, then re-run this script"
fi

step "Installing the engine (editable, with dev/agents/cloud/adk/cad extras)"
# --quiet rather than a shell redirect, so --dry-run still shows the command.
run "$PY" -m pip install --quiet --upgrade pip
# `cad` is the build123d enclosure kernel, and it is what makes the case step
# work at all: without it that step refuses in words naming this extra rather
# than shipping a worse case, and the kernel tests skip.
run "$PY" -m pip install -e "$ROOT[dev,agents,cloud,adk,cad]" || die \
    "pip install failed" \
    "re-run with the output above; if ortools has no wheel for this platform, check 'python -VV'"

# -------------------------------------------------------------- frontend ----
WEB_BUILT=0
if [ "$WANT_WEB" -eq 0 ]; then
    step "Web UI: skipped (--no-web)"
elif ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then
    step "Web UI: skipped"
    say "Node and npm were not found on PATH. The API and the CLI work without"
    say "them; only the browser UI needs a build. Install Node $MIN_NODE_MAJOR+ from"
    say "https://nodejs.org and re-run this script to add it."
else
    NODE_MAJOR="$(node -p 'process.versions.node.split(".")[0]')"
    if [ "$NODE_MAJOR" -lt "$MIN_NODE_MAJOR" ]; then
        step "Web UI: skipped"
        say "Node $(node --version) is older than v$MIN_NODE_MAJOR, which Vite requires."
        say "The API and the CLI work without it. Upgrade Node and re-run to add the UI."
    else
        step "Building the web UI (Node $(node --version))"
        # `npm ci` needs a lockfile and deletes node_modules; fall back to
        # `npm install` rather than failing an otherwise-fine install.
        if [ -f "$ROOT/frontend/package-lock.json" ]; then
            run npm --prefix "$ROOT/frontend" ci || die \
                "npm ci failed in $ROOT/frontend" \
                "rm -rf '$ROOT/frontend/node_modules' && ./scripts/install.sh"
        else
            run npm --prefix "$ROOT/frontend" install || die \
                "npm install failed in $ROOT/frontend" \
                "run it by hand in $ROOT/frontend to see the full output"
        fi
        run npm --prefix "$ROOT/frontend" run build || die \
            "the web build failed" \
            "cd '$ROOT/frontend' && npm run build"
        WEB_BUILT=1
    fi
fi

# ------------------------------------------------------------ next steps ----
if [ "$DRY_RUN" -eq 1 ]; then
    say ""
    say "Dry run complete. Nothing was written."
    exit 0
fi

step "Installed"
say "  engine   $VENV"
if [ "$WEB_BUILT" -eq 1 ] || [ -f "$ROOT/frontend/dist/index.html" ]; then
    say "  web UI   $ROOT/frontend/dist"
else
    say "  web UI   not built (API-only)"
fi

cat <<'NEXT'

Next steps:

  1. Configure a Gemini API key (writes .env, never echoes the key):

       ./.venv/bin/silkscreen setup

  2. Start the app and open it in a browser:

       ./.venv/bin/silkscreen serve

  Or generate a board straight from the command line:

       ./.venv/bin/silkscreen "an stm32 stepper driver"

  Everything except the model calls runs offline; the test suite needs no key:

       ./.venv/bin/python -m pytest -q
NEXT
