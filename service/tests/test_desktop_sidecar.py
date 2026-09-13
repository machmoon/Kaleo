from __future__ import annotations

import io
import json
import queue
import socket
import subprocess
import sys
import threading
import urllib.request

from desktop import sidecar


class _FakeServer:
    server_port = 43123

    def __init__(self) -> None:
        self.shutdown_called = False
        self.close_called = False

    def shutdown(self) -> None:
        self.shutdown_called = True

    def server_close(self) -> None:
        self.close_called = True


def test_sidecar_announces_loopback_url_then_stops_on_parent_eof():
    server = _FakeServer()
    output = io.StringIO()

    code = sidecar.run(
        stdin=io.StringIO(''),
        stdout=output,
        stderr=io.StringIO(),
        start=lambda port: server,
        wait=lambda url: url == 'http://127.0.0.1:43123/healthz',
    )

    assert code == 0
    assert json.loads(output.getvalue()) == {
        'event': 'ready',
        'url': 'http://127.0.0.1:43123',
    }
    assert server.shutdown_called
    assert server.close_called


def test_sidecar_closes_server_without_announcing_when_health_never_arrives():
    server = _FakeServer()
    output = io.StringIO()
    errors = io.StringIO()

    code = sidecar.run(
        stdin=io.StringIO(''),
        stdout=output,
        stderr=errors,
        start=lambda port: server,
        wait=lambda url: False,
    )

    assert code == 1
    assert output.getvalue() == ''
    assert 'health' in errors.getvalue().lower()
    assert server.shutdown_called
    assert server.close_called


def test_sidecar_process_serves_health_and_exits_when_parent_pipe_closes():
    process = subprocess.Popen(
        [sys.executable, "-m", "desktop.sidecar"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    lines: queue.Queue[str] = queue.Queue()
    assert process.stdout is not None
    reader = threading.Thread(
        target=lambda: lines.put(process.stdout.readline()), daemon=True
    )
    reader.start()

    try:
        ready = json.loads(lines.get(timeout=30))
        assert ready["event"] == "ready"
        base_url = ready["url"]
        with urllib.request.urlopen(f"{base_url}/healthz", timeout=5) as response:
            assert json.loads(response.read())["ok"] is True

        assert process.stdin is not None
        process.stdin.close()
        assert process.wait(timeout=10) == 0

        host, port_text = base_url.removeprefix("http://").split(":")
        with socket.socket() as probe:
            probe.settimeout(1)
            assert probe.connect_ex((host, int(port_text))) != 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
