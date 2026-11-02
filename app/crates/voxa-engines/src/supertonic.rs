//! Supertonic 3 (Supertone; model OpenRAIL-M, code MIT) through sherpa-onnx: 31 languages
//! from plain text, 10 speakers, 44.1 kHz. Voxa offers the languages it has checked.
//!
//! A Voxa Supertonic package holds sherpa-onnx's export:
//!
//! ```text
//! duration_predictor.int8.onnx  text_encoder.int8.onnx  vector_estimator.int8.onnx
//! vocoder.int8.onnx  tts.json  unicode_indexer.bin  voice.bin
//! ```
//!
//! Supertonic reads raw text through its own Unicode indexer, so no G2P (and no espeak) is
//! involved. A voice here is "<speaker id>:<language>", e.g. "3:es".

use std::collections::HashMap;
use std::path::{Path, PathBuf};

use sherpa_onnx::{GenerationConfig, OfflineTts, OfflineTtsConfig};
use voxa_core::{Audio, Engine, EngineError};

/// Languages Voxa has checked (speech transcribed back by Whisper base): Spanish, French,
/// German, Italian, Portuguese and Hindi. Supertonic has 31; others can be added after the
/// same check.
pub const CHECKED_LANGUAGES: &[&str] = &["es", "fr", "de", "it", "pt", "hi"];

const FILES: &[&str] = &[
    "duration_predictor.int8.onnx",
    "text_encoder.int8.onnx",
    "vector_estimator.int8.onnx",
    "vocoder.int8.onnx",
    "tts.json",
    "unicode_indexer.bin",
    "voice.bin",
];

pub struct SupertonicEngine {
    id: String,
    tts: OfflineTts,
    sample_rate: u32,
    speakers: i32,
    languages: Vec<String>,
}

/// Splits a voice id "3:es" into (speaker, language).
pub fn parse_voice(voice: &str) -> Option<(i32, &str)> {
    let (sid, lang) = voice.split_once(':')?;
    Some((sid.parse().ok()?, lang))
}

impl SupertonicEngine {
    pub fn load(
        id: &str,
        dir: &Path,
        languages: &[&str],
        threads: i32,
    ) -> Result<Self, EngineError> {
        let path = |name: &str| -> Result<String, EngineError> {
            let p: PathBuf = dir.join(name);
            if p.is_file() {
                Ok(p.to_string_lossy().into_owned())
            } else {
                Err(EngineError::Failed(format!(
                    "Supertonic package is missing {name}"
                )))
            }
        };
        for f in FILES {
            path(f)?;
        }
        let mut cfg = OfflineTtsConfig::default();
        let s = &mut cfg.model.supertonic;
        s.duration_predictor = Some(path(FILES[0])?);
        s.text_encoder = Some(path(FILES[1])?);
        s.vector_estimator = Some(path(FILES[2])?);
        s.vocoder = Some(path(FILES[3])?);
        s.tts_json = Some(path(FILES[4])?);
        s.unicode_indexer = Some(path(FILES[5])?);
        s.voice_style = Some(path(FILES[6])?);
        cfg.model.num_threads = threads.max(1);
        let tts = OfflineTts::create(&cfg)
            .ok_or_else(|| EngineError::Failed("could not load the Supertonic model".into()))?;
        Ok(Self {
            id: id.into(),
            sample_rate: tts.sample_rate() as u32,
            speakers: tts.num_speakers(),
            tts,
            languages: languages.iter().map(|l| l.to_string()).collect(),
        })
    }

    pub fn speakers(&self) -> i32 {
        self.speakers
    }
}

impl Engine for SupertonicEngine {
    fn id(&self) -> &str {
        &self.id
    }

    fn sample_rate(&self) -> u32 {
        self.sample_rate
    }

    fn voices(&self) -> Vec<String> {
        (0..self.speakers)
            .flat_map(|sid| self.languages.iter().map(move |l| format!("{sid}:{l}")))
            .collect()
    }

    fn synthesize_sentence(
        &self,
        sentence: &str,
        voice: &str,
        speed: f32,
    ) -> Result<Audio, EngineError> {
        let (sid, lang) = parse_voice(voice)
            .filter(|(s, l)| *s < self.speakers && self.languages.iter().any(|x| x == l))
            .ok_or_else(|| EngineError::UnknownVoice(voice.into()))?;
        let mut extra = HashMap::new();
        extra.insert("lang".to_string(), serde_json::json!(lang));
        let cfg = GenerationConfig {
            sid,
            speed,
            extra: Some(extra),
            ..Default::default()
        };
        let out = self
            .tts
            .generate_with_config(sentence, &cfg, None::<fn(&[f32], f32) -> bool>)
            .ok_or_else(|| EngineError::Failed("Supertonic generation failed".into()))?;
        Ok(Audio::new(out.sample_rate() as u32, out.samples().to_vec()))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use voxa_core::SynthesisRequest;

    #[test]
    fn parses_voice_ids() {
        assert_eq!(parse_voice("3:es"), Some((3, "es")));
        assert_eq!(parse_voice("x:es"), None);
        assert_eq!(parse_voice("3"), None);
    }

    #[test]
    fn missing_files_are_named() {
        let d = tempfile::tempdir().unwrap();
        let err = SupertonicEngine::load("s", d.path(), CHECKED_LANGUAGES, 1)
            .err()
            .unwrap();
        assert!(err.to_string().contains("duration_predictor"), "{err}");
    }

    /// `VOXA_SUPERTONIC_DIR=<package> cargo test -p voxa-engines -- --ignored supertonic`
    #[test]
    #[ignore = "needs the Supertonic model (VOXA_SUPERTONIC_DIR)"]
    fn supertonic_speaks_the_checked_languages() {
        let dir = std::env::var("VOXA_SUPERTONIC_DIR").expect("set VOXA_SUPERTONIC_DIR");
        let e = SupertonicEngine::load("s", Path::new(&dir), CHECKED_LANGUAGES, 2).unwrap();
        assert_eq!(e.speakers(), 10);
        assert_eq!(e.sample_rate(), 44_100);
        assert_eq!(e.voices().len(), 10 * CHECKED_LANGUAGES.len());
        for (lang, text) in [
            ("es", "Hola. ¿Cómo estás?"),
            ("de", "Guten Tag. Wie geht es dir?"),
            ("hi", "नमस्ते। आप कैसे हैं?"),
        ] {
            let req = SynthesisRequest {
                text: text.into(),
                voice: format!("0:{lang}"),
                speed: 1.0,
            };
            let a = e.synthesize(&req, &mut |_, _| {}).unwrap();
            assert!(
                a.duration() > 0.5 && a.duration() < 6.0,
                "{lang}: {}",
                a.duration()
            );
        }
        assert!(matches!(
            e.synthesize_sentence("Hi.", "0:xx", 1.0),
            Err(EngineError::UnknownVoice(_))
        ));
        assert!(matches!(
            e.synthesize_sentence("Hi.", "10:es", 1.0),
            Err(EngineError::UnknownVoice(_))
        ));
    }
}
