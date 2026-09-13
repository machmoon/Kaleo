//! Open-source wake word via [livekit-wakeword](https://crates.io/crates/livekit-wakeword)
//! (Apache-2.0, openWakeWord lineage: melspectrogram -> speech embedding ->
//! per-word classifier over the last 16 embeddings).
//!
//! The classifier is a local ONNX file (`KALEO_WAKE_ONNX`, the shipped
//! `resources/wake/hey_ada.onnx`). Command transcription after a hit still
//! uses `/transcribe`. The streaming and trigger policy live in `spotter.rs`.

use super::spotter::{Scorer, Trigger, WindowSpotter, SAMPLE_RATE};
use anyhow::{bail, Context, Result};
use livekit_wakeword::wakeword::WakeWordModel;
use std::path::{Path, PathBuf};

pub struct LivekitScorer {
    model: WakeWordModel,
}

impl Scorer for LivekitScorer {
    fn score(&mut self, window: &[i16]) -> Result<f32> {
        let scores = self
            .model
            .predict(window)
            .context("livekit-wakeword predict failed")?;
        Ok(scores.into_values().fold(0.0, f32::max))
    }
}

pub type LivekitSpotter = WindowSpotter<LivekitScorer>;

pub fn open_paths(paths: &[PathBuf], trigger: Trigger) -> Result<LivekitSpotter> {
    let Some(first) = paths.first() else {
        bail!("no wake ONNX path");
    };
    for p in paths {
        if !p.is_file() {
            bail!("wake ONNX not found: {}", p.display());
        }
    }
    let model = WakeWordModel::new(paths, SAMPLE_RATE)
        .context("livekit-wakeword failed to load classifiers")?;
    Ok(WindowSpotter::new(
        LivekitScorer { model },
        trigger,
        phrase_from_path(first),
    ))
}

fn phrase_from_path(path: &Path) -> String {
    path.file_stem()
        .and_then(|s| s.to_str())
        .unwrap_or("ada")
        .replace(['_', '-'], " ")
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::wake::mic::{to_i16, Resampler};
    use crate::wake::spotter::Spotter;
    use std::process::Command;

    #[test]
    fn open_refuses_a_missing_onnx() {
        let err = match open_paths(
            &[PathBuf::from("/tmp/kaleo-no-such-wake.onnx")],
            Trigger::new(0.7, 1, 0),
        ) {
            Ok(_) => panic!("expected missing onnx to fail"),
            Err(e) => e.to_string(),
        };
        assert!(err.contains("not found"));
    }

    fn shipped_model() -> PathBuf {
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("resources/wake/hey_ada.onnx")
    }

    /// `say` a phrase to a 48 kHz WAV so the test exercises the same
    /// resampling path the microphone does. `None` off macOS.
    fn synthesise(phrase: &str, voice: &str, dir: &Path) -> Option<Vec<f32>> {
        let aiff = dir.join("clip.aiff");
        let wav = dir.join("clip.wav");
        let ok = Command::new("say")
            .args(["-v", voice, "-o"])
            .arg(&aiff)
            .arg(phrase)
            .status()
            .ok()?
            .success()
            && Command::new("afconvert")
                .args(["-f", "WAVE", "-d", "LEI16@48000", "-c", "1"])
                .arg(&aiff)
                .arg(&wav)
                .status()
                .ok()?
                .success();
        if !ok {
            return None;
        }
        let mut r = hound::WavReader::open(&wav).ok()?;
        Some(r.samples::<i16>().map(|s| s.unwrap() as f32 / 32768.0).collect())
    }

    /// Stream a clip the way `run_loop` does: 1 s of room tone, the clip,
    /// 1.5 s of room tone, in 512-frame 48 kHz callbacks, polling after each
    /// callback. Returns the hit, if any.
    fn stream(spotter: &mut dyn Spotter, clip: &[f32]) -> Option<f32> {
        let mut audio = vec![0.0_f32; 48_000];
        audio.extend_from_slice(clip);
        audio.extend(vec![0.0_f32; 72_000]);
        let mut seed: u32 = 7;
        for s in audio.iter_mut() {
            seed = seed.wrapping_mul(1_664_525).wrapping_add(1_013_904_223);
            *s += ((seed >> 8) as f32 / 16_777_216.0 - 0.5) * 0.004;
        }
        let mut rs = Resampler::new(48_000, 16_000);
        for chunk in audio.chunks(512) {
            spotter.push(&to_i16(&rs.process(chunk)));
            if let Some(hit) = spotter.poll().unwrap() {
                return Some(hit.score);
            }
        }
        None
    }

    /// End to end on the shipped model: a synthesised "Hey Ada" fires and an
    /// unrelated command does not. Gated like the ngspice tests: skips
    /// unless `say`, `afconvert` and the model are all present.
    #[test]
    fn shipped_model_fires_on_hey_ada_through_the_streaming_path() {
        let model = shipped_model();
        let dir = std::env::temp_dir().join(format!("kaleo-wake-test-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let Some(pos) = synthesise("Hey Ada", "Samantha", &dir) else {
            eprintln!("skipping: no `say`/`afconvert` on this machine");
            return;
        };
        if !model.is_file() {
            eprintln!("skipping: {} is missing", model.display());
            return;
        }
        let neg = synthesise("okay let's route it again", "Samantha", &dir).unwrap();
        let _ = std::fs::remove_dir_all(&dir);

        let mut spotter = open_paths(&[model.clone()], Trigger::new(0.7, 1, 0)).unwrap();
        let hit = stream(&mut spotter, &pos);
        assert!(hit.is_some(), "Hey Ada did not fire");

        let mut spotter = open_paths(&[model], Trigger::new(0.7, 1, 0)).unwrap();
        assert_eq!(stream(&mut spotter, &neg), None, "an unrelated sentence fired");
    }
}
