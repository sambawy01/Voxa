//! Kokoro-82M (Apache-2.0) through sherpa-onnx.
//!
//! A Voxa Kokoro package (built by `app/tools/build_kokoro_package.py`) is a directory with:
//!
//! ```text
//! model.int8.onnx   the model (or model.onnx)
//! voices.bin        style table, one entry per speaker
//! tokens.txt        phoneme symbol -> id
//! speakers.json     ["af_alloy", "af_aoede", ...] in voices.bin order (from the model's
//!                   own metadata, so ids never depend on a hard-coded list)
//! ```
//!
//! Text never reaches sherpa-onnx: the [`Phonemizer`] turns each sentence into misaki
//! phonemes, which are passed as codepoints. That keeps espeak-ng (GPL) out and makes
//! unknown words the G2P's job instead of being silently skipped.

use std::path::{Path, PathBuf};
use std::sync::Arc;

use sherpa_onnx::{GenerationConfig, OfflineTts, OfflineTtsConfig};
use voxa_core::{Audio, Engine, EngineError, Phonemizer};

/// The files of a Kokoro package, checked to exist.
#[derive(Debug, Clone, PartialEq)]
pub struct KokoroPackage {
    pub model: PathBuf,
    pub voices: PathBuf,
    pub tokens: PathBuf,
    pub speakers: Vec<String>,
}

impl KokoroPackage {
    pub fn open(dir: &Path) -> Result<Self, EngineError> {
        let need = |name: &str| -> Result<PathBuf, EngineError> {
            let p = dir.join(name);
            if p.is_file() {
                Ok(p)
            } else {
                Err(EngineError::Failed(format!(
                    "Kokoro package is missing {name}"
                )))
            }
        };
        let model = need("model.int8.onnx").or_else(|_| need("model.onnx"))?;
        let raw = std::fs::read(need("speakers.json")?)
            .map_err(|e| EngineError::Failed(format!("speakers.json: {e}")))?;
        let speakers: Vec<String> = serde_json::from_slice(&raw)
            .map_err(|e| EngineError::Failed(format!("speakers.json: {e}")))?;
        if speakers.is_empty() {
            return Err(EngineError::Failed("speakers.json is empty".into()));
        }
        Ok(Self {
            model,
            voices: need("voices.bin")?,
            tokens: need("tokens.txt")?,
            speakers,
        })
    }

    fn config(&self, threads: i32) -> OfflineTtsConfig {
        let s = |p: &PathBuf| Some(p.to_string_lossy().into_owned());
        let mut cfg = OfflineTtsConfig::default();
        cfg.model.kokoro.model = s(&self.model);
        cfg.model.kokoro.voices = s(&self.voices);
        cfg.model.kokoro.tokens = s(&self.tokens);
        cfg.model.num_threads = threads.max(1);
        cfg.model.debug = false;
        cfg
    }
}

/// Kokoro for one language (one G2P), exposing that language's speakers.
pub struct KokoroEngine {
    id: String,
    tts: OfflineTts,
    sample_rate: u32,
    /// (Kokoro speaker name, sid) for the speakers this engine offers.
    voices: Vec<(String, i32)>,
    phonemizer: Arc<dyn Phonemizer>,
}

impl KokoroEngine {
    /// Loads the model. `voice_prefixes` picks the language's speakers by Kokoro's naming
    /// (e.g. `["af_", "am_"]` for American English).
    pub fn load(
        id: &str,
        package: &KokoroPackage,
        voice_prefixes: &[&str],
        phonemizer: Arc<dyn Phonemizer>,
        threads: i32,
    ) -> Result<Self, EngineError> {
        let tts = OfflineTts::create(&package.config(threads))
            .ok_or_else(|| EngineError::Failed("could not load the Kokoro model".into()))?;
        let n = tts.num_speakers();
        if n as usize != package.speakers.len() {
            return Err(EngineError::Failed(format!(
                "model has {n} speakers but speakers.json lists {}",
                package.speakers.len()
            )));
        }
        let voices = select_voices(&package.speakers, voice_prefixes);
        if voices.is_empty() {
            return Err(EngineError::Failed(format!(
                "no Kokoro speakers match {voice_prefixes:?}"
            )));
        }
        let sample_rate = tts.sample_rate() as u32;
        Ok(Self {
            id: id.into(),
            tts,
            sample_rate,
            voices,
            phonemizer,
        })
    }

    fn sid(&self, voice: &str) -> Option<i32> {
        self.voices
            .iter()
            .find(|(n, _)| n == voice)
            .map(|(_, i)| *i)
    }
}

/// Speakers whose names start with one of the prefixes, with their ids.
fn select_voices(speakers: &[String], prefixes: &[&str]) -> Vec<(String, i32)> {
    speakers
        .iter()
        .enumerate()
        .filter(|(_, n)| prefixes.iter().any(|p| n.starts_with(p)))
        .map(|(i, n)| (n.clone(), i as i32))
        .collect()
}

/// Phonemes as codepoints for sherpa-onnx (one sentence = one inner vector).
fn codepoints(phonemes: &str) -> Vec<i32> {
    phonemes.chars().map(|c| c as i32).collect()
}

impl Engine for KokoroEngine {
    fn id(&self) -> &str {
        &self.id
    }

    fn sample_rate(&self) -> u32 {
        self.sample_rate
    }

    fn voices(&self) -> Vec<String> {
        self.voices.iter().map(|(n, _)| n.clone()).collect()
    }

