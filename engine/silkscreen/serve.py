"""``silkscreen serve``: start the HTTP service and open the Hardy desktop app.

Thin on purpose. The server itself is ``service.app`` -- this module only does
the three things that stand between "the module exists" and "the app is open":

* Load ``.env``. The service deliberately does not read it (only the CLIs do),
  so without this step a key sitting in .env produces a service that answers
  every request with a missing-key 502, which reads as an outage.
* Resolve the port once, from ``--port`` or ``PORT``, and say what happened
  when the bind fails. The default is 8081, the repository's local
  convention (the frontend dev proxy and the desktop app both expect
  ``127.0.0.1:8081``), because 8080 is routinely already taken on a
  developer machine. The container is different: ``service.app`` and the
  ``Dockerfile`` keep Cloud Run's 8080, and this default never reaches them
  -- ``make_server`` is always handed the resolved port.
* Open the app, since "runs in one command" means the app is on screen. The
  desktop overlay (``app/``) is the product, so it is the default; ``--web``
  opens the browser SPA instead and ``--no-browser`` opens nothing. From a
  checkout with the Tauri toolchain the overlay is this tree's own build
  (``npm run tauri dev``, a child of this process that stops with it); without
  one, an installed ``Hardy.app``; with neither, the browser, saying why.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import shutil
import signal
import socket
import subprocess
import sys
import webbrowser
from pathlib import Path

from .onboard import load_dotenv, repo_root

__all__ = ["main"]

DEFAULT_PORT = 8081

#: The overlay's Vite dev server (``app/src-tauri/tauri.conf.json`` ``devUrl``).
#: Something already listening there means a ``tauri dev`` is running, and a
#: second one would fight it for the port.
DESKTOP_DEV_PORT = 1420

#: Where a release install of the overlay lives (``productName`` "Hardy").
INSTALLED_APP = Path("/Applications/Hardy.app")


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _launch_desktop(root: Path) -> tuple[subprocess.Popen | None, str | None]:
    """Open the desktop overlay.

    Returns ``(child to stop on exit, None)``, ``(None, why not)``, or
    ``(None, None)`` when it is open and owned by something else.
    """
    app = root / "app"
    env = dict(os.environ)
    cargo_bin = Path.home() / ".cargo" / "bin"
    if cargo_bin.is_dir():
        env["PATH"] = f"{cargo_bin}{os.pathsep}{env.get('PATH', '')}"
    npm = shutil.which("npm", path=env["PATH"])
    has_toolchain = (
        (app / "node_modules").is_dir()
        and npm is not None
        and shutil.which("cargo", path=env["PATH"]) is not None
    )
    if has_toolchain:
        if _port_in_use(DESKTOP_DEV_PORT):
            print(f"desktop already running (something is on :{DESKTOP_DEV_PORT})")
            return None, None
        print("desktop starting Hardy from app/ "
              "(npm run tauri dev; a first build takes minutes)")
        # Its own process group: npm spawns vite, cargo and the app binary,
        # and stopping serve must take all of them down, not just npm.
        child = subprocess.Popen(
            [npm, "run", "tauri", "dev"], cwd=app, env=env, start_new_session=True
        )
        return child, None
    if sys.platform == "darwin" and INSTALLED_APP.exists():
        print(f"desktop opening {INSTALLED_APP} "
              "(the installed release, not this checkout)")
        subprocess.run(["open", str(INSTALLED_APP)], check=False)
        return None, None
    return None, (
        "no desktop app to open: app/ needs `npm install` plus a Rust toolchain, "
        f"or install {INSTALLED_APP.name}"
    )


def _stop(child: subprocess.Popen | None) -> None:
    if child is None or child.poll() is not None:
        return
    if not hasattr(os, "killpg"):  # Windows: no process groups to signal
        child.terminate()
        return
    try:
        os.killpg(child.pid, signal.SIGTERM)
        child.wait(timeout=10)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        with contextlib.suppress(ProcessLookupError):
            os.killpg(child.pid, signal.SIGKILL)


def _import_service(root: Path):
    """Import ``service.app`` out of the checkout.

    ``service/`` is not part of the installed package (setuptools packages only
    ``engine/``), so it can only be reached through the repository on disk. A
    wheel installed away from its source tree therefore cannot serve, and
    saying so beats an ImportError traceback about a module nobody mentioned.
    """
    if not (root / "service" / "app.py").exists():
        raise SystemExit(
            f"error: no service/app.py under {root}.\n"
            "'silkscreen serve' runs the service out of the repository; "
            "install it editable with scripts/install.sh and serve from there."
        )
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from service.app import make_server  # noqa: PLC0415 - after sys.path setup

    return make_server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="silkscreen serve",
        description="Run the Silkscreen API and web UI, and open the Hardy desktop app.",
    )
    parser.add_argument(
        "-p", "--port", type=int, default=None,
        help=f"port to bind (default: $PORT, else {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--host", default="localhost",
        help="hostname to open in the browser (default: %(default)s); "
             "the server always binds 0.0.0.0",
    )
    parser.add_argument(
        "--web", action="store_true",
        help="open the web UI in a browser instead of the desktop app",
    )
    parser.add_argument(
        "--no-browser", action="store_true",
        help="bind and serve without opening anything",
    )
    args = parser.parse_args(argv)

    root = repo_root()

    # Before anything imports the service: app.py reads its configuration from
    # os.environ at request time, and never looks at .env itself.
    load_dotenv(root / ".env")

    port = args.port if args.port is not None else int(os.getenv("PORT", DEFAULT_PORT))
    # make_server() re-reads PORT when handed None; we always pass the resolved
    # value, so this only keeps the environment honest for anything downstream
    # that reports the port back (the container's own health checks do).
    os.environ["PORT"] = str(port)

    make_server = _import_service(root)

    try:
        server = make_server(port)
    except OSError as exc:
        print(f"error: could not bind port {port}: {exc}", file=sys.stderr)
        print(f"try:   silkscreen serve --port {port + 1}", file=sys.stderr)
        return 2

    url = f"http://{args.host}:{server.server_port}/"

    dist = Path(os.getenv("SILKSCREEN_WEB_DIST") or root / "frontend" / "dist")
    if (dist / "index.html").exists():
        print(f"web UI  {url}")
    else:
        # Serving the API alone is a legitimate mode, not an error -- but a
        # blank page at the root would otherwise look like a broken install.
        print(f"web UI  not built ({dist} has no index.html); serving the API only")
        print("        build it with: cd frontend && npm ci && npm run build")

    print(f"API     POST {url}generate   |   GET {url}healthz")
    if not os.getenv("GOOGLE_API_KEY"):
        # Generation is the whole point, so warn plainly rather than letting
        # the first request come back as a 502 the user has to decode.
        print("note    no GOOGLE_API_KEY set; /generate will fail. "
              "Run 'silkscreen setup' to store one.")
    # serve_forever() then blocks indefinitely, so a block-buffered stdout (any
    # pipe: `silkscreen serve | tee`, a supervisor, a CI log) would hold this
    # whole banner back until the process is killed. Flush it out now.
    print("Ctrl-C to stop.", flush=True)

    # The socket is already bound and listening, so anything the app sends
    # before serve_forever() starts waits in the accept backlog.
    desktop = None
    if not args.no_browser:
        why_not = None
        if not args.web:
            desktop, why_not = _launch_desktop(root)
            if why_not:
                print(f"note    {why_not}; opening the web UI instead")
        if args.web or why_not:
            webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        _stop(desktop)
        server.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry
    raise SystemExit(main())
