from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TAURI = ROOT / "desktop" / "src-tauri"


def test_tauri_shell_embeds_the_existing_svelte_bundle_with_a_real_csp():
    config = json.loads((TAURI / "tauri.conf.json").read_text())
    manifest = (TAURI / "Cargo.toml").read_text()

    assert config["productName"] == "Ada"
    assert config["build"]["frontendDist"] == "../../frontend/dist"
    assert config["build"]["beforeBuildCommand"] == (
        "npm --prefix ../../frontend run build"
    )
    assert "devUrl" not in config["build"]
    assert "beforeDevCommand" not in config["build"]
    assert config["app"]["windows"] == []
    assert config["app"]["security"]["csp"]
    assert config["bundle"]["active"] is False
    assert "macOSPrivateApi" not in config["app"]
    assert "macos-private-api" not in manifest


def test_tauri_http_scope_is_only_the_canonical_ipv4_loopback_origin():
    capability = json.loads(
        (TAURI / "capabilities" / "default.json").read_text()
    )
    http = next(
        permission
        for permission in capability["permissions"]
        if isinstance(permission, dict)
        and permission.get("identifier") == "http:default"
    )

    assert http["allow"] == [
        {"url": "http://127.0.0.1:*"},
        {"url": "http://127.0.0.1:*/*"},
    ]


def test_native_shell_stays_minimal_and_parent_owns_the_python_sidecar():
    manifest = (TAURI / "Cargo.toml").read_text()
    source = (TAURI / "src" / "lib.rs").read_text()

    assert 'tauri-plugin-http = "2"' in manifest
    assert 'tauri-plugin-dialog = "2"' in manifest
    assert 'tauri-plugin-fs = "2"' in manifest
    assert 'tauri-plugin-global-shortcut = "2"' in manifest
    for inherited_surface in [
        "autostart",
        "cpal",
        "nspanel",
        "updater",
        "speaker",
    ]:
        assert inherited_surface not in manifest.lower()

    assert '"desktop.sidecar"' in source
    assert "__SILKSCREEN_BASE__" in source
    assert "ExitRequested" in source
    assert "content_protected(false)" in source
    assert source.index("tauri_plugin_fs::init") < source.index(
        "tauri_plugin_dialog::init"
    )
    assert "CommandOrControl+Shift+K" in source
    assert "ShortcutState::Pressed" in source
    assert "could not register the global shortcut" in source


def test_native_save_permissions_are_present_without_broad_filesystem_scope():
    capability = json.loads(
        (TAURI / "capabilities" / "default.json").read_text()
    )
    permissions = capability["permissions"]

    assert "dialog:allow-save" in permissions
    assert "fs:allow-write-text-file" in permissions
    assert not any(
        isinstance(permission, dict)
        and str(permission.get("identifier", "")).startswith("fs:")
        for permission in permissions
    )
