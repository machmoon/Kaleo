# The headless Zoom Meeting SDK participant.
#
# NOT BUILT OR RUN BY THE TEST SUITE, and never built or run against a live
# Zoom account from this repository. See README.md in this directory before
# assuming any of this works.
#
# Modelled on zoom/meetingsdk-headless-linux-sample. Ubuntu 22.04 because that
# is what the Zoom Meeting SDK for Linux is built and supported against; a
# newer glibc has bitten that sample before.
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

# The SDK links against GTK/GLib, PulseAudio and a handful of X libraries even
# headless; upstream's list is the authority, this is a trimmed copy of it.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        libgl1 \
        libglib2.0-0 \
        libgtk-3-0 \
        libasound2 \
        libx11-xcb1 \
        libxcb-xfixes0 \
        libxcb-shape0 \
        libxfixes3 \
        libdrm2 \
        libegl1 \
        libnss3 \
        pulseaudio \
        python3 \
        xvfb \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/zoombot

# The Zoom Meeting SDK for Linux is downloaded from the Zoom App Marketplace
# under Zoom's licence and is deliberately NOT vendored in this repository.
# Put the archive you downloaded at zoombot/bot/vendor/ before building.
# The COPY fails loudly if it is missing, which is the intended behaviour: a
# build that silently produced an image without the SDK would produce a
# container that joins nothing while looking healthy.
COPY vendor/ /opt/zoombot/vendor/

COPY entrypoint.sh /opt/zoombot/entrypoint.sh
COPY control_stub.py /opt/zoombot/control_stub.py
RUN chmod +x /opt/zoombot/entrypoint.sh

# Loopback only. The control surface is unauthenticated by design (speak.py
# refuses any non-loopback control URL for exactly this reason), so publish it
# as 127.0.0.1:8781:8781 and never as 0.0.0.0.
ENV BOT_CONTROL_PORT=8781
EXPOSE 8781

# Text to speech is the operator's choice; see README.md. Piper is the
# documented option. Any command reading text on stdin and writing WAV on
# stdout will do.
ENV TTS_CMD=""

ENTRYPOINT ["/opt/zoombot/entrypoint.sh"]
