//! Default input device via cpal. This is the always-on wake mic owner.
//!
//! Wake keeps its own stream and drops it on a hit so the webview can take
//! the device for one command clip. (The Pluely system-audio `speaker`
//! module that used to own a second stream is gone: nothing in the webview
//! ever invoked its commands.)

use anyhow::{bail, Context, Result};
use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};
use cpal::{SampleFormat, Stream, StreamConfig};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError, SyncSender, TryRecvError};
use std::sync::Arc;
use std::time::Duration;

/// Hardware callbacks the queue holds before it starts dropping audio.
///
/// It used to be 8 (~85 ms of 512-frame callbacks at 48 kHz) while one wake
/// score took 3 s in a `tauri dev` build, so the spotter was scoring a
/// window stitched out of the few callbacks that fit: the word was never in
/// it whole. 1024 callbacks is ~10 s at 48 kHz, far more than one score
/// takes, and the wake thread drains the whole queue before every score.
const QUEUE_CALLBACKS: usize = 1024;

/// Mono f32 chunks from the hardware callback. The wake thread resamples.
pub struct Mic {
    _stream: Stream,
    rx: Receiver<Vec<f32>>,
    pub sample_rate: u32,
    pub channels: u16,
    /// Callbacks dropped because the queue was full. Must stay zero; a
    /// non-zero count means the spotter saw audio with holes in it.
    pub dropped: Arc<AtomicU64>,
}

impl Mic {
    pub fn open() -> Result<Self> {
        let host = cpal::default_host();
        let device = host
            .default_input_device()
            .context("no default microphone")?;
        let supported = device
            .default_input_config()
            .context("could not read the microphone format")?;
        let sample_rate = supported.sample_rate().0;
        let channels = supported.channels();
        let format = supported.sample_format();
        let config: StreamConfig = supported.into();
        let (tx, rx) = mpsc::sync_channel::<Vec<f32>>(QUEUE_CALLBACKS);
        let dropped = Arc::new(AtomicU64::new(0));
        let d = dropped.clone();
        let stream = match format {
            SampleFormat::F32 => build_stream(&device, &config, tx, d, |s| s),
            SampleFormat::I16 => build_stream(&device, &config, tx, d, |s: i16| s as f32 / 32768.0),
            SampleFormat::U16 => {
                build_stream(&device, &config, tx, d, |s: u16| (s as f32 / 32768.0) - 1.0)
            }
            other => bail!("unsupported microphone format: {other}"),
        }?;
        stream.play().context("could not start the microphone")?;
        Ok(Self {
            _stream: stream,
            rx,
            sample_rate,
            channels,
            dropped,
        })
    }

    /// Next queued chunk without waiting, so the wake thread can drain the
    /// queue before it spends a score on the newest window.
    pub fn try_recv(&self) -> Result<Option<Vec<f32>>> {
        match self.rx.try_recv() {
            Ok(chunk) => Ok(Some(chunk)),
            Err(TryRecvError::Empty) => Ok(None),
            Err(TryRecvError::Disconnected) => bail!("microphone stream ended"),
        }
    }

    pub fn recv_timeout(&self, timeout: Duration) -> Result<Option<Vec<f32>>> {
        match self.rx.recv_timeout(timeout) {
            Ok(chunk) => Ok(Some(chunk)),
            Err(RecvTimeoutError::Timeout) => Ok(None),
            Err(RecvTimeoutError::Disconnected) => bail!("microphone stream ended"),
        }
    }
}

fn build_stream<T, F>(
    device: &cpal::Device,
    config: &StreamConfig,
    tx: SyncSender<Vec<f32>>,
    dropped: Arc<AtomicU64>,
    to_f32: F,
) -> Result<Stream>
where
    T: cpal::SizedSample + Send + 'static,
    F: Fn(T) -> f32 + Send + 'static,
{
    let err_fn = |e| eprintln!("[wake] microphone stream error: {e}");
    device
        .build_input_stream(
            config,
            move |data: &[T], _| {
                let converted: Vec<f32> = data.iter().copied().map(&to_f32).collect();
                if tx.try_send(converted).is_err() {
                    dropped.fetch_add(1, Ordering::Relaxed);
                }
            },
            err_fn,
            None,
        )
        .context("could not open the microphone stream")
}

/// Downmix interleaved samples to mono.
pub fn downmix_mono(samples: &[f32], channels: u16) -> Vec<f32> {
    if channels <= 1 {
        return samples.to_vec();
    }
    let n = channels as usize;
    samples
        .chunks(n)
        .map(|frame| frame.iter().sum::<f32>() / n as f32)
        .collect()
}

