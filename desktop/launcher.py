"""Run silkscreen as a desktop application, with no new toolchain.

The service already serves the built SPA same-origin from ``frontend/dist``,
so the only things standing between that and an application are a window with
no browser chrome and a process lifecycle that ties the two together. This
module supplies both:

* the HTTP server runs **in this process**, bound to loopback on an
  OS-assigned port;
* the window is a Chromium-family browser in ``--app=`` mode, in its own
  profile directory, so it is a real standalone window rather than a tab;
* closing the window stops the server, and Ctrl-C does the same.

This is deliberately not a native shell -- see TAURI.md for the plan for that,
and README.md for what this is and is not.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from http.server import ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Where the app profile lives. A dedicated profile is not a nicety: launching
#: Chrome with --app= against the user's *default* profile hands the URL to the
#: already-running Chrome and exits immediately, so we would have no process to
#: wait on and no way to notice the window closing. A private profile forces a
#: new browser process that we own for its whole life.
PROFILE_DIR = Path(__file__).resolve().parent / ".profile"

HEALTH_TIMEOUT_S = 30.0
HEALTH_POLL_S = 0.1

__all__ = ["find_app_browser", "main", "start_server", "wait_for_health"]


def load_dotenv(path: Path) -> None:
    """Minimal .env reader, mirroring ``silkscreen.cli._load_dotenv``.

    ``service/app.py`` deliberately does not read .env -- in Cloud Run the
    environment is the environment. On a desktop there is no deploy step to
    export anything, so a launcher that skipped this would open a window whose
    every run fails on a missing GOOGLE_API_KEY.
    """
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def start_server(port: int = 0) -> ThreadingHTTPServer:
    """Serve the API and the built bundle on loopback, in a background thread.

    Not ``service.app.make_server``: that binds 0.0.0.0 because Cloud Run
    requires it, which on a laptop publishes an unauthenticated /generate --
    an endpoint that spends the user's Gemini quota -- to everything on the
    café wifi. A desktop app has exactly one client, on this machine.

    Port 0 lets the kernel choose. Asking a probe socket for a free port and
    then binding it a moment later is the classic TOCTOU race; there is no
    reason to run it when bind(0) reports the port it actually got.
    """
    # Imported here, not at module scope: it costs an ortools import, and
    # --help should not pay for it.
    sys.path.insert(0, str(REPO_ROOT))
    from service.app import Handler

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def wait_for_health(url: str, timeout_s: float = HEALTH_TIMEOUT_S) -> bool:
    """Poll /healthz until it answers, or the deadline passes.

    Sleeping a fixed second and hoping is how a launcher opens a window onto
    connection-refused: the first request is served only once the listener
    thread is actually accepting, and how long that takes is not ours to
    predict on a cold, loaded machine.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as response:
                if response.status == 200:
                    payload = json.loads(response.read() or b"{}")
                    if payload.get("ok"):
                        return True
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            pass
        time.sleep(HEALTH_POLL_S)
    return False


def _macos_candidates() -> list[Path]:
    """Chromium-family app bundles, in preference order.

    The executable inside a .app is not on PATH, so `which` finds nothing on
    macOS however many browsers are installed. Both /Applications and the
    per-user ~/Applications are checked -- a Homebrew-cask Chrome can land in
    either depending on how the cask was installed.
    """
    names = [
        ("Google Chrome.app", "Google Chrome"),
        ("Microsoft Edge.app", "Microsoft Edge"),
        ("Brave Browser.app", "Brave Browser"),
        ("Chromium.app", "Chromium"),
        ("Vivaldi.app", "Vivaldi"),
    ]
    roots = [Path("/Applications"), Path.home() / "Applications"]
    return [
        root / bundle / "Contents" / "MacOS" / binary
        for root in roots
        for bundle, binary in names
    ]


def _windows_candidates() -> list[Path]:
    """Chromium-family exes under the three install roots Windows uses.

    Chrome installs per-machine under Program Files or per-user under
    LOCALAPPDATA depending on whether the installer had admin rights, and a
    per-user install is on none of the machine-wide paths -- so checking one
    root reports "no browser" on a perfectly ordinary desktop.
    """
    roots = [
        os.environ.get("PROGRAMFILES"),
        os.environ.get("PROGRAMFILES(X86)"),
        os.environ.get("LOCALAPPDATA"),
    ]
    relatives = [
        Path("Google/Chrome/Application/chrome.exe"),
        Path("Microsoft/Edge/Application/msedge.exe"),
        Path("BraveSoftware/Brave-Browser/Application/brave.exe"),
        Path("Chromium/Application/chrome.exe"),
    ]
    return [
        Path(root) / relative
        for root in roots
        if root
        for relative in relatives
    ]


