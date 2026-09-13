"""Run Ada's local HTTP service as a parent-owned desktop sidecar.

The process writes one JSON readiness record to stdout, then remains alive
until its parent closes stdin.  Tying lifetime to the pipe prevents a crashed
or force-quit desktop shell from leaving an API listener behind.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from http.server import ThreadingHTTPServer
from typing import TextIO

from desktop.launcher import REPO_ROOT, load_dotenv, start_server, wait_for_health

HEALTH_TIMEOUT_S = 30.0

__all__ = ["main", "run"]


def run(
    *,
    stdin: TextIO = sys.stdin,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
    port: int = 0,
    start: Callable[[int], ThreadingHTTPServer] = start_server,
    wait: Callable[[str], bool] = wait_for_health,
) -> int:
    """Start on loopback, announce readiness, and stop when ``stdin`` closes."""
    load_dotenv(REPO_ROOT / ".env")
    try:
        server = start(port)
    except OSError as exc:
        print(f"error: could not start desktop service: {exc}", file=stderr)
        return 2

    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        if not wait(f"{base_url}/healthz"):
            message = (
                "error: desktop service health check failed after "
                f"{HEALTH_TIMEOUT_S:.0f}s"
            )
            print(
                message,
                file=stderr,
            )
            return 1

        json.dump({"event": "ready", "url": base_url}, stdout, separators=(",", ":"))
        stdout.write("\n")
        stdout.flush()

        # A pipe read blocks without polling while the parent is alive and
        # returns b''/'' immediately when every writer disappears.
        while stdin.read(8192):
            pass
        return 0
    except KeyboardInterrupt:
        return 0
    finally:
        server.shutdown()
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Ada's desktop API sidecar")
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="loopback port (default: an OS-assigned free port)",
    )
    args = parser.parse_args(argv)
    return run(port=args.port)


if __name__ == "__main__":
    raise SystemExit(main())
