//! Resolve the on-device wake classifier without logging secrets.
//!
//! Primary path: open-source **livekit-wakeword** ONNX (`KALEO_WAKE_ONNX`).
//! No Picovoice AccessKey. Command STT after a hit stays Google `/transcribe`.
//! ElevenLabs is TTS-only elsewhere in the app.

use serde::Serialize;
use std::env;
use std::fs;
use std::path::{Path, PathBuf};

const ONNX_VAR: &str = "KALEO_WAKE_ONNX";
const ONNX_DIR_VAR: &str = "KALEO_WAKE_MODEL_DIR";
const THRESHOLD_VAR: &str = "KALEO_WAKE_THRESHOLD";
const MOCK_VAR: &str = "KALEO_WAKE_MOCK";
const TRIGGER_VAR: &str = "KALEO_WAKE_TRIGGER_LEVEL";

/// Event name the overlay already listens for (`app/src/lib/local-wake.ts`).
pub const WAKE_EVENT: &str = "ada-wake";

#[derive(Debug, Clone)]
pub struct WakeConfig {
    pub onnx_paths: Vec<PathBuf>,
    pub threshold: f32,
    /// Consecutive scores at or above `threshold` before a hit: wyoming's
    /// `--trigger-level` / openWakeWord's `patience`. Both default to 1.
    pub trigger_level: usize,
    pub mock: bool,
}

#[derive(Debug, Clone, Serialize)]
pub struct WakeStatus {
    pub available: bool,
    pub listening: bool,
    pub backend: String,
    pub reason: String,
    pub platform: String,
    pub has_access_key: bool,
    pub keyword_count: usize,
}

impl WakeConfig {
    pub fn resolve(extra_roots: &[PathBuf]) -> Self {
        hydrate_env();
        let mock = truthy(&env_string(MOCK_VAR));
        let roots = search_roots(extra_roots);
        let onnx_paths = collect_onnx(&roots);
        // 0.7, not 0.5, and the difference is measured. On the shipped
        // `hey_ada` classifier (2026-09-07, 180 held-out-voice utterances and
        // 1.42 h of held-out speech): 0.5 gives recall 0.917 at 1.40 false
        // accepts/hour and lets "hey Aidan" through; 0.7 gives recall 0.867 at
        // **zero** per hour. A wake word that fires at someone else's name is
        // worse than one you occasionally repeat. The threshold used to live
        // only in a gitignored .env, so anyone cloning ran at the looser value
        // without knowing it.
        let threshold = env_string(THRESHOLD_VAR)
            .and_then(|s| s.parse::<f32>().ok())
            .filter(|t| *t > 0.0 && *t <= 1.0)
            .unwrap_or(0.7);
        let trigger_level = env_string(TRIGGER_VAR)
            .and_then(|s| s.parse::<usize>().ok())
            .filter(|n| (1..=10).contains(n))
            .unwrap_or(1);
        Self {
            onnx_paths,
            threshold,
            trigger_level,
            mock,
        }
    }

    pub fn livekit_ready(&self) -> bool {
        !self.onnx_paths.is_empty()
    }

    pub fn status(&self, listening: bool) -> WakeStatus {
        if self.mock {
            return WakeStatus {
                available: true,
                listening,
                backend: "mock".into(),
                reason: "KALEO_WAKE_MOCK is set — ⌥-click the ear to simulate Hey Ada".into(),
                platform: platform_name().into(),
                has_access_key: false,
                keyword_count: 0,
            };
        }
        if cfg!(not(target_os = "macos")) {
            return WakeStatus {
                available: false,
                listening: false,
                backend: "none".into(),
                reason: "native Hey Ada is macOS-first; use the mic button to dictate".into(),
                platform: platform_name().into(),
                has_access_key: false,
                keyword_count: self.onnx_paths.len(),
            };
        }
        if self.livekit_ready() {
            return WakeStatus {
                available: true,
                listening,
                backend: "livekit".into(),
                reason: format!(
                    "on-device openWakeWord (livekit) ready — {}",
                    self.onnx_paths
                        .iter()
                        .filter_map(|p| p.file_name()?.to_str())
                        .collect::<Vec<_>>()
                        .join(", ")
                ),
                platform: platform_name().into(),
                has_access_key: false,
                keyword_count: self.onnx_paths.len(),
            };
        }
        WakeStatus {
            available: false,
            listening: false,
            backend: "none".into(),
            reason: self.missing_reason(),
            platform: platform_name().into(),
            has_access_key: false,
            keyword_count: 0,
        }
    }

    fn missing_reason(&self) -> String {
        "on-device wake is off (set KALEO_WAKE_ONNX to a livekit/openWakeWord .onnx, \
         or KALEO_WAKE_MOCK=1) — ear falls back to one-shot listen / PTT"
            .into()
    }
}

pub fn phrase_from_path(path: &Path) -> String {
    let stem = path
        .file_stem()
        .and_then(|s| s.to_str())
        .unwrap_or("ada");
    stem.replace('_', " ").replace('-', " ").trim().to_string()
}

