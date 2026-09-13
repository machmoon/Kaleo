"""``service/envfiles.py``: the files Setup writes, and the demo boundary.

Everything here is about what must never happen: a credential briefly
world-readable, a file overriding the environment, a demo file becoming a
credential, a write following a symlink out of ``~/.kaleo``.
"""

from __future__ import annotations

import os
import stat

import pytest

from service import envfiles


def mode_of(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX modes")


def test_kaleo_home_is_read_at_call_time(monkeypatch, tmp_path):
    monkeypatch.setenv("KALEO_HOME", str(tmp_path / "a"))
    assert envfiles.kaleo_home() == tmp_path / "a"
    monkeypatch.setenv("KALEO_HOME", str(tmp_path / "b"))
    assert envfiles.kaleo_home() == tmp_path / "b"
    assert envfiles.env_path("google") == tmp_path / "b" / "google.env"
    assert (
        envfiles.env_path("google", demo=True) == tmp_path / "b" / "demo" / "google.env"
    )


def test_billing_live_path_honours_the_old_override(monkeypatch, tmp_path):
    monkeypatch.setenv("KALEO_BILLING_ENV_PATH", str(tmp_path / "elsewhere.env"))
    assert envfiles.env_path("billing") == tmp_path / "elsewhere.env"
    # The demo path never moves with it: demo state stays under demo/.
    assert envfiles.env_path("billing", demo=True).parent == envfiles.demo_dir()


@posix_only
def test_save_creates_0600_under_0700_and_reads_back():
    path = envfiles.env_path("google")
    envfiles.save_env(
        path,
        {
            "GOOGLEAPPS_CLIENT_ID": "x.apps.googleusercontent.com",
            "GOOGLEAPPS_CLIENT_SECRET": "s",
        },
        allow=envfiles.FILES["google"],
    )
    assert mode_of(path) == 0o600
    assert mode_of(path.parent) == 0o700
    assert envfiles.load_env(path) == {
        "GOOGLEAPPS_CLIENT_ID": "x.apps.googleusercontent.com",
        "GOOGLEAPPS_CLIENT_SECRET": "s",
    }
    assert envfiles.is_private(path)
    assert envfiles.mode_hint(path) is None


@posix_only
def test_save_is_atomic_and_leaves_no_temp_file(monkeypatch):
    """The temp file is created 0600 by mkstemp and swapped in by replace, so
    a reader never sees a partial file; and nothing is left behind."""
    path = envfiles.env_path("microsoft")
    envfiles.save_env(path, {"TEAMS_APP_ID": "one"}, allow=envfiles.FILES["microsoft"])
    seen: list[int] = []
    real_replace = os.replace

    def spy(src, dst):
        seen.append(mode_of(src))
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    envfiles.save_env(path, {"TEAMS_APP_ID": "two"}, allow=envfiles.FILES["microsoft"])
    assert seen == [0o600]
    assert sorted(p.name for p in path.parent.iterdir()) == ["microsoft.env"]
    assert envfiles.load_env(path) == {"TEAMS_APP_ID": "two"}


def test_save_refuses_keys_outside_the_allowlist():
    with pytest.raises(envfiles.EnvFileError, match="PATH"):
        envfiles.save_env(
            envfiles.env_path("google"),
            {"PATH": "/tmp"},
            allow=envfiles.FILES["google"],
        )
    assert not envfiles.env_path("google").exists()


def test_save_refuses_a_line_break_in_a_value():
    with pytest.raises(envfiles.EnvFileError, match="line break"):
        envfiles.save_env(
            envfiles.env_path("google"),
            {"GOOGLEAPPS_CLIENT_ID": "a\nGOOGLEAPPS_CLIENT_SECRET=injected"},
            allow=envfiles.FILES["google"],
        )


def test_apply_env_never_overrides_the_environment():
    environ = {"STRIPE_API_KEY": "from-the-shell"}
    applied = envfiles.apply_env(
        {"STRIPE_API_KEY": "from-the-file", "STRIPE_PRICE_ID": "price_1"},
        allow=envfiles.FILES["billing"],
        environ=environ,
    )
    assert applied == 1
    assert environ == {"STRIPE_API_KEY": "from-the-shell", "STRIPE_PRICE_ID": "price_1"}
    active, source = envfiles.activation(
        {"STRIPE_API_KEY": "from-the-file"}, environ=environ
    )
    assert (active, source) == (False, "environment")
    assert envfiles.activation({"STRIPE_PRICE_ID": "price_1"}, environ=environ) == (
        True,
        "file",
    )


def test_apply_env_drops_keys_outside_the_allowlist():
    environ: dict[str, str] = {}
    envfiles.apply_env(
        {"SILKSCREEN_ACCESS_TOKEN": "x", "TEAMS_APP_ID": "a"},
        allow=envfiles.FILES["microsoft"],
        environ=environ,
    )
    assert environ == {"TEAMS_APP_ID": "a"}


def test_demo_save_writes_header_and_marker_and_is_never_applied():
    path = envfiles.env_path("google", demo=True)
    envfiles.save_env(
        path, {"GOOGLE_SIGNED_IN": "1"}, allow={"GOOGLE_SIGNED_IN"}, demo=True
    )
    text = path.read_text()
    assert text.splitlines()[0] == envfiles.DEMO_HEADER
    assert "KALEO_DEMO=1" in text
    values = envfiles.load_env(path, demo=True)
    environ: dict[str, str] = {}
    assert envfiles.apply_env(values, allow={"GOOGLE_SIGNED_IN"}, environ=environ) == 0
    assert environ == {}


def test_live_read_refuses_a_demo_marker_regardless_of_path():
    """The marker, not the directory, is what the live side refuses."""
    path = envfiles.env_path("google")  # a LIVE path
    path.parent.mkdir(parents=True)
    path.write_text("KALEO_DEMO=1\nGOOGLEAPPS_CLIENT_ID=x\n")
    with pytest.raises(envfiles.DemoFileError):
        envfiles.load_env(path)
    # And the startup loader counts it as nothing rather than importing it.
    environ: dict[str, str] = {}
    assert envfiles.apply_saved_env(environ)["google"] == 0
    assert environ == {}


def test_demo_read_refuses_a_file_without_the_marker():
    path = envfiles.env_path("google", demo=True)
    path.parent.mkdir(parents=True)
    path.write_text("GOOGLEAPPS_CLIENT_SECRET=real\n")
    with pytest.raises(envfiles.DemoFileError):
        envfiles.load_env(path, demo=True)


def test_live_save_refuses_a_path_under_demo_and_demo_save_refuses_outside():
    with pytest.raises(envfiles.EnvFileError, match="demo"):
        envfiles.save_env(
            envfiles.env_path("google", demo=True),
            {"GOOGLEAPPS_CLIENT_ID": "x"},
            allow=envfiles.FILES["google"],
        )
    with pytest.raises(envfiles.EnvFileError, match="demo"):
        envfiles.save_env(
            envfiles.env_path("google"), {"X": "1"}, allow={"X"}, demo=True
        )
    with pytest.raises(envfiles.EnvFileError, match="KALEO_DEMO"):
        envfiles.save_env(
            envfiles.env_path("google"),
            {"KALEO_DEMO": "1"},
            allow={"KALEO_DEMO"},
        )


def test_apply_saved_env_never_reads_the_demo_directory():
    demo = envfiles.env_path("google", demo=True)
    envfiles.save_env(
        demo, {"GOOGLE_SIGNED_IN": "1"}, allow={"GOOGLE_SIGNED_IN"}, demo=True
    )
    live = envfiles.env_path("microsoft")
    envfiles.save_env(live, {"TEAMS_APP_ID": "a"}, allow=envfiles.FILES["microsoft"])
    environ: dict[str, str] = {}
    counts = envfiles.apply_saved_env(environ)
    assert counts == {"billing": 0, "google": 0, "microsoft": 1}
    assert environ == {"TEAMS_APP_ID": "a"}


@posix_only
def test_symlinked_target_and_home_are_refused(tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    home = envfiles.kaleo_home()
    home.parent.mkdir(parents=True, exist_ok=True)
    home.symlink_to(elsewhere)
    with pytest.raises(envfiles.EnvFileError, match="symlink"):
        envfiles.save_env(
            envfiles.env_path("google"),
            {"GOOGLEAPPS_CLIENT_ID": "x"},
            allow=envfiles.FILES["google"],
        )
    assert list(elsewhere.iterdir()) == []

    monkeypatch.setenv("KALEO_HOME", str(tmp_path / "home2"))
    real_home = envfiles.kaleo_home()
    real_home.mkdir(mode=0o700)
    target = tmp_path / "victim.env"
    target.write_text("")
    (real_home / "google.env").symlink_to(target)
    with pytest.raises(envfiles.EnvFileError, match="symlink"):
        envfiles.save_env(
            envfiles.env_path("google"),
            {"GOOGLEAPPS_CLIENT_ID": "x"},
            allow=envfiles.FILES["google"],
        )
    assert target.read_text() == ""
    with pytest.raises(envfiles.EnvFileError, match="symlink"):
        envfiles.load_env(envfiles.env_path("google"))
    with pytest.raises(envfiles.EnvFileError, match="symlink"):
        envfiles.remove_env(envfiles.env_path("google"))
    assert target.exists()


@posix_only
def test_mode_hint_names_a_loose_file():
    path = envfiles.env_path("billing")
    envfiles.save_env(
        path, {"STRIPE_PRICE_ID": "price_1"}, allow=envfiles.FILES["billing"]
    )
    os.chmod(path, 0o644)
    hint = envfiles.mode_hint(path)
    assert hint and "not mode 0600" in hint and str(path) in hint


def test_remove_env_reports_whether_anything_was_there():
    path = envfiles.env_path("billing")
    assert envfiles.remove_env(path) is False
    envfiles.save_env(
        path, {"STRIPE_PRICE_ID": "price_1"}, allow=envfiles.FILES["billing"]
    )
    assert envfiles.remove_env(path) is True
    assert not path.exists()
