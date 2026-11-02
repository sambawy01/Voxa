//! Audio clean-up (roadmap M8): removing noise from recorded speech, for film dialogue and
//! recordings. Through sherpa-onnx:
//!
//! - **DPDFNet 48 kHz HR** (Ceva, Apache-2.0): full-band 48 kHz, the "Studio" clean-up.
//!   On speech mixed with noise at 0 dB SNR it reached 17.1 dB at ~5x real time on 4 CPUs.
//! - **GTCRN** (MIT): tiny and ~20x real time, but it works at 16 kHz, so it removes the
//!   top of the spectrum from 44.1/48 kHz audio: the "Fast" option for voice memos.
//!
//! Film needs the output to line up with the picture, so [`Denoiser::clean`] always returns
//! **exactly as many samples as it was given, with no delay** (the models drop a partial
//! last frame; the input is padded and the output trimmed). The strength mix is separate
//! ([`mix`]) so moving the slider never re-runs the model.

use std::path::Path;

use sherpa_onnx::{LinearResampler, OfflineSpeechDenoiser, OfflineSpeechDenoiserConfig};
use voxa_core::EngineError;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DenoiserKind {
    /// DPDFNet 48 kHz HR.
    Studio,
    /// GTCRN, 16 kHz.
    Fast,
}

pub struct Denoiser {
    kind: DenoiserKind,
    inner: OfflineSpeechDenoiser,
    rate: u32,
}

/// Padding added after the input so the model's last (dropped) partial frame is silence.
const PAD_SECONDS: f32 = 0.1;

fn resample(x: &[f32], from: u32, to: u32) -> Result<Vec<f32>, EngineError> {
    if from == to {
        return Ok(x.to_vec());
    }
    let r = LinearResampler::create(from as i32, to as i32)
        .ok_or_else(|| EngineError::Failed(format!("cannot resample {from} -> {to} Hz")))?;
    Ok(r.resample(x, true))
}

/// Pads with zeros or trims so the result has exactly `n` samples.
fn exact(mut v: Vec<f32>, n: usize) -> Vec<f32> {
    v.resize(n, 0.0);
    v
}

impl Denoiser {
    pub fn load(kind: DenoiserKind, model: &Path, threads: i32) -> Result<Self, EngineError> {
        if !model.is_file() {
            return Err(EngineError::Failed(format!(
                "denoiser model missing: {}",
                model.display()
            )));
        }
        let path = Some(model.to_string_lossy().into_owned());
        let mut cfg = OfflineSpeechDenoiserConfig::default();
        match kind {
            DenoiserKind::Studio => cfg.model.dpdfnet.model = path,
            DenoiserKind::Fast => cfg.model.gtcrn.model = path,
        }
        cfg.model.num_threads = threads.max(1);
        let inner = OfflineSpeechDenoiser::create(&cfg)
            .ok_or_else(|| EngineError::Failed("could not load the denoiser".into()))?;
        let rate = inner.sample_rate() as u32;
        Ok(Self { kind, inner, rate })
    }

    pub fn kind(&self) -> DenoiserKind {
        self.kind
    }

    /// The rate the model works at (48000 for Studio, 16000 for Fast).
    pub fn model_rate(&self) -> u32 {
        self.rate
    }

    /// Denoises one channel at `rate`. Returns exactly `x.len()` samples at `rate`, aligned
    /// with the input.
    pub fn clean(&self, x: &[f32], rate: u32) -> Result<Vec<f32>, EngineError> {
        if x.is_empty() {
            return Ok(Vec::new());
        }
        let mut input = resample(x, rate, self.rate)?;
        let n_model = input.len();
        input.extend(std::iter::repeat_n(
            0.0,
            (PAD_SECONDS * self.rate as f32) as usize,
        ));
        let out = self.inner.run(&input, self.rate as i32);
        let at_model = exact(
            resample(&out.samples, out.sample_rate as u32, self.rate)?,
            n_model,
        );
        Ok(exact(resample(&at_model, self.rate, rate)?, x.len()))
    }
}

/// Blends cleaned and original audio: `strength` 1.0 is fully cleaned, 0.0 the original.
/// Film editors usually keep some room tone; nothing is re-computed when this changes.
pub fn mix(original: &[f32], cleaned: &[f32], strength: f32) -> Vec<f32> {
    let s = strength.clamp(0.0, 1.0);
    original
        .iter()
        .zip(cleaned)
        .map(|(o, c)| s * c + (1.0 - s) * o)
        .collect()
}

/// Anything that removes noise from a recording, keeping its exact length: the built-in
/// denoisers here, or a clean-up worker (RE-USE on a GPU, see `worker`).
pub trait Cleaner: Send + Sync {
    fn clean(&self, samples: &[f32], rate: u32) -> Result<Vec<f32>, EngineError>;
}

impl Cleaner for Denoiser {
    fn clean(&self, samples: &[f32], rate: u32) -> Result<Vec<f32>, EngineError> {
        Denoiser::clean(self, samples, rate)
    }
}

impl Cleaner for crate::worker::WorkerEngine {
    fn clean(&self, samples: &[f32], rate: u32) -> Result<Vec<f32>, EngineError> {
        crate::worker::WorkerEngine::clean(self, samples, rate)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn mix_blends_and_clamps() {
        let o = [1.0, 1.0];
        let c = [0.0, 0.5];
        assert_eq!(mix(&o, &c, 1.0), vec![0.0, 0.5]);
        assert_eq!(mix(&o, &c, 0.0), vec![1.0, 1.0]);
        assert_eq!(mix(&o, &c, 0.5), vec![0.5, 0.75]);
        assert_eq!(mix(&o, &c, 7.0), vec![0.0, 0.5]);
    }

    #[test]
    fn exact_pads_and_trims() {
        assert_eq!(exact(vec![1.0, 2.0], 3), vec![1.0, 2.0, 0.0]);
        assert_eq!(exact(vec![1.0, 2.0, 3.0], 2), vec![1.0, 2.0]);
    }

    #[test]
    fn missing_model_is_reported() {
        let err = Denoiser::load(DenoiserKind::Studio, Path::new("/nope.onnx"), 1)
            .err()
            .unwrap();
        assert!(err.to_string().contains("missing"));
    }

    /// `VOXA_DPDFNET=<dpdfnet2_48khz_hr.onnx> VOXA_GTCRN=<gtcrn_simple.onnx>
    ///  cargo test -p voxa-engines -- --ignored cleanup`
    #[test]
    #[ignore = "needs the denoiser models (VOXA_DPDFNET, VOXA_GTCRN)"]
    fn cleanup_keeps_exact_length_at_every_rate() {
        let models = [
            (DenoiserKind::Studio, std::env::var("VOXA_DPDFNET").unwrap()),
            (DenoiserKind::Fast, std::env::var("VOXA_GTCRN").unwrap()),
        ];
        for (kind, path) in models {
            let d = Denoiser::load(kind, Path::new(&path), 2).unwrap();
            for (rate, n) in [
                (48_000, 48_001),
                (44_100, 30_007),
                (16_000, 9_999),
                (48_000, 7),
            ] {
                let x: Vec<f32> = (0..n).map(|i| ((i * 7919) % 101) as f32 / 404.0).collect();
                let y = d.clean(&x, rate).unwrap();
                assert_eq!(y.len(), n, "{kind:?} at {rate} Hz, {n} samples");
                assert!(y.iter().all(|v| v.is_finite()));
            }
            assert!(d.clean(&[], 48_000).unwrap().is_empty());
        }
    }
}
