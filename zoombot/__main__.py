"""``python -m zoombot`` — run the Zoom webhook surface.

One process: it validates the configuration (naming every gap at once), binds
``POST /zoom/events`` and ``GET /healthz``, and serves until interrupted.
Nothing is polled and nothing is joined — Zoom calls this, not the other way
round.

Exit codes follow the CLI convention: 0 success, 2 a configuration problem.
"""

from .app import main

if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
