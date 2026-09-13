#!/usr/bin/env bash
# Entrypoint for the headless Zoom Meeting SDK participant.
#
# NOT BUILT OR RUN BY THE TEST SUITE, and never run against a live Zoom
# account from this repository. See README.md.
#
# What this does today: check the environment, then start the control-surface
# stub, which answers every /say with {"spoken": false, ...}. That refusal is
# deliberate. A stub that answered {"spoken": true} would let the Python side
# report "spoke_via: meeting_sdk" for a meeting in which nothing was heard,
# which is the one failure this whole package is shaped to prevent.
#
# What a working version does instead: launch the participant built from
# zoom/meetingsdk-headless-linux-sample against /opt/zoombot/vendor, join the
# meeting with a JWT signed from ZOOM_SDK_KEY/ZOOM_SDK_SECRET, and serve the
# same three endpoints, answering {"spoken": true} only after audio has been
# handed to the SDK's raw audio sender.
set -euo pipefail

missing=()
for var in ZOOM_SDK_KEY ZOOM_SDK_SECRET ZOOM_MEETING_NUMBER; do
    if [[ -z "${!var:-}" ]]; then
        missing+=("$var")
    fi
done
# Name every gap at once, the config.py convention: a container that dies on
# the first missing value makes you restart it once per variable.
if (( ${#missing[@]} > 0 )); then
    echo "zoombot/bot: not configured; missing: ${missing[*]}" >&2
    echo "zoombot/bot: see zoombot/bot/README.md for what each one is." >&2
    exit 78  # EX_CONFIG
fi

if [[ ! -e /opt/zoombot/vendor/zoom-meeting-sdk-linux.tar.xz ]]; then
    echo "zoombot/bot: the Zoom Meeting SDK archive is not present." >&2
    echo "zoombot/bot: it is downloaded from the Zoom App Marketplace under" >&2
    echo "zoombot/bot: Zoom's licence and is not vendored in this repository." >&2
    exit 78
fi

# PulseAudio inside the container gives the SDK a sink to render into; Xvfb
# because the SDK still wants a display even headless. Both come straight from
# upstream's sample.
pulseaudio --start --exit-idle-time=-1 >/dev/null 2>&1 || true
Xvfb :99 -screen 0 640x480x16 >/dev/null 2>&1 &
export DISPLAY=:99

echo "zoombot/bot: starting the control stub on 127.0.0.1:${BOT_CONTROL_PORT:-8781}" >&2
echo "zoombot/bot: the participant itself is NOT implemented; every /say is refused." >&2
exec python3 /opt/zoombot/control_stub.py
