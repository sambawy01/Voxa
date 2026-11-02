//! Pocket TTS (Kyutai; ONNX export CC-BY-4.0) through sherpa-onnx: English speech in the
//! voice of a short reference recording. Used only for "Your voices", created after the
//! consent checks in [`crate::consent`]. Shipped as **Preview**: in Voxa's checks its
//! clones resembled the reference only weakly (speaker similarity 0.27-0.37).
//!
//! A voice here is the id of a reference recording registered with [`PocketEngine::set_voice`].

use std::collections::HashMap;
use std::path::Path;
use std::sync::Mutex;

use sherpa_onnx::{GenerationConfig, OfflineTts, OfflineTtsConfig};
use voxa_core::{Audio, Engine, EngineError};

const FILES: &[&str] = &[
    "lm_flow.int8.onnx",
    "lm_main.int8.onnx",
    "encoder.onnx",
    "decoder.int8.onnx",
    "text_conditioner.onnx",
    "vocab.json",
    "token_scores.json",
];

pub struct PocketEngine {
    id: String,
    tts: OfflineTts,
    sample_rate: u32,
    references: Mutex<HashMap<String, Audio>>,
}

impl PocketEngine {
    pub fn load(id: &str, dir: &Path, threads: i32) -> Result<Self, EngineError> {
        let path = |n: &str| -> Result<String, EngineError> {
            let p = dir.join(n);
            if p.is_file() {
                Ok(p.to_string_lossy().into_owned())
            } else {
                Err(EngineError::Failed(format!(
                    "Pocket TTS package is missing {n}"
                )))
            }
        };
        for f in FILES {
            path(f)?;
        }
        let mut cfg = OfflineTtsConfig::default();
        let p = &mut cfg.model.pocket;
        p.lm_flow = Some(path(FILES[0])?);
        p.lm_main = Some(path(FILES[1])?);
        p.encoder = Some(path(FILES[2])?);
        p.decoder = Some(path(FILES[3])?);
        p.text_conditioner = Some(path(FILES[4])?);
        p.vocab_json = Some(path(FILES[5])?);
        p.token_scores_json = Some(path(FILES[6])?);
        cfg.model.num_threads = threads.max(1);
        let tts = OfflineTts::create(&cfg)
            .ok_or_else(|| EngineError::Failed("could not load Pocket TTS".into()))?;
        Ok(Self {
            id: id.into(),
            sample_rate: tts.sample_rate() as u32,
            tts,
            references: Mutex::new(HashMap::new()),
        })
    }

    /// Registers (or replaces) the reference recording for a cloned voice.
    pub fn set_voice(&self, voice: &str, reference: Audio) {
        self.references
            .lock()
            .unwrap()
            .insert(voice.to_string(), reference);
    }

    pub fn remove_voice(&self, voice: &str) {
        self.references.lock().unwrap().remove(voice);
    }
}

impl Engine for PocketEngine {
    fn id(&self) -> &str {
        &self.id
    }

    fn sample_rate(&self) -> u32 {
        self.sample_rate
    }

    fn voices(&self) -> Vec<String> {
        let mut v: Vec<String> = self.references.lock().unwrap().keys().cloned().collect();
        v.sort();
        v
    }

    fn synthesize_sentence(
        &self,
        sentence: &str,
        voice: &str,
        speed: f32,
    ) -> Result<Audio, EngineError> {
        let reference = self
            .references
            .lock()
            .unwrap()
            .get(voice)
            .cloned()
            .ok_or_else(|| EngineError::UnknownVoice(voice.into()))?;
        let cfg = GenerationConfig {
            speed,
            reference_audio: Some(reference.samples),
            reference_sample_rate: reference.sample_rate as i32,
            ..Default::default()
        };
        let out = self
            .tts
            .generate_with_config(sentence, &cfg, None::<fn(&[f32], f32) -> bool>)
            .ok_or_else(|| EngineError::Failed("Pocket TTS generation failed".into()))?;
        Ok(Audio::new(out.sample_rate() as u32, out.samples().to_vec()))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use voxa_core::SynthesisRequest;

    #[test]
    fn missing_files_are_named() {
        let d = tempfile::tempdir().unwrap();
        let err = PocketEngine::load("p", d.path(), 1).err().unwrap();
        assert!(err.to_string().contains("lm_flow"), "{err}");
    }

    /// `VOXA_POCKET_DIR=<package with test_wavs/> cargo test -p voxa-engines -- --ignored pocket`
    #[test]
    #[ignore = "needs the Pocket TTS model (VOXA_POCKET_DIR)"]
    fn pocket_speaks_in_a_registered_voice_only() {
        let dir = std::path::PathBuf::from(std::env::var("VOXA_POCKET_DIR").unwrap());
        let e = PocketEngine::load("p", &dir, 2).unwrap();
        let w = voxa_core::WavFile::parse(&std::fs::read(dir.join("test_wavs/bria.wav")).unwrap())
            .unwrap();
        let req = SynthesisRequest {
            text: "Hello. This is a test.".into(),
            voice: "me".into(),
            speed: 1.0,
        };
        assert!(matches!(
            e.synthesize(&req, &mut |_, _| {}),
            Err(EngineError::UnknownVoice(_))
        ));
        e.set_voice("me", Audio::new(w.sample_rate, w.channels[0].clone()));
        assert_eq!(e.voices(), ["me"]);
        let a = e.synthesize(&req, &mut |_, _| {}).unwrap();
        assert!(a.duration() > 0.8 && a.duration() < 6.0, "{}", a.duration());
        e.remove_voice("me");
        assert!(e.voices().is_empty());
    }
}