_LINUX_COMMANDS = [
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "microsoft-edge",
    "microsoft-edge-stable",
    "brave-browser",
    "vivaldi-stable",
]


def find_app_browser() -> Path | None:
    """A Chromium-family executable that understands ``--app=``, or None.

    Only Chromium's family has app mode. Firefox dropped ``-ssb`` and Safari
    never had it, so on a machine with neither Chrome nor Edge the honest
    answer is None and the caller falls back to an ordinary browser tab.
    """
    if sys.platform == "darwin":
        candidates = _macos_candidates()
    elif os.name == "nt":
        candidates = _windows_candidates()
    else:
        # PATH is the whole story on Linux; snap and flatpak both drop a
        # wrapper there.
        candidates = [Path(p) for c in _LINUX_COMMANDS if (p := shutil.which(c))]

    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def open_app_window(browser: Path, url: str, profile: Path) -> subprocess.Popen[bytes]:
    """Open ``url`` as a standalone window and return the browser process."""
    profile.mkdir(parents=True, exist_ok=True)
    argv = [
        str(browser),
        f"--app={url}",
        f"--user-data-dir={profile}",
        "--window-size=1440,900",
        # Otherwise the first launch of the private profile spends the window
        # on a welcome flow and a "make me default?" prompt instead of the app.
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate",
    ]
    return subprocess.Popen(
        argv,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="silkscreen-app",
        description="Run silkscreen as a desktop app: local service + app window.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="port to bind on loopback (default: an OS-assigned free one)",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="start the service and print the URL, but open no window",
    )
    parser.add_argument(
        "--profile",
        type=Path,
        default=PROFILE_DIR,
        help="browser profile directory for the app window (default: %(default)s)",
    )
    args = parser.parse_args(argv)

    load_dotenv(REPO_ROOT / ".env")
    sys.path.insert(0, str(REPO_ROOT))
    # What Setup saved under ~/.kaleo; after .env so a file never beats it.
    __import__("service.envfiles").envfiles.apply_saved_env()

    dist = REPO_ROOT / "frontend" / "dist" / "index.html"
    if not dist.is_file():
        # Without the bundle the window would open onto the API's bare JSON
        # health object, which looks exactly like a broken app.
        print(
            "error: frontend/dist is not built -- "
            "run `cd frontend && npm install && npm run build` first",
            file=sys.stderr,
        )
        return 2

    try:
        server = start_server(args.port)
    except OSError as exc:
        print(f"error: could not bind port {args.port}: {exc}", file=sys.stderr)
        return 2

    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"silkscreen service on {url}", file=sys.stderr)

    browser_process: subprocess.Popen[bytes] | None = None
    try:
        if not wait_for_health(f"{url}healthz"):
            print(
                f"error: /healthz did not answer within {HEALTH_TIMEOUT_S:.0f}s",
                file=sys.stderr,
            )
            return 1
        print("healthz ok", file=sys.stderr)

        if args.no_browser:
            print("no-browser mode: Ctrl-C to stop", file=sys.stderr)
            _wait_forever()
            return 0

        browser = find_app_browser()
        if browser is None:
            # Still useful, just not a window without chrome. Saying so is the
            # point: silently opening a tab makes a missing Chrome look like a
            # launcher bug.
            print(
                "no Chromium-family browser found; opening a normal browser tab",
                file=sys.stderr,
            )
            webbrowser.open(url)
            _wait_forever()
            return 0

        print(f"opening app window via {browser.name}", file=sys.stderr)
        browser_process = open_app_window(browser, url, args.profile)
        # Closing the window is how a desktop app is quit, so the browser
        # process exiting has to end the run -- otherwise the service would
        # linger with nothing attached to it.
        browser_process.wait()
        print("app window closed", file=sys.stderr)
        return 0
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 0
    finally:
        if browser_process is not None and browser_process.poll() is None:
            browser_process.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                browser_process.wait(timeout=5)
        # shutdown() before server_close(): closing the socket out from under
        # a serve_forever() still in its poll loop is how you get a stray
        # exception on the way out of an otherwise clean quit.
        server.shutdown()
        server.server_close()
        print("service stopped", file=sys.stderr)


def _wait_forever() -> None:
    """Block until Ctrl-C. ``signal.pause`` is POSIX-only, so use an Event:
    a plain ``while True: sleep`` swallows SIGINT timing on Windows."""
    threading.Event().wait()


if __name__ == "__main__":
    raise SystemExit(main())