/// Streaming linear resampler: the fractional read position and the last
/// input sample carry across hardware callbacks.
///
/// The stateless version resampled each callback on its own and floored the
/// output length, so a 512-frame callback at 48 kHz became 170 samples, not
/// 170.67: every callback lost part of a sample and restarted its phase,
/// which is a click ~94 times a second and a clip that runs 0.4 % fast.
/// Linear interpolation itself is fine for this model — measured against
/// livekit-wakeword's own 90 dB FIR (`src/wakeword.rs`, `ResamplerFir`) on
/// 70 synthesised clips, recall and false accepts were the same — so the
/// fix is the state, not a better kernel.
#[derive(Debug, Clone)]
pub struct Resampler {
    src_hz: u32,
    dst_hz: u32,
    /// Position of the next output sample, in input samples, relative to
    /// the start of the next chunk (may be negative: it sits between `last`
    /// and the next chunk's first sample).
    pos: f64,
    last: Option<f32>,
}

impl Resampler {
    pub fn new(src_hz: u32, dst_hz: u32) -> Self {
        Self {
            src_hz,
            dst_hz,
            pos: 0.0,
            last: None,
        }
    }

    pub fn process(&mut self, input: &[f32]) -> Vec<f32> {
        if input.is_empty() || self.src_hz == 0 || self.dst_hz == 0 {
            return Vec::new();
        }
        if self.src_hz == self.dst_hz {
            return input.to_vec();
        }
        let step = self.src_hz as f64 / self.dst_hz as f64;
        let mut out = Vec::with_capacity((input.len() as f64 / step) as usize + 2);
        let at = |i: isize| -> f32 {
            if i < 0 {
                self.last.unwrap_or(input[0])
            } else {
                input[i as usize]
            }
        };
        // Emit while both neighbours of `pos` are known.
        while self.pos < (input.len() - 1) as f64 {
            let i0 = self.pos.floor() as isize;
            let frac = (self.pos - i0 as f64) as f32;
            out.push(at(i0) * (1.0 - frac) + at(i0 + 1) * frac);
            self.pos += step;
        }
        self.pos -= input.len() as f64;
        self.last = input.last().copied();
        out
    }
}

pub fn to_i16(samples: &[f32]) -> Vec<i16> {
    samples
        .iter()
        .map(|s| {
            let x = (s * 32767.0).round();
            x.clamp(i16::MIN as f32, i16::MAX as f32) as i16
        })
        .collect()
}

/// Push hardware samples into `pending` as 16 kHz mono i16.
pub fn ingest(pending: &mut Vec<i16>, chunk: &[f32], channels: u16, resampler: &mut Resampler) {
    let mono = downmix_mono(chunk, channels);
    pending.extend(to_i16(&resampler.process(&mono)));
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn stereo_downmix_averages_the_pair() {
        assert_eq!(downmix_mono(&[1.0, 3.0, 5.0, 7.0], 2), vec![2.0, 6.0]);
        assert_eq!(downmix_mono(&[1.0, 2.0], 1), vec![1.0, 2.0]);
    }

    #[test]
    fn forty_eight_to_sixteen_keeps_every_third_sample() {
        let input: Vec<f32> = (0..12).map(|i| i as f32).collect();
        let out = Resampler::new(48_000, 16_000).process(&input);
        assert_eq!(out, vec![0.0, 3.0, 6.0, 9.0]);
    }

    #[test]
    fn same_rate_is_a_copy() {
        assert_eq!(Resampler::new(16_000, 16_000).process(&[0.1, 0.2]), vec![0.1, 0.2]);
    }

    /// The bug this replaced: 512-frame callbacks at 48 kHz lost part of a
    /// sample each and restarted their phase. Chunked output must equal the
    /// one-shot output sample for sample, and keep the exact 3:1 length.
    #[test]
    fn chunked_resampling_matches_one_shot_with_no_lost_samples() {
        for src in [48_000_u32, 44_100] {
            let ramp: Vec<f32> = (0..48_000).map(|i| i as f32).collect();
            let whole = Resampler::new(src, 16_000).process(&ramp);
            let mut r = Resampler::new(src, 16_000);
            let chunked: Vec<f32> = ramp.chunks(512).flat_map(|c| r.process(c)).collect();
            assert_eq!(whole.len(), chunked.len(), "{src} Hz");
            for (a, b) in whole.iter().zip(&chunked) {
                assert!((a - b).abs() < 1e-2, "{src} Hz: {a} vs {b}");
            }
            let expected = 48_000.0 * 16_000.0 / src as f64;
            assert!((chunked.len() as f64 - expected).abs() <= 2.0, "{src} Hz");
            // A ramp resampled linearly stays a ramp: no click at a seam.
            let step = src as f32 / 16_000.0;
            for w in chunked.windows(2) {
                assert!((w[1] - w[0] - step).abs() < 1e-2);
            }
        }
    }

    #[test]
    fn to_i16_clamps() {
        assert_eq!(to_i16(&[0.0, 2.0, -2.0]), vec![0, 32767, i16::MIN]);
    }

    #[test]
    fn ingest_appends_resampled_mono() {
        let mut pending = Vec::new();
        let mut r = Resampler::new(16_000, 16_000);
        ingest(&mut pending, &[0.5, 0.5, -0.5, -0.5], 2, &mut r);
        assert_eq!(pending, vec![16384, -16384]);
    }
}
