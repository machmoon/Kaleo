//! On-device "Hey Ada" — cpal owns the mic, a Spotter scores frames, Tauri
//! emits `ada-wake`. Nothing here POSTs `/transcribe` or talks to Gemini.
//!
//! Primary spotter: open-source **livekit-wakeword** (ONNX). Feature gate:
//! without `KALEO_WAKE_ONNX` (or `KALEO_WAKE_MOCK=1`), `wake_start` refuses
//! and the ear falls back to PTT. Picovoice is not used.

mod config;
mod mic;
mod oww;
mod spotter;

pub use config::{hydrate_env, WakeConfig, WakeStatus, WAKE_EVENT};
pub use spotter::{MockSpotter, Spotter};

use anyhow::{bail, Result};
use serde::Serialize;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::Duration;
use tauri::{AppHandle, Emitter, Manager, State};

#[derive(Default)]
pub struct WakeState {
    stop: Mutex<Option<Arc<AtomicBool>>>,
    join: Mutex<Option<JoinHandle<()>>>,
    /// Shared with the wake thread, which clears it when it exits for any
    /// reason — a mic that failed to open used to leave this `true`, so the
    /// ear said "listening" over a dead thread and `wake_start` refused to
    /// start a new one.
    listening: Arc<AtomicBool>,
    fire: Arc<AtomicBool>,
}

#[derive(Debug, Clone, Serialize)]
pub struct WakeHitPayload {
    pub phrase: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub utterance: Option<String>,
}

fn resource_roots(app: &AppHandle) -> Vec<PathBuf> {
    let mut roots = Vec::new();
    if let Ok(cwd) = std::env::current_dir() {
        roots.push(cwd.join("wake"));
        roots.push(cwd.join("resources").join("wake"));
        roots.push(cwd.join("app").join("src-tauri").join("resources").join("wake"));
    }
    if let Ok(res) = app.path().resource_dir() {
        roots.push(res.join("wake"));
        roots.push(res);
    }
    roots
}

/// wyoming-openwakeword's `--refractory-seconds` default.
const REFRACTORY_SAMPLES: usize = 2 * spotter::SAMPLE_RATE as usize;
/// How often the wake thread says what it is hearing, in audio samples.
const HEARTBEAT_SAMPLES: usize = 30 * spotter::SAMPLE_RATE as usize;
/// A macOS microphone the host app has no permission for delivers exact
/// zeros rather than an error. This much digital silence says so.
const DEAD_MIC_SAMPLES: usize = 3 * spotter::SAMPLE_RATE as usize;

fn build_spotter(cfg: &WakeConfig) -> Result<Box<dyn Spotter>> {
    if !cfg.livekit_ready() {
        bail!("{}", cfg.status(false).reason);
    }
    let trigger = spotter::Trigger::new(cfg.threshold, cfg.trigger_level, REFRACTORY_SAMPLES);
    Ok(Box::new(oww::open_paths(&cfg.onnx_paths, trigger)?))
}

fn emit_hit(app: &AppHandle, hit: spotter::WakeHit) {
    eprintln!("[wake] heard \"{}\" (score {:.2})", hit.phrase, hit.score);
    let _ = app.emit(
        WAKE_EVENT,
        WakeHitPayload {
            phrase: hit.phrase,
            utterance: None,
        },
    );
}

fn run_loop(
    app: AppHandle,
    mut spotter: Box<dyn Spotter>,
    stop: Arc<AtomicBool>,
    mock: bool,
    policy: String,
) {
    // Mock mode does not need a real mic — debug trigger flips `fire`.
    if mock {
        while !stop.load(Ordering::SeqCst) {
            match spotter.poll() {
                Ok(Some(hit)) => return emit_hit(&app, hit),
                Ok(None) => thread::sleep(Duration::from_millis(50)),
                Err(err) => return eprintln!("[wake] mock spotter error: {err}"),
            }
        }
        return;
    }

    let mic = match mic::Mic::open() {
        Ok(m) => m,
        Err(err) => {
            eprintln!("[wake] microphone open failed: {err}");
            return;
        }
    };
    eprintln!(
        "[wake] listening: mic {} Hz x{}, {policy}",
        mic.sample_rate, mic.channels
    );
    let mut resampler = mic::Resampler::new(mic.sample_rate, spotter::SAMPLE_RATE);
    let mut pending: Vec<i16> = Vec::with_capacity(spotter::WINDOW);
    let mut heard = 0_usize;
    let mut since_beat = 0_usize;
    let mut peak = 0_i16;
    let mut warned_dead = false;
    while !stop.load(Ordering::SeqCst) {
        // Block briefly for audio, then drain everything queued, so a score
        // always runs on the newest contiguous window and the queue never
        // fills while one is running.
        match mic.recv_timeout(Duration::from_millis(50)) {
            Ok(Some(chunk)) => mic::ingest(&mut pending, &chunk, mic.channels, &mut resampler),
            Ok(None) => continue,
            Err(err) => {
                eprintln!("[wake] mic ended: {err}");
                break;
            }
        }
        loop {
            match mic.try_recv() {
                Ok(Some(chunk)) => mic::ingest(&mut pending, &chunk, mic.channels, &mut resampler),
                Ok(None) => break,
                Err(err) => {
                    eprintln!("[wake] mic ended: {err}");
                    return;
                }
            }
        }
        peak = pending.iter().fold(peak, |p, s| p.max(s.saturating_abs()));
        heard += pending.len();
        since_beat += pending.len();
        spotter.push(&pending);
        pending.clear();

        if !warned_dead && heard >= DEAD_MIC_SAMPLES && peak == 0 {
            warned_dead = true;
            eprintln!(
                "[wake] the microphone is delivering exact silence — on macOS that is \
                 what a denied microphone permission looks like; allow it for the app \
                 that launched Kaleo in System Settings > Privacy & Security > Microphone"
            );
        }

        match spotter.poll() {
            Ok(Some(hit)) => {
                emit_hit(&app, hit);
                stop.store(true, Ordering::SeqCst);
                return;
            }
            Ok(None) => {}
            Err(err) => {
                eprintln!("[wake] spotter error: {err}");
                stop.store(true, Ordering::SeqCst);
                return;
            }
        }

        if since_beat >= HEARTBEAT_SAMPLES {
            let (best, scored) = spotter.take_stats();
            let dropped = mic.dropped.load(Ordering::Relaxed);
            eprintln!(
                "[wake] last {}s: peak level {:.3}, {} scores, best {:.2}, dropped callbacks {}",
                since_beat / spotter::SAMPLE_RATE as usize,
                peak as f32 / 32768.0,
                scored,
                best,
                dropped
            );
            since_beat = 0;
            peak = 0;
        }
    }
}

