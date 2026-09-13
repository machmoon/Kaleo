#!/usr/bin/env bash
# Provision Ada's voice: Kokoro-82M, locally, no key and no per-word cost.
#
# WHY THIS FILE EXISTS. `service/tts.py` deliberately refuses to download
# weights on demand -- a first spoken word that silently pulls 330 MB is a
# surprise on a metered connection and a hang on a hot path. That refusal is
# right, but until now its consequence was that the recipe existed ONLY as
# prose inside a Python docstring (`kokoro_paths`), so "provisioning is an
# explicit operator step" meant, in practice, "there is no way to do it that
# anybody can find". A refusal to act automatically is only honest if the
# manual path is real. This is the manual path.
#
# Running this script IS the consent. It says what it will fetch and how big
# that is before it fetches anything, and it re-runs safely: a file already
# the right size is left alone.
#
#     ./scripts/install_voice.sh
#
# Then restart the service and check:
#
#     curl -s localhost:8081/speak | python3 -m json.tool
#
# `"selected": "kokoro"` means Ada has her voice. Anything else prints the
# reason in words -- `service/tts.py` never answers with a quiet zero.

set -euo pipefail

# The fp32 graph, NOT a quantised one. Measured on an M4 (2026-09-07): int8 is
# 109 MiB and RTF 1.52 -- SLOWER THAN REAL TIME -- because onnxruntime's CPU
# kernels for those graphs fall back to reference implementations on ARM.
# fp32 is 310 MiB and RTF 0.77. Disk is the cheap resource here; do not
# "optimise" this to the smaller file. See `kokoro_paths` for the full table.
RELEASE="https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1"
MODEL_URL="$RELEASE/kokoro-v1.0.onnx"
VOICES_URL="$RELEASE/voices-v1.0.bin"

# Byte-exact sizes of the two release assets, so a truncated download or an
# HTML error page saved under a .onnx name is caught here rather than surfacing
# later as an onnxruntime parse error that names nothing useful.
MODEL_BYTES=325505369
VOICES_BYTES=28214398

# Mirrors `service/tts.py:kokoro_paths`, including the two overrides. Kept out
# of the checkout on purpose: .gitignore protects a repo from junk, and a
# 330 MB weight file should never be in a position to need protecting.
VOICE_HOME="${KALEO_VOICE_HOME:-$HOME/.kaleo/voices}"
MODEL_PATH="${KALEO_KOKORO_MODEL:-$VOICE_HOME/kokoro/model.onnx}"
VOICES_PATH="${KALEO_KOKORO_VOICES:-$VOICE_HOME/kokoro/voices.bin}"

PY="${PYTHON:-./.venv/bin/python}"

say() { printf '\n== %s\n' "$1"; }

# Size on disk, or 0. Portable across BSD (macOS) and GNU stat.
size_of() {
  [ -f "$1" ] || { echo 0; return; }
  stat -f%z "$1" 2>/dev/null || stat -c%s "$1" 2>/dev/null || echo 0
}

fetch() {
  local url="$1" dest="$2" want="$3" name="$4"
  if [ "$(size_of "$dest")" = "$want" ]; then
    echo "  $name: already present and the right size, skipping"
    return
  fi
  if [ -f "$dest" ]; then
    echo "  $name: present but $(size_of "$dest") bytes, not $want -- refetching"
  fi
  # --fail so an HTTP error is an error rather than a saved error page;
  # --location because the release redirects to a signed asset host.
  # Downloaded beside the target and moved into place only once complete, so
  # an interrupted run can never leave a half file that looks provisioned.
  curl --fail --location --progress-bar --output "$dest.part" "$url"
  local got
  got="$(size_of "$dest.part")"
  if [ "$got" != "$want" ]; then
    rm -f "$dest.part"
    echo "  $name: got $got bytes, expected $want -- refusing to install it" >&2
    exit 1
  fi
  mv "$dest.part" "$dest"
  echo "  $name: installed ($got bytes)"
}

say "Ada's voice: Kokoro-82M (Apache-2.0 code AND weights)"
cat <<EOF
  This downloads two files, about 340 MB in total, to:

    $MODEL_PATH   (310 MiB, the ONNX graph)
    $VOICES_PATH   (27 MiB, all 54 voices)

  and installs the 'kokoro-onnx' package (which brings onnxruntime) into
  $PY.

  Nothing here calls a hosted API, and no key is involved: once this
  finishes, synthesis happens on this machine at no per-word cost.
EOF

say "Downloading weights"
mkdir -p "$(dirname "$MODEL_PATH")" "$(dirname "$VOICES_PATH")"
fetch "$MODEL_URL" "$MODEL_PATH" "$MODEL_BYTES" "model.onnx"
fetch "$VOICES_URL" "$VOICES_PATH" "$VOICES_BYTES" "voices.bin"

say "Installing kokoro-onnx"
if [ ! -x "$PY" ]; then
  echo "  no interpreter at $PY -- set PYTHON=/path/to/python and re-run" >&2
  exit 1
fi
if "$PY" -c 'import kokoro_onnx' 2>/dev/null; then
  echo "  already installed"
else
  "$PY" -m pip install kokoro-onnx
fi

# The check is deliberately the SAME one the service makes, rather than a
# second opinion that could disagree with it: `KokoroEngine.status()` is what
# `GET /speak` reports, so if it says ready, the service will too.
say "Verifying"
"$PY" - <<'PYEOF'
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd()))
from service.tts import KokoroEngine

status = KokoroEngine().status()
print(f"  kokoro: {status.state} -- {status.detail}")
if status.state != "ready":
    if status.hint:
        print(f"  fix: {status.hint}")
    sys.exit(1)
print("\n  Restart the service, then: curl -s localhost:8081/speak")
PYEOF