fn collect_onnx(roots: &[PathBuf]) -> Vec<PathBuf> {
    let mut out = Vec::new();
    if let Some(p) = env_path(ONNX_VAR) {
        if p.is_file() {
            out.push(p);
        } else if p.is_dir() {
            push_onnx_dir(&p, &mut out);
        }
    }
    if let Some(dir) = env_path(ONNX_DIR_VAR) {
        push_onnx_dir(&dir, &mut out);
    }
    for root in roots {
        for name in ["hey_ada.onnx", "ada.onnx", "hey_livekit.onnx"] {
            let p = root.join(name);
            if p.is_file() && !out.contains(&p) {
                out.push(p);
            }
        }
        push_onnx_dir(root, &mut out);
    }
    out
}

fn push_onnx_dir(dir: &Path, out: &mut Vec<PathBuf>) {
    let Ok(entries) = fs::read_dir(dir) else {
        return;
    };
    for entry in entries.flatten() {
        let p = entry.path();
        if p.extension().and_then(|e| e.to_str()) == Some("onnx") && !out.contains(&p) {
            out.push(p);
        }
    }
}

fn search_roots(extra: &[PathBuf]) -> Vec<PathBuf> {
    let mut roots = Vec::new();
    roots.extend(extra.iter().cloned());
    if let Some(home) = env::var_os("HOME") {
        roots.push(PathBuf::from(home).join(".config/kaleo/wake"));
    }
    if let Ok(cwd) = env::current_dir() {
        roots.push(cwd.join("resources/wake"));
        roots.push(cwd.join("wake"));
        roots.push(cwd.join("src-tauri/resources/wake"));
        if let Some(parent) = cwd.parent() {
            roots.push(parent.join("src-tauri/resources/wake"));
            roots.push(parent.join("resources/wake"));
        }
    }
    roots
}

fn platform_name() -> &'static str {
    if cfg!(target_os = "macos") {
        "macos"
    } else if cfg!(target_os = "windows") {
        "windows"
    } else if cfg!(target_os = "linux") {
        "linux"
    } else {
        "unknown"
    }
}

fn env_string(key: &str) -> Option<String> {
    env::var(key).ok().and_then(|t| {
        let t = t.trim().to_string();
        if t.is_empty() {
            None
        } else {
            Some(t)
        }
    })
}

fn env_path(key: &str) -> Option<PathBuf> {
    env_string(key).map(PathBuf::from)
}

fn truthy(value: &Option<String>) -> bool {
    matches!(
        value.as_deref().map(|s| s.trim().to_ascii_lowercase()),
        Some(s) if s == "1" || s == "true" || s == "yes"
    )
}

/// Load wake vars from a nearby `.env` if the process does not already have them.
pub fn hydrate_env() {
    for candidate in dotenv_candidates() {
        if let Ok(text) = fs::read_to_string(&candidate) {
            apply_dotenv(&text);
            return;
        }
    }
}

fn dotenv_candidates() -> Vec<PathBuf> {
    let mut out = Vec::new();
    if let Ok(explicit) = env::var("KALEO_DOTENV_PATH") {
        out.push(PathBuf::from(explicit));
    }
    if let Ok(mut dir) = env::current_dir() {
        for _ in 0..5 {
            out.push(dir.join(".env"));
            if !dir.pop() {
                break;
            }
        }
    }
    out
}

fn apply_dotenv(text: &str) {
    for raw in text.lines() {
        let line = raw.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let Some((key, value)) = line.split_once('=') else {
            continue;
        };
        let key = key.trim();
        if !(key.starts_with("KALEO_WAKE") || key == "KALEO_DOTENV_PATH") {
            continue;
        }
        if env::var_os(key).is_some() {
            continue;
        }
        let value = value.trim().trim_matches('"').trim_matches('\'');
        // SAFETY: process-local env for wake config only.
        unsafe { env::set_var(key, value) };
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex;

    static LOCK: Mutex<()> = Mutex::new(());

    #[test]
    fn mock_flag_makes_wake_available_without_onnx() {
        let _g = LOCK.lock().unwrap();
        // SAFETY: test-only env mutation under mutex.
        unsafe {
            env::set_var(MOCK_VAR, "1");
            env::remove_var(ONNX_VAR);
        }
        let cfg = WakeConfig {
            onnx_paths: vec![],
            threshold: 0.5,
            trigger_level: 1,
            mock: true,
        };
        let status = cfg.status(false);
        assert!(status.available);
        assert_eq!(status.backend, "mock");
        unsafe {
            env::remove_var(MOCK_VAR);
        }
    }

    #[test]
    fn livekit_ready_needs_an_onnx_file() {
        let cfg = WakeConfig {
            onnx_paths: vec![],
            threshold: 0.5,
            trigger_level: 1,
            mock: false,
        };
        assert!(!cfg.livekit_ready());
        let cfg = WakeConfig {
            onnx_paths: vec![PathBuf::from("/tmp/hey_ada.onnx")],
            threshold: 0.5,
            trigger_level: 1,
            mock: false,
        };
        assert!(cfg.livekit_ready());
    }

    #[test]
    fn phrase_from_onnx_stem() {
        assert_eq!(phrase_from_path(Path::new("hey_ada.onnx")), "hey ada");
    }
}
