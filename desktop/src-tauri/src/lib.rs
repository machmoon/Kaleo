use serde::Deserialize;
use std::env;
use std::ffi::OsString;
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::thread;
use std::time::Duration;
use tauri::{Manager, RunEvent, WebviewUrl, WebviewWindowBuilder};
use tauri_plugin_global_shortcut::{GlobalShortcutExt, ShortcutState};
use url::Url;

#[derive(Deserialize)]
struct ReadyMessage {
    event: String,
    url: String,
}

/// Parse the sidecar's single startup record without widening its authority.
///
/// The URL becomes a native HTTP capability in the webview.  Accepting a
/// lookalike host, a path, or an implicit port here would turn untrusted child
/// output into access outside the one loopback listener we launched.
pub fn parse_ready_line(line: &str) -> Result<Url, String> {
    let message: ReadyMessage =
        serde_json::from_str(line).map_err(|error| format!("invalid readiness JSON: {error}"))?;
    if message.event != "ready" {
        return Err("sidecar did not send a ready event".into());
    }

    let url =
        Url::parse(&message.url).map_err(|error| format!("invalid readiness URL: {error}"))?;
    let canonical = url.scheme() == "http"
        && url.host_str() == Some("127.0.0.1")
        && url.port().is_some()
        && url.username().is_empty()
        && url.password().is_none()
        && url.path() == "/"
        && url.query().is_none()
        && url.fragment().is_none();
    if !canonical {
        return Err("sidecar URL must be a canonical loopback origin with an explicit port".into());
    }
    Ok(url)
}

struct SidecarProcess(Mutex<Option<Child>>);

fn repository_root() -> anyhow::Result<PathBuf> {
    let candidate = env::var_os("ADA_REPO_ROOT")
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../.."));
    let root = candidate
        .canonicalize()
        .map_err(|error| anyhow::anyhow!("could not resolve Ada repository root: {error}"))?;
    if !root.join("desktop/sidecar.py").is_file() {
        anyhow::bail!(
            "{} is not an Ada checkout (desktop/sidecar.py is missing)",
            root.display()
        );
    }
    Ok(root)
}

fn python_interpreter(root: &Path) -> anyhow::Result<OsString> {
    if let Some(value) = env::var_os("ADA_PYTHON").filter(|value| !value.is_empty()) {
        return Ok(value);
    }
    for relative in [".venv/bin/python", ".venv/Scripts/python.exe"] {
        let candidate = root.join(relative);
        if candidate.is_file() {
            return Ok(candidate.into_os_string());
        }
    }
    anyhow::bail!("Ada's Python environment is missing; run ./scripts/install.sh or set ADA_PYTHON")
}

fn spawn_sidecar(root: &Path) -> anyhow::Result<(Child, Url)> {
    let python = python_interpreter(root)?;
    let mut child = Command::new(python)
        .args(["-m", "desktop.sidecar"])
        .current_dir(root)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit())
        .spawn()
        .map_err(|error| anyhow::anyhow!("could not start Ada's Python sidecar: {error}"))?;

    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| anyhow::anyhow!("desktop sidecar stdout was not piped"))?;
    let mut reader = BufReader::new(stdout);
    let mut ready = String::new();
    if reader.read_line(&mut ready)? == 0 {
        let _ = child.wait();
        anyhow::bail!("desktop sidecar exited before announcing readiness");
    }
    let url = parse_ready_line(ready.trim()).map_err(anyhow::Error::msg)?;

    // Keep draining diagnostic output so a future sidecar log cannot fill its
    // pipe and block the service. Readiness is the only protocol record.
    thread::spawn(move || {
        for line in reader.lines().map_while(Result::ok) {
            eprintln!("[ada-sidecar] {line}");
        }
    });
    Ok((child, url))
}

