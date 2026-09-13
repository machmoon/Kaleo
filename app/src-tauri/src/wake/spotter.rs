//! The streaming half of the wake word: a sliding window, a score, a trigger.
//!
//! Shaped after the two open-source listeners that ship this model family:
//!
//! - livekit-wakeword's own listener (`src/livekit/wakeword/inference/listener.py`)
//!   keeps a deque of the last 25 frames of 1280 samples (2 s at 16 kHz) and
//!   calls the stateless `predict` on the whole window after **every** new
//!   frame, with `threshold=0.5` and a 2 s `debounce`.
//! - wyoming-openwakeword (`wyoming_openwakeword/handler.py`, the Home
//!   Assistant server) scores every 80 ms of features and fires when a score
//!   exceeds `--threshold` (default 0.5) `--trigger-level` times (default 1),
//!   then ignores the detector for `--refractory-seconds` (default 2).
//!   openWakeWord's `Model.predict` (`openwakeword/model.py`) calls the same
//!   idea `patience`: consecutive 80 ms frames at or above the threshold.
//!
//! One deliberate difference: `predict` recomputes 25 embeddings per call
//! (~0.1-0.25 s on this machine), so scoring after literally every 80 ms
//! frame cannot keep up. The window here always holds the newest contiguous
//! 2 s, and a score is taken on it whenever at least one new frame has
//! arrived since the last — as often as the CPU allows, and never on stale or
//! gapped audio. The old spotter scored every 0.5 s of *audio* no matter how
//! long a score took, which in a debug build (3 s per score) meant the queue
//! overflowed and the window was stitched from fragments.

use anyhow::Result;
use std::collections::VecDeque;

/// openWakeWord / livekit frame: 80 ms at 16 kHz.
pub const FRAME: usize = 1280;
/// livekit listener `CHUNK_FRAMES` × `FRAME_SAMPLES`: 2 s at 16 kHz.
pub const WINDOW: usize = 25 * FRAME;
pub const SAMPLE_RATE: u32 = 16_000;

/// One on-device hit. Rust does not record the command — the page starts
/// one short clip after `ada-wake`.
#[derive(Debug, Clone, PartialEq)]
pub struct WakeHit {
    pub phrase: String,
    pub score: f32,
}

pub trait Spotter: Send {
    /// Append 16 kHz mono PCM. Cheap: never runs inference.
    fn push(&mut self, pcm: &[i16]);
    /// Score the newest window if a frame arrived since the last score.
    fn poll(&mut self) -> Result<Option<WakeHit>>;
    /// Best score and scores taken since the last call (for the heartbeat log).
    fn take_stats(&mut self) -> (f32, usize) {
        (0.0, 0)
    }
}

/// Anything that turns 2 s of 16 kHz PCM into a 0..1 score. The livekit model
/// in production; a fake in tests.
pub trait Scorer: Send {
    fn score(&mut self, window: &[i16]) -> Result<f32>;
}

/// When a stream of scores becomes a detection.
///
/// `trigger_level` is openWakeWord's `patience`: that many *consecutive*
/// scores at or above `threshold`. `refractory_samples` is wyoming's
/// `--refractory-seconds`, counted in audio samples rather than wall-clock
/// time so the same audio always gives the same answer.
#[derive(Debug, Clone)]
pub struct Trigger {
    pub threshold: f32,
    pub trigger_level: usize,
    pub refractory_samples: usize,
    run: usize,
    since_fire: Option<usize>,
}

impl Trigger {
    pub fn new(threshold: f32, trigger_level: usize, refractory_samples: usize) -> Self {
        Self {
            threshold,
            trigger_level: trigger_level.max(1),
            refractory_samples,
            run: 0,
            since_fire: None,
        }
    }

    /// Advance the audio clock (called with every pushed sample count).
    pub fn advance(&mut self, samples: usize) {
        if let Some(n) = self.since_fire.as_mut() {
            *n += samples;
        }
    }

    pub fn observe(&mut self, score: f32) -> bool {
        if matches!(self.since_fire, Some(n) if n < self.refractory_samples) {
            self.run = 0;
            return false;
        }
        if score < self.threshold {
            self.run = 0;
            return false;
        }
        self.run += 1;
        if self.run < self.trigger_level {
            return false;
        }
        self.run = 0;
        self.since_fire = Some(0);
        true
    }
}

/// Sliding 2 s window over a [`Scorer`].
pub struct WindowSpotter<S: Scorer> {
    scorer: S,
    trigger: Trigger,
    phrase: String,
    window: VecDeque<i16>,
    fresh: usize,
    best: f32,
    scored: usize,
}

impl<S: Scorer> WindowSpotter<S> {
    pub fn new(scorer: S, trigger: Trigger, phrase: impl Into<String>) -> Self {
        Self {
            scorer,
            trigger,
            phrase: phrase.into(),
            window: VecDeque::with_capacity(WINDOW + FRAME),
            fresh: 0,
            best: 0.0,
            scored: 0,
        }
    }
}

impl<S: Scorer> Spotter for WindowSpotter<S> {
    fn push(&mut self, pcm: &[i16]) {
        self.window.extend(pcm.iter().copied());
        let over = self.window.len().saturating_sub(WINDOW);
        self.window.drain(..over);
        self.fresh += pcm.len();
        self.trigger.advance(pcm.len());
    }

