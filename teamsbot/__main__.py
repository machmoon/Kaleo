"""``python -m teamsbot`` — run the Teams bot's HTTP surface.

Thin on purpose, the way ``slackbot/__main__.py`` is: everything worth testing
lives in :mod:`teamsbot.app`, and an entry point that does its own work is an
entry point nothing covers.

``python -m teamsbot check`` is the one addition: a single client-credentials
request that says whether Entra issues an app token for the configured
registration, in the fixed vocabulary of :func:`teamsbot.graph.verify_credentials`
-- it prints no secret, no token and no Entra free text.
"""

from __future__ import annotations

import sys

from .app import main


def check(argv: list[str] | None = None) -> int:
    from .config import ConfigError, load_config
    from .graph import verify_credentials

    try:
        config = load_config()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    verdict = verify_credentials(config)
    for key, value in verdict.as_dict().items():
        print(f"{key}: {value}")
    print(
        "An app token proves the ids and the secret; it does not prove Graph "
        "permissions are consented or that the bot is running."
    )
    return 0 if verdict.ok else 1


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "check":
        raise SystemExit(check(sys.argv[2:]))
    raise SystemExit(main())
