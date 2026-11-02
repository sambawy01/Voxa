//! The interface every on-device voice model implements, and a placeholder engine.

use crate::audio::Audio;
use crate::text::{split_sentences, validate_text, TextError};
use thiserror::Error;

/// Silence between sentences when joining, in seconds.
pub const SENTENCE_GAP_S: f32 = 0.25;

#[derive(Debug, Clone, PartialEq)]
pub struct SynthesisRequest {
    pub text: String,
    /// Voxa voice id (e.g. "harper"); the engine maps it to its own voice.
    pub voice: String,
    /// 0.5–2.0, as on the website.
    pub speed: f32,
}

#[derive(Debug, Error, PartialEq)]
pub enum EngineError {
    #[error(transparent)]
    Text(#[from] TextError),
    #[error("speed must be between 0.5 and 2.0, got {0}")]
    BadSpeed(f32),
    #[error("unknown voice {0:?}")]
    UnknownVoice(String),
    #[error("generation failed: {0}")]
    Failed(String),
}

/// Turns one sentence of text into the phoneme string a model was trained on (for Kokoro,
/// misaki's symbols). Implemented by the G2P crate; engines that read raw text (Supertonic,
/// Pocket TTS) don't need one.
pub trait Phonemizer: Send + Sync {
    fn phonemize(&self, sentence: &str) -> Result<String, EngineError>;
}

/// A voice model that turns one sentence into audio.
///
/// Implementations do the model work in [`Engine::synthesize_sentence`]; the provided
/// [`Engine::synthesize`] validates the request, splits sentences, reports progress and
/// joins the results, so every engine behaves the same way.
pub trait Engine: Send + Sync {
    /// Stable id, e.g. "kokoro-82m-int8".
    fn id(&self) -> &str;
    fn sample_rate(&self) -> u32;
    fn voices(&self) -> Vec<String>;
    fn synthesize_sentence(
        &self,
        sentence: &str,
        voice: &str,
        speed: f32,
    ) -> Result<Audio, EngineError>;

    /// Generates the whole request. `on_progress` gets (sentences done, total).
    fn synthesize(
        &self,
        req: &SynthesisRequest,
        on_progress: &mut dyn FnMut(usize, usize),
    ) -> Result<Audio, EngineError> {
        let text = validate_text(&req.text)?;
        if !(0.5..=2.0).contains(&req.speed) {
            return Err(EngineError::BadSpeed(req.speed));
        }
        if !self.voices().iter().any(|v| v == &req.voice) {
            return Err(EngineError::UnknownVoice(req.voice.clone()));
        }
        let sentences = split_sentences(text);
        let total = sentences.len();
        let mut out = Audio::new(self.sample_rate(), Vec::new());
        on_progress(0, total);
        for (i, s) in sentences.iter().enumerate() {
            let a = self.synthesize_sentence(s, &req.voice, req.speed)?;
            if a.sample_rate != out.sample_rate {
                return Err(EngineError::Failed(format!(
                    "engine returned {} Hz, expected {}",
                    a.sample_rate, out.sample_rate
                )));
            }
            out.append(&a, SENTENCE_GAP_S);
            on_progress(i + 1, total);
        }
        Ok(out)
    }
}

/// Placeholder engine for M1: a soft tone per word, so the app can be wired and tested
/// before real models arrive (M3). Not a voice; never offered to users once M3 lands.
pub struct ToneEngine;

impl ToneEngine {
    const RATE: u32 = 24_000;
}

impl Engine for ToneEngine {
    fn id(&self) -> &str {
        "placeholder-tone"
    }

    fn sample_rate(&self) -> u32 {
        Self::RATE
    }

    fn voices(&self) -> Vec<String> {
        vec!["low".into(), "high".into()]
    }

    fn synthesize_sentence(
        &self,
        sentence: &str,
        voice: &str,
        speed: f32,
    ) -> Result<Audio, EngineError> {
        let freq = if voice == "high" { 440.0 } else { 220.0 };
        let word_s = 0.18 / speed;
        let gap_s = 0.06 / speed;
        let rate = Self::RATE as f32;
        let mut samples = Vec::new();
        for _ in sentence.split_whitespace() {
            let n = (word_s * rate) as usize;
            for k in 0..n {
                let t = k as f32 / rate;
                // Fade in and out to avoid clicks.
                let env = (std::f32::consts::PI * k as f32 / n as f32).sin();
                samples.push(0.25 * env * (2.0 * std::f32::consts::PI * freq * t).sin());
            }
            samples.extend(std::iter::repeat_n(0.0, (gap_s * rate) as usize));
        }
        Ok(Audio::new(Self::RATE, samples))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn req(text: &str, voice: &str, speed: f32) -> SynthesisRequest {
        SynthesisRequest {
            text: text.into(),
            voice: voice.into(),
            speed,
        }
    }

    #[test]
    fn synthesizes_and_reports_progress() {
        let mut seen = Vec::new();
        let a = ToneEngine
            .synthesize(&req("One two. Three!", "low", 1.0), &mut |d, t| {
                seen.push((d, t))
            })
            .unwrap();
        assert_eq!(seen, vec![(0, 2), (1, 2), (2, 2)]);
        assert_eq!(a.sample_rate, 24_000);
        // 3 words of (0.18 + 0.06) s plus one sentence gap.
        assert!((a.duration() - (3.0 * 0.24 + 0.25)).abs() < 0.01);
    }

    #[test]
    fn faster_speed_is_shorter() {
        let slow = ToneEngine
            .synthesize(&req("a b c", "low", 0.5), &mut |_, _| {})
            .unwrap();
        let fast = ToneEngine
            .synthesize(&req("a b c", "low", 2.0), &mut |_, _| {})
            .unwrap();
        assert!(fast.duration() < slow.duration() / 3.0);
    }

    #[test]
    fn rejects_bad_requests() {
        let e = &ToneEngine;
        assert_eq!(
            e.synthesize(&req("  ", "low", 1.0), &mut |_, _| {}),
            Err(EngineError::Text(TextError::Empty))
        );
        assert_eq!(
            e.synthesize(&req("hi", "low", 0.01), &mut |_, _| {}),
            Err(EngineError::BadSpeed(0.01))
        );
        assert_eq!(
            e.synthesize(&req("hi", "nobody", 1.0), &mut |_, _| {}),
            Err(EngineError::UnknownVoice("nobody".into()))
        );
    }

    #[test]
    fn output_stays_in_range() {
        let a = ToneEngine
            .synthesize(&req("loud words here", "high", 1.0), &mut |_, _| {})
            .unwrap();
        assert!(a.samples.iter().all(|s| s.abs() <= 1.0));
    }
}