    fn poll(&mut self) -> Result<Option<WakeHit>> {
        if self.window.len() < WINDOW || self.fresh < FRAME {
            return Ok(None);
        }
        self.fresh = 0;
        let score = self.scorer.score(self.window.make_contiguous())?;
        self.best = self.best.max(score);
        self.scored += 1;
        if self.trigger.observe(score) {
            // livekit's listener clears its buffer after a detection so the
            // same utterance cannot be scored twice.
            self.window.clear();
            return Ok(Some(WakeHit {
                phrase: self.phrase.clone(),
                score,
            }));
        }
        Ok(None)
    }

    fn take_stats(&mut self) -> (f32, usize) {
        let out = (self.best, self.scored);
        self.best = 0.0;
        self.scored = 0;
        out
    }
}

/// Offline / `KALEO_WAKE_MOCK=1` spotter. Never inspects PCM for a word —
/// `wake_debug_trigger` flips `fire`.
pub struct MockSpotter {
    fire: std::sync::Arc<std::sync::atomic::AtomicBool>,
}

impl MockSpotter {
    pub fn new(fire: std::sync::Arc<std::sync::atomic::AtomicBool>) -> Self {
        Self { fire }
    }
}

impl Spotter for MockSpotter {
    fn push(&mut self, _pcm: &[i16]) {}

    fn poll(&mut self) -> Result<Option<WakeHit>> {
        if self.fire.swap(false, std::sync::atomic::Ordering::SeqCst) {
            return Ok(Some(WakeHit {
                phrase: "ada".to_string(),
                score: 1.0,
            }));
        }
        Ok(None)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::sync::Arc;

    /// Scores a window by whether it ends with the marker sample value, and
    /// records every window it was shown.
    struct Fake {
        seen: Vec<Vec<i16>>,
        scores: VecDeque<f32>,
    }

    impl Scorer for Fake {
        fn score(&mut self, window: &[i16]) -> Result<f32> {
            self.seen.push(window.to_vec());
            Ok(self.scores.pop_front().unwrap_or(0.0))
        }
    }

    fn fake(scores: &[f32]) -> Fake {
        Fake {
            seen: Vec::new(),
            scores: scores.iter().copied().collect(),
        }
    }

    #[test]
    fn mock_stays_quiet_until_triggered() {
        let fire = Arc::new(AtomicBool::new(false));
        let mut spotter = MockSpotter::new(Arc::clone(&fire));
        assert_eq!(spotter.poll().unwrap(), None);
        fire.store(true, Ordering::SeqCst);
        assert_eq!(spotter.poll().unwrap().expect("hit").phrase, "ada");
        assert_eq!(spotter.poll().unwrap(), None);
    }

    #[test]
    fn nothing_is_scored_before_two_seconds_of_audio() {
        let mut s = WindowSpotter::new(fake(&[1.0]), Trigger::new(0.5, 1, 0), "hey ada");
        s.push(&vec![0; WINDOW - 1]);
        assert_eq!(s.poll().unwrap(), None);
        assert!(s.scorer.seen.is_empty());
        s.push(&[0]);
        assert!(s.poll().unwrap().is_some());
    }

    /// The failure this module exists to prevent: a late score must see the
    /// newest contiguous 2 s, however much audio arrived while it was busy.
    #[test]
    fn a_late_poll_scores_the_newest_contiguous_window() {
        let mut s = WindowSpotter::new(fake(&[]), Trigger::new(0.5, 1, 0), "ada");
        let audio: Vec<i16> = (0..(WINDOW * 3) as i32).map(|i| (i % 30_000) as i16).collect();
        // Arrives in odd-sized callbacks, and nobody polls for 6 s of audio.
        for chunk in audio.chunks(170) {
            s.push(chunk);
        }
        s.poll().unwrap();
        let seen = &s.scorer.seen[0];
        assert_eq!(seen.len(), WINDOW);
        assert_eq!(seen.as_slice(), &audio[audio.len() - WINDOW..]);
        // No new frame yet: no second score of the same audio.
        s.push(&[0; FRAME - 1]);
        s.poll().unwrap();
        assert_eq!(s.scorer.seen.len(), 1);
        s.push(&[0]);
        s.poll().unwrap();
        assert_eq!(s.scorer.seen.len(), 2);
    }

    #[test]
    fn trigger_level_needs_consecutive_scores() {
        let mut t = Trigger::new(0.7, 2, 0);
        assert!(!t.observe(0.9));
        assert!(!t.observe(0.2)); // run broken
        assert!(!t.observe(0.8));
        assert!(t.observe(0.7)); // inclusive, like livekit's `score >= threshold`
    }

    #[test]
    fn refractory_period_is_counted_in_audio() {
        let mut t = Trigger::new(0.5, 1, 32_000);
        assert!(t.observe(0.9));
        t.advance(31_999);
        assert!(!t.observe(0.9));
        t.advance(1);
        assert!(t.observe(0.9));
    }

    #[test]
    fn a_hit_clears_the_window_so_one_utterance_fires_once() {
        let mut s = WindowSpotter::new(fake(&[0.9, 0.9]), Trigger::new(0.5, 1, 0), "hey ada");
        s.push(&vec![0; WINDOW]);
        let hit = s.poll().unwrap().expect("hit");
        assert_eq!(hit.phrase, "hey ada");
        s.push(&vec![0; FRAME]);
        assert_eq!(s.poll().unwrap(), None);
    }
}
