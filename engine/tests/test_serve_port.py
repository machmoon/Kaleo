"""``silkscreen serve``'s default port is a contract, not a convenience.

The repository's local convention is 8081 -- CLAUDE.md's commands block, the
frontend dev proxy and the desktop app's engine URL all expect it, because
8080 is taken on a teammate's machine. The container deliberately keeps Cloud
Run's 8080 in ``service.app`` and the ``Dockerfile``; ``serve.py`` never
reaches that path because it always hands ``make_server`` the resolved port.
These tests pin the default and the ``--port`` / ``PORT`` precedence without
binding a socket.
"""

from __future__ import annotations

import os

import pytest
from silkscreen import serve


class _FakeServer:
    def __init__(self, port: int) -> None:
        self.server_port = port
        self.closed = False

    def serve_forever(self) -> None:
        raise KeyboardInterrupt

    def server_close(self) -> None:
        self.closed = True


@pytest.fixture
def bound_ports(monkeypatch):
    """Run ``serve.main`` offline; collect the port ``make_server`` was handed."""
    ports: list[int] = []
    servers: list[_FakeServer] = []

    def make_server(port):
        ports.append(port)
        server = _FakeServer(port)
        servers.append(server)
        return server

    monkeypatch.setattr(serve, "_import_service", lambda root: make_server)
    monkeypatch.setattr(serve, "load_dotenv", lambda path: None)
    monkeypatch.setattr(serve.webbrowser, "open", lambda url: True)
    monkeypatch.delenv("PORT", raising=False)
    monkeypatch.delenv("SILKSCREEN_WEB_DIST", raising=False)
    return ports, servers


def test_default_port_is_the_repo_convention():
    assert serve.DEFAULT_PORT == 8081


def test_bare_serve_binds_the_default(bound_ports, monkeypatch):
    ports, servers = bound_ports
    assert serve.main(["--no-browser"]) == 0
    assert ports == [serve.DEFAULT_PORT]
    assert servers[0].closed
    # The resolved value is written back so anything downstream that reports
    # the port (the container's own health check reads PORT) agrees with it.
    assert os.environ["PORT"] == str(serve.DEFAULT_PORT)


def test_port_env_overrides_the_default(bound_ports, monkeypatch):
    ports, _ = bound_ports
    monkeypatch.setenv("PORT", "8090")
    assert serve.main(["--no-browser"]) == 0
    assert ports == [8090]


def test_port_flag_overrides_port_env(bound_ports, monkeypatch):
    ports, _ = bound_ports
    monkeypatch.setenv("PORT", "8090")
    assert serve.main(["--no-browser", "--port", "8099"]) == 0
    assert ports == [8099]


def test_help_names_the_default(capsys):
    with pytest.raises(SystemExit) as exc:
        serve.main(["--help"])
    assert exc.value.code == 0
    assert str(serve.DEFAULT_PORT) in capsys.readouterr().out


def _never(what):
    def fail(*_args):
        pytest.fail(what)

    return fail


def test_bare_serve_opens_the_desktop_app_not_the_browser(bound_ports, monkeypatch):
    opened: list[str] = []
    launched: list[object] = []
    monkeypatch.setattr(serve.webbrowser, "open", lambda url: opened.append(url))
    def launch(root):
        launched.append(root)
        return None, None

    monkeypatch.setattr(serve, "_launch_desktop", launch)
    assert serve.main([]) == 0
    assert len(launched) == 1
    assert opened == []


def test_web_flag_opens_the_browser_and_skips_the_desktop(bound_ports, monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr(serve.webbrowser, "open", lambda url: opened.append(url))
    monkeypatch.setattr(serve, "_launch_desktop", _never("desktop launched"))
    assert serve.main(["--web"]) == 0
    assert opened == [f"http://localhost:{serve.DEFAULT_PORT}/"]


def test_no_desktop_falls_back_to_the_browser_and_says_why(
    bound_ports, monkeypatch, capsys
):
    opened: list[str] = []
    monkeypatch.setattr(serve.webbrowser, "open", lambda url: opened.append(url))
    monkeypatch.setattr(
        serve, "_launch_desktop", lambda root: (None, "no desktop app to open")
    )
    assert serve.main([]) == 0
    assert opened == [f"http://localhost:{serve.DEFAULT_PORT}/"]
    assert "no desktop app to open" in capsys.readouterr().out


def test_no_browser_opens_nothing(bound_ports, monkeypatch):
    monkeypatch.setattr(serve.webbrowser, "open", _never("browser opened"))
    monkeypatch.setattr(serve, "_launch_desktop", _never("desktop launched"))
    assert serve.main(["--no-browser"]) == 0
