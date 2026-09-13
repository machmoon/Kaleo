"""``python -m slackbot`` — run the Slack bot.

* ``python -m slackbot``        — the Events API server (needs a public URL).
* ``python -m slackbot socket`` — the Socket Mode bridge to Ada on this laptop
  (no public URL; see ``slackbot/bridge.py``).
"""

import sys

if __name__ == "__main__":
    if sys.argv[1:2] == ["socket"]:
        from .bridge import main as socket_main

        raise SystemExit(socket_main(sys.argv[2:]))
    from .app import main

    raise SystemExit(main())