fn stop_sidecar(child: &mut Child) {
    // Closing the writer is the sidecar's graceful shutdown signal.
    drop(child.stdin.take());
    for _ in 0..40 {
        match child.try_wait() {
            Ok(Some(_)) => return,
            Ok(None) => thread::sleep(Duration::from_millis(50)),
            Err(_) => break,
        }
    }
    let _ = child.kill();
    let _ = child.wait();
}

fn stop_managed_sidecar(app: &tauri::AppHandle) {
    let Some(state) = app.try_state::<SidecarProcess>() else {
        return;
    };
    let Ok(mut guard) = state.0.lock() else {
        return;
    };
    if let Some(mut child) = guard.take() {
        stop_sidecar(&mut child);
    }
}

/// Start the native host, its loopback Python service, and the shared Svelte UI.
pub fn run() {
    let app = tauri::Builder::default()
        // Dialog-selected paths are added to the fs plugin's runtime scope,
        // so fs must be managed before the dialog plugin initializes.
        .plugin(tauri_plugin_fs::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_http::init())
        .plugin(
            tauri_plugin_global_shortcut::Builder::new()
                .with_handler(|app, _shortcut, event| {
                    if event.state() != ShortcutState::Pressed {
                        return;
                    }
                    let Some(window) = app.get_webview_window("main") else {
                        return;
                    };
                    if window.is_visible().unwrap_or(false) {
                        let _ = window.hide();
                    } else {
                        let _ = window.show();
                        let _ = window.set_focus();
                    }
                })
                .build(),
        )
        .setup(|app| {
            let root = repository_root()?;
            let (child, base_url) = spawn_sidecar(&root)?;
            eprintln!("Ada desktop service ready on {base_url}");
            app.manage(SidecarProcess(Mutex::new(Some(child))));

            let encoded_url = serde_json::to_string(base_url.as_str())?;
            let initialization_script = format!(
                "Object.defineProperty(globalThis, '__SILKSCREEN_BASE__', {{ value: {encoded_url}, writable: false, configurable: false }});"
            );
            WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
                .title("Ada")
                .inner_size(1440.0, 900.0)
                .min_inner_size(960.0, 640.0)
                .content_protected(false)
                .visible(true)
                .focused(true)
                .center()
                .initialization_script(initialization_script)
                .build()?;
            if let Err(error) = app
                .global_shortcut()
                .register("CommandOrControl+Shift+K")
            {
                eprintln!("could not register the global shortcut: {error}");
            }
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("failed to build Ada desktop application");

    app.run(|app_handle, event| {
        if matches!(event, RunEvent::ExitRequested { .. }) {
            stop_managed_sidecar(app_handle);
        }
    });
}

#[cfg(test)]
mod tests {
    use super::parse_ready_line;

    #[test]
    fn accepts_the_sidecars_canonical_loopback_readiness_record() {
        let url = parse_ready_line(r#"{"event":"ready","url":"http://127.0.0.1:43123"}"#)
            .expect("canonical readiness record");

        assert_eq!(url.as_str(), "http://127.0.0.1:43123/");
    }

    #[test]
    fn rejects_non_loopback_or_ambiguous_readiness_origins() {
        for origin in [
            "https://example.com",
            "http://127.0.0.1.evil.test:43123",
            "http://0.0.0.0:43123",
            "http://[::1]:43123",
            "http://127.0.0.1",
        ] {
            let line = format!(r#"{{"event":"ready","url":"{origin}"}}"#);
            assert!(parse_ready_line(&line).is_err(), "accepted {origin}");
        }
    }

    #[test]
    fn rejects_non_ready_messages_and_urls_with_extra_components() {
        for line in [
            r#"{"event":"log","url":"http://127.0.0.1:43123"}"#,
            r#"{"event":"ready","url":"http://127.0.0.1:43123/api"}"#,
            r#"{"event":"ready","url":"http://127.0.0.1:43123?next=evil"}"#,
            "not json",
        ] {
            assert!(parse_ready_line(line).is_err(), "accepted {line}");
        }
    }
}