fn stop_inner(state: &WakeState) {
    if let Some(flag) = state.stop.lock().ok().and_then(|mut g| g.take()) {
        flag.store(true, Ordering::SeqCst);
    }
    if let Some(handle) = state.join.lock().ok().and_then(|mut g| g.take()) {
        let _ = handle.join();
    }
    state.listening.store(false, Ordering::SeqCst);
}

#[tauri::command]
pub fn wake_status(app: AppHandle, state: State<'_, WakeState>) -> WakeStatus {
    let cfg = WakeConfig::resolve(&resource_roots(&app));
    let mut status = cfg.status(state.listening.load(Ordering::SeqCst));
    if cfg.mock {
        status.available = true;
        status.backend = "mock".into();
    }
    status
}

#[tauri::command]
pub fn wake_start(app: AppHandle, state: State<'_, WakeState>) -> Result<WakeStatus, String> {
    if state.listening.load(Ordering::SeqCst) {
        return Ok(wake_status(app, state));
    }
    let cfg = WakeConfig::resolve(&resource_roots(&app));
    let status = cfg.status(false);
    if !status.available && !cfg.mock {
        return Err(status.reason);
    }

    let fire = state.fire.clone();
    fire.store(false, Ordering::SeqCst);

    let spotter: Box<dyn Spotter> = if cfg.mock {
        Box::new(MockSpotter::new(fire.clone()))
    } else {
        build_spotter(&cfg).map_err(|e| e.to_string())?
    };

    let stop = Arc::new(AtomicBool::new(false));
    let stop_thread = stop.clone();
    let app_thread = app.clone();
    let mock = cfg.mock;
    let listening = state.listening.clone();
    let policy = format!(
        "threshold {:.2}, trigger level {}",
        cfg.threshold, cfg.trigger_level
    );
    // Join a thread that already ended on its own (a hit, a dead mic).
    if let Some(old) = state.join.lock().ok().and_then(|mut g| g.take()) {
        let _ = old.join();
    }
    listening.store(true, Ordering::SeqCst);
    let handle = thread::Builder::new()
        .name("ada-wake".into())
        .spawn(move || {
            run_loop(app_thread, spotter, stop_thread, mock, policy);
            listening.store(false, Ordering::SeqCst);
        })
        .map_err(|e| {
            state.listening.store(false, Ordering::SeqCst);
            e.to_string()
        })?;

    *state.stop.lock().map_err(|e| e.to_string())? = Some(stop);
    *state.join.lock().map_err(|e| e.to_string())? = Some(handle);
    Ok(cfg.status(true))
}

#[tauri::command]
pub fn wake_stop(state: State<'_, WakeState>) -> Result<(), String> {
    stop_inner(&state);
    Ok(())
}

/// Offline / demo: fire one `ada-wake` without speaking (needs mock or listening).
#[tauri::command]
pub fn wake_debug_trigger(app: AppHandle, state: State<'_, WakeState>) -> Result<(), String> {
    let cfg = WakeConfig::resolve(&resource_roots(&app));
    if cfg.mock {
        state.fire.store(true, Ordering::SeqCst);
        return Ok(());
    }
    if state.listening.load(Ordering::SeqCst) {
        let _ = app.emit(
            WAKE_EVENT,
            WakeHitPayload {
                phrase: "ada".into(),
                utterance: None,
            },
        );
        return Ok(());
    }
    Err("wake is not listening — set KALEO_WAKE_MOCK=1 or start the ear".into())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn payload_omits_utterance_so_the_page_records_the_command() {
        let payload = WakeHitPayload {
            phrase: "hey ada".into(),
            utterance: None,
        };
        let json = serde_json::to_value(&payload).unwrap();
        assert_eq!(json["phrase"], "hey ada");
        assert!(json.get("utterance").is_none());
    }

    #[test]
    fn event_name_matches_the_frontend_seam() {
        assert_eq!(WAKE_EVENT, "ada-wake");
    }

    #[test]
    fn mock_spotter_fires_once_when_triggered() {
        let fire = Arc::new(AtomicBool::new(true));
        let mut spotter = MockSpotter::new(Arc::clone(&fire));
        assert_eq!(spotter.poll().unwrap().expect("hit").phrase, "ada");
        assert!(spotter.poll().unwrap().is_none());
    }
}
