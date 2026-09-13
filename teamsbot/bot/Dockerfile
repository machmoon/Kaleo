# The calling bot's container: control plane only, and it says so on startup.
#
# NOT BUILT OR RUN BY THE TEST SUITE, and never run against a live Microsoft
# tenant from this repository. `docker build` here produces a container that
# answers a health check and refuses `POST /say` with a 501 naming the missing
# media stack (see control.py and README.md). That is the honest skeleton an
# operator completes; it is not a working Teams calling bot.
#
# Why there is no media stack in this image: joining a Teams call as a
# participant and emitting audio needs Microsoft's Graph Communications
# calling/media libraries, which are a .NET dependency with their own platform
# requirements, licensing and tenant policy. Vendoring a half-configured copy
# would produce exactly the failure this repo refuses to ship — something that
# looks like it works.
#
#   docker build -t silkscreen-teams-callingbot teamsbot/bot
#   docker run --rm -p 127.0.0.1:8791:8791 silkscreen-teams-callingbot
#
# Bind it to loopback. This surface has no authentication of its own
# (teamsbot/speak.py refuses plaintext http to anything but loopback for that
# reason): anything that can reach this port can make the bot speak.

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8791

WORKDIR /app

# Standard library only, exactly like the rest of this package: no pip install
# step, and nothing to audit beyond the base image.
COPY control.py /app/control.py

# Not root. The control surface needs no privileges and holds the credentials
# a real media backend would use.
RUN useradd --create-home --uid 10001 bot
USER bot

EXPOSE 8791

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,os; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8791')+'/healthz', timeout=3)"

CMD ["python", "/app/control.py"]
