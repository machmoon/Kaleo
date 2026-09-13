"""Service-wide fixtures.

``KALEO_HOME`` is pointed at ``tmp_path`` for every test in this directory,
so nothing here can read or write the developer's real ``~/.kaleo`` -- the
Setup Assistant's files, the demo state and the Stripe consent token all
resolve their location at call time for exactly this reason. The setup
module's in-process state (demo jobs, the Microsoft stamp, the Google auth
job) is reset around each test the same way ``billing_routes`` is.
"""

from __future__ import annotations

import pytest

from service import billing_routes, setup


@pytest.fixture(autouse=True)
def _kaleo_home(tmp_path, monkeypatch):
    monkeypatch.setenv("KALEO_HOME", str(tmp_path / "kaleo"))
    monkeypatch.delenv("KALEO_SETUP_MODE", raising=False)
    monkeypatch.delenv("KALEO_BILLING_ENV_PATH", raising=False)
    setup.reset_for_tests()
    default_verify = billing_routes.verify_transport_factory
    default_ms = setup.microsoft_transport_factory
    default_stripe_demo = setup.demo_stripe_transport_factory
    default_clock = setup.clock
    yield
    setup.reset_for_tests()
    billing_routes.verify_transport_factory = default_verify
    setup.microsoft_transport_factory = default_ms
    setup.demo_stripe_transport_factory = default_stripe_demo
    setup.clock = default_clock