    fn synthesize_sentence(
        &self,
        sentence: &str,
        voice: &str,
        speed: f32,
    ) -> Result<Audio, EngineError> {
        let sid = self
            .sid(voice)
            .ok_or_else(|| EngineError::UnknownVoice(voice.into()))?;
        let phonemes = self.phonemizer.phonemize(sentence)?;
        if phonemes.trim().is_empty() {
            return Ok(Audio::new(self.sample_rate, Vec::new()));
        }
        let cfg = GenerationConfig {
            sid,
            speed,
            phoneme_codepoints: Some(vec![codepoints(&phonemes)]),
            ..Default::default()
        };
        let out = self
            .tts
            .generate_with_config("", &cfg, None::<fn(&[f32], f32) -> bool>)
            .ok_or_else(|| EngineError::Failed("Kokoro generation failed".into()))?;
        Ok(Audio::new(out.sample_rate() as u32, out.samples().to_vec()))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    fn package_dir(files: &[&str], speakers: &str) -> tempfile::TempDir {
        let d = tempfile::tempdir().unwrap();
        for f in files {
            fs::write(d.path().join(f), b"x").unwrap();
        }
        fs::write(d.path().join("speakers.json"), speakers).unwrap();
        d
    }

    #[test]
    fn package_requires_every_file() {
        let d = package_dir(&["model.int8.onnx", "voices.bin"], r#"["af_heart"]"#);
        let err = KokoroPackage::open(d.path()).unwrap_err();
        assert!(err.to_string().contains("tokens.txt"), "{err}");
    }

    #[test]
    fn package_accepts_either_model_name() {
        let d = package_dir(
            &["model.onnx", "voices.bin", "tokens.txt"],
            r#"["af_heart"]"#,
        );
        let p = KokoroPackage::open(d.path()).unwrap();
        assert!(p.model.ends_with("model.onnx"));
        assert_eq!(p.speakers, ["af_heart"]);
    }

    #[test]
    fn package_rejects_bad_speakers() {
        let files = ["model.int8.onnx", "voices.bin", "tokens.txt"];
        assert!(KokoroPackage::open(package_dir(&files, "[]").path()).is_err());
        assert!(KokoroPackage::open(package_dir(&files, "nope").path()).is_err());
    }

    #[test]
    fn selects_speakers_by_prefix_keeping_ids() {
        let speakers: Vec<String> = ["af_alloy", "am_adam", "bf_alice", "af_heart"]
            .iter()
            .map(|s| s.to_string())
            .collect();
        assert_eq!(
            select_voices(&speakers, &["af_", "am_"]),
            vec![
                ("af_alloy".into(), 0),
                ("am_adam".into(), 1),
                ("af_heart".into(), 3)
            ]
        );
        assert!(select_voices(&speakers, &["zf_"]).is_empty());
    }

    #[test]
    fn codepoints_are_unicode_scalars() {
        assert_eq!(codepoints("hˈə"), vec![104, 712, 601]);
    }

    /// Phonemizes from a word table; enough to exercise the real model.
    struct TablePhonemizer(Vec<(&'static str, &'static str)>);

    impl Phonemizer for TablePhonemizer {
        fn phonemize(&self, sentence: &str) -> Result<String, EngineError> {
            let mut out = Vec::new();
            for w in sentence.split_whitespace() {
                let w = w
                    .trim_matches(|c: char| !c.is_alphanumeric())
                    .to_lowercase();
                let p = self
                    .0
                    .iter()
                    .find(|(k, _)| *k == w)
                    .map(|(_, v)| *v)
                    .ok_or_else(|| EngineError::Failed(format!("no phonemes for {w}")))?;
                out.push(p);
            }
            Ok(out.join(" ") + ".")
        }
    }

    /// Needs a real package: `VOXA_KOKORO_DIR=/path/to/package cargo test -- --ignored`.
    #[test]
    #[ignore = "needs the Kokoro model (VOXA_KOKORO_DIR)"]
    fn speaks_with_the_real_model() {
        let dir = std::env::var("VOXA_KOKORO_DIR").expect("set VOXA_KOKORO_DIR");
        let pkg = KokoroPackage::open(Path::new(&dir)).unwrap();
        let g2p = Arc::new(TablePhonemizer(vec![
            ("hello", "həlˈO"),
            ("world", "wˈɜɹld"),
        ]));
        let e = KokoroEngine::load("kokoro-en-us", &pkg, &["af_", "am_"], g2p, 2).unwrap();
        assert_eq!(e.sample_rate(), 24_000);
        assert!(e.voices().contains(&"af_heart".to_string()));
        assert!(!e.voices().iter().any(|v| v.starts_with("bf_")));
        let a = e
            .synthesize_sentence("Hello world.", "af_heart", 1.0)
            .unwrap();
        // About 1 s of speech; not silence.
        assert!(a.duration() > 0.4 && a.duration() < 3.0, "{}", a.duration());
        let rms = (a.samples.iter().map(|s| s * s).sum::<f32>() / a.samples.len() as f32).sqrt();
        assert!(rms > 0.01, "rms {rms}");
        let fast = e
            .synthesize_sentence("Hello world.", "af_heart", 1.5)
            .unwrap();
        assert!(fast.duration() < a.duration());
        assert!(matches!(
            e.synthesize_sentence("Hello.", "bf_emma", 1.0),
            Err(EngineError::UnknownVoice(_))
        ));
    }
}
