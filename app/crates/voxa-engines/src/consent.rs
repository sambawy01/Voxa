//! Consent checks for voice cloning (roadmap M9a). Cloning runs on the device, so Voxa
//! can't moderate it afterwards; these checks happen before a voice is created.
//!
//! A clone needs two **live** recordings of the same person:
//! 1. the **consent sentence**, read aloud: it contains their name, today's date and three
//!    random code words, so a recording made for anything else, or an old one, won't pass;
//! 2. the **voice sample** the clone is made from.
//!
//! The consent passes only if speech recognition (Whisper tiny.en, MIT) hears the name and
//! every code word, and the speaker embedding (3D-Speaker CAM++, Apache-2.0) of the two
//! recordings matches. On real recordings the same speaker scored 0.92-0.97 and different
//! speakers 0.13-0.62 in Voxa's checks, so the threshold is 0.75. The embedding can't tell
//! synthetic voices apart well (0.82-0.91 between different TTS voices), which is why the
//! code words and live recording carry the weight.

use std::path::Path;

use serde::Serialize;
use sha2::{Digest, Sha256};
use sherpa_onnx::{
    OfflineRecognizer, OfflineRecognizerConfig, SpeakerEmbeddingExtractor,
    SpeakerEmbeddingExtractorConfig,
};
use voxa_core::EngineError;

pub const SAME_SPEAKER_THRESHOLD: f32 = 0.75;
pub const MIN_CONSENT_SECONDS: f32 = 4.0;
pub const MIN_SAMPLE_SECONDS: f32 = 10.0;

/// Short, distinct, easy-to-say words for the code. Avoids homophones ("two"/"too").
const CODE_WORDS: &[&str] = &[
    "amber", "banjo", "canyon", "dolphin", "ember", "falcon", "garden", "harbor", "igloo",
    "jungle", "kettle", "lantern", "meadow", "nectar", "orbit", "pepper", "quartz", "rocket",
    "saddle", "tiger", "umbrella", "velvet", "walnut", "yellow", "zebra", "copper", "violin",
    "marble", "pebble", "thunder",
];

/// What the person must read aloud.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Challenge {
    pub name: String,
    pub date: String,
    pub words: [String; 3],
    pub sentence: String,
}

impl Challenge {
    /// `seed` should be random per attempt (the app uses fresh OS randomness).
    pub fn new(name: &str, date: &str, seed: u64) -> Self {
        let mut s = seed | 1;
        let mut pick = |taken: &[usize]| loop {
            // xorshift64*
            s ^= s >> 12;
            s ^= s << 25;
            s ^= s >> 27;
            let i = (s.wrapping_mul(0x2545_F491_4F6C_DD1D) % CODE_WORDS.len() as u64) as usize;
            if !taken.contains(&i) {
                return i;
            }
        };
        let a = pick(&[]);
        let b = pick(&[a]);
        let c = pick(&[a, b]);
        let words = [CODE_WORDS[a], CODE_WORDS[b], CODE_WORDS[c]].map(str::to_string);
        let name = name.trim().to_string();
        let sentence = format!(
            "I am {name}, and today, {date}, I agree that Voxa may make a copy of my voice. \
             My code words are {}, {}, {}.",
            words[0], words[1], words[2]
        );
        Self {
            name,
            date: date.into(),
            words,
            sentence,
        }
    }
}

/// The outcome of a consent check, kept with the cloned voice.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct ConsentRecord {
    pub passed: bool,
    /// Why it failed, in plain words (empty when it passed).
    pub problems: Vec<String>,
    pub challenge: Challenge,
    pub transcript: String,
    pub similarity: f32,
    pub consent_seconds: f32,
    pub sample_seconds: f32,
    /// sha256 of the two recordings (16 kHz float samples), so the record is tied to them.
    pub consent_sha256: String,
    pub sample_sha256: String,
}

fn words_of(s: &str) -> Vec<String> {
    s.to_lowercase()
        .split(|c: char| !c.is_alphanumeric())
        .filter(|w| !w.is_empty())
        .map(str::to_string)
        .collect()
}

/// Edit distance, for forgiving one-letter recognition slips ("lanterns", "harbour").
fn close(a: &str, b: &str) -> bool {
    if a == b {
        return true;
    }
    let (a, b): (Vec<char>, Vec<char>) = (a.chars().collect(), b.chars().collect());
    let mut prev: Vec<usize> = (0..=b.len()).collect();
    for (i, x) in a.iter().enumerate() {
        let mut cur = vec![i + 1];
        for (j, y) in b.iter().enumerate() {
            cur.push(
                (prev[j] + usize::from(x != y))
                    .min(prev[j + 1] + 1)
                    .min(cur[j] + 1),
            );
        }
        prev = cur;
    }
    prev[b.len()] <= 1 + usize::from(a.len().max(b.len()) >= 7)
}

/// Which of the challenge's required words are missing from a transcript.
pub fn missing_words(challenge: &Challenge, transcript: &str) -> Vec<String> {
    let heard = words_of(transcript);
    let mut required: Vec<String> = challenge.words.to_vec();
    required.extend(words_of(&challenge.name));
    required
        .into_iter()
        .filter(|w| !heard.iter().any(|h| close(h, w)))
        .collect()
}

pub fn cosine(a: &[f32], b: &[f32]) -> f32 {
    let dot: f32 = a.iter().zip(b).map(|(x, y)| x * y).sum();
    let n = |v: &[f32]| v.iter().map(|x| x * x).sum::<f32>().sqrt();
    dot / (n(a) * n(b)).max(1e-9)
}

fn sha(samples: &[f32]) -> String {
    let mut h = Sha256::new();
    for s in samples {
        h.update(s.to_le_bytes());
    }
    hex::encode(h.finalize())
}

pub struct ConsentChecker {
    asr: OfflineRecognizer,
    speaker: SpeakerEmbeddingExtractor,
}

impl ConsentChecker {
    /// `dir` holds `tiny.en-encoder.int8.onnx`, `tiny.en-decoder.int8.onnx`,
    /// `tiny.en-tokens.txt` and `campplus_en_voxceleb_16k.onnx`.
    pub fn load(dir: &Path, threads: i32) -> Result<Self, EngineError> {
        let need = |n: &str| -> Result<String, EngineError> {
            let p = dir.join(n);
            if p.is_file() {
                Ok(p.to_string_lossy().into_owned())
            } else {
                Err(EngineError::Failed(format!(
                    "consent package is missing {n}"
                )))
            }
        };
        let mut rc = OfflineRecognizerConfig::default();
        rc.model_config.whisper.encoder = Some(need("tiny.en-encoder.int8.onnx")?);
        rc.model_config.whisper.decoder = Some(need("tiny.en-decoder.int8.onnx")?);
        rc.model_config.tokens = Some(need("tiny.en-tokens.txt")?);
        rc.model_config.num_threads = threads.max(1);
        let asr = OfflineRecognizer::create(&rc)
            .ok_or_else(|| EngineError::Failed("could not load speech recognition".into()))?;
        let ec = SpeakerEmbeddingExtractorConfig {
            model: Some(need("campplus_en_voxceleb_16k.onnx")?),
            num_threads: threads.max(1),
            ..Default::default()
        };
        let speaker = SpeakerEmbeddingExtractor::create(&ec)
            .ok_or_else(|| EngineError::Failed("could not load the speaker model".into()))?;
        Ok(Self { asr, speaker })
    }

    pub fn transcribe(&self, samples: &[f32], rate: u32) -> String {
        let s = self.asr.create_stream();
        s.accept_waveform(rate as i32, samples);
        self.asr.decode(&s);
        s.get_result()
            .map(|r| r.text.trim().to_string())
            .unwrap_or_default()
    }

    pub fn embedding(&self, samples: &[f32], rate: u32) -> Option<Vec<f32>> {
        let s = self.speaker.create_stream()?;
        s.accept_waveform(rate as i32, samples);
        s.input_finished();
        self.speaker.compute(&s)
    }

    /// Checks a consent recording against its challenge and the voice sample.
    pub fn check(
        &self,
        challenge: &Challenge,
        consent: (&[f32], u32),
        sample: (&[f32], u32),
    ) -> ConsentRecord {
        let mut problems = Vec::new();
        let consent_seconds = consent.0.len() as f32 / consent.1.max(1) as f32;
        let sample_seconds = sample.0.len() as f32 / sample.1.max(1) as f32;
        if consent_seconds < MIN_CONSENT_SECONDS {
            problems.push(format!(
                "the consent recording is too short ({consent_seconds:.1} s); read the whole sentence"
            ));
        }
        if sample_seconds < MIN_SAMPLE_SECONDS {
            problems.push(format!(
                "the voice sample is too short ({sample_seconds:.1} s); record at least {MIN_SAMPLE_SECONDS:.0} s"
            ));
        }
        let transcript = self.transcribe(consent.0, consent.1);
        let missing = missing_words(challenge, &transcript);
        if !missing.is_empty() {
            problems.push(format!(
                "these words weren't heard in the consent recording: {}",
                missing.join(", ")
            ));
        }
        let similarity = match (
            self.embedding(consent.0, consent.1),
            self.embedding(sample.0, sample.1),
        ) {
            (Some(a), Some(b)) => cosine(&a, &b),
            _ => 0.0,
        };
        if similarity < SAME_SPEAKER_THRESHOLD {
            problems.push(
                "the consent recording and the voice sample don't sound like the same person"
                    .into(),
            );
        }
        ConsentRecord {
            passed: problems.is_empty(),
            problems,
            challenge: challenge.clone(),
            transcript,
            similarity,
            consent_seconds,
            sample_seconds,
            consent_sha256: sha(consent.0),
            sample_sha256: sha(sample.0),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn challenges_have_three_distinct_words_and_vary() {
        let a = Challenge::new("Sam Lee", "November 2, 2026", 1);
        let b = Challenge::new("Sam Lee", "November 2, 2026", 2);
        assert_ne!(a.words, b.words);
        let mut w = a.words.to_vec();
        w.sort();
        w.dedup();
        assert_eq!(w.len(), 3);
        assert!(a.sentence.contains("Sam Lee") && a.sentence.contains("November 2, 2026"));
        for word in &a.words {
            assert!(a.sentence.contains(word.as_str()));
        }
    }

    #[test]
    fn missing_words_tolerates_small_recognition_slips() {
        let mut c = Challenge::new("Sam", "today", 7);
        c.words = ["lantern".into(), "harbor".into(), "tiger".into()];
        assert!(missing_words(&c, "I am Sam ... lanterns, harbour, tiger.").is_empty());
        assert_eq!(
            missing_words(&c, "I am Sam. Lantern, tiger."),
            vec!["harbor"]
        );
        assert_eq!(
            missing_words(&c, "lantern harbor tiger"),
            vec!["sam"],
            "the name is required too"
        );
        // A different word ("tigress" is 3 edits from "tiger") is not accepted.
        assert_eq!(
            missing_words(&c, "lantern harbor tigress sam"),
            vec!["tiger"]
        );
    }

    #[test]
    fn cosine_basics() {
        assert!((cosine(&[1.0, 0.0], &[1.0, 0.0]) - 1.0).abs() < 1e-6);
        assert!(cosine(&[1.0, 0.0], &[0.0, 1.0]).abs() < 1e-6);
    }

    /// `VOXA_CONSENT_DIR=<pack> VOXA_POCKET_REFS=<dir with bria.wav, loona.wav>
    ///  cargo test -p voxa-engines -- --ignored consent`
    #[test]
    #[ignore = "needs the consent models (VOXA_CONSENT_DIR) and reference speech"]
    fn consent_check_on_real_speech() {
        let c = ConsentChecker::load(Path::new(&std::env::var("VOXA_CONSENT_DIR").unwrap()), 2)
            .unwrap();
        let refs = std::path::PathBuf::from(std::env::var("VOXA_POCKET_REFS").unwrap());
        let read = |n: &str| {
            let w = voxa_core::WavFile::parse(&std::fs::read(refs.join(n)).unwrap()).unwrap();
            (w.channels[0].clone(), w.sample_rate)
        };
        let (bria, r) = read("bria.wav");
        let (hibiki, rh) = read("sample_fr_hibiki_crepes.wav");
        // The recognised text must contain the name and the code words, so build the
        // challenge from what this recording actually says.
        let first = &bria[..bria.len() / 2];
        let heard = c.transcribe(first, r);
        let words: Vec<String> = words_of(&heard)
            .into_iter()
            .filter(|w| w.len() >= 4)
            .collect();
        assert!(words.len() >= 4, "transcript too short: {heard:?}");
        let mut ch = Challenge::new(&words[0], "today", 3);
        ch.words = [words[1].clone(), words[2].clone(), words[3].clone()];

        let same = c.check(&ch, (first, r), (&bria[bria.len() / 2..], r));
        assert!(same.passed, "{:?}", same.problems);
        assert!(same.similarity > 0.9, "{}", same.similarity);

        let other = c.check(&ch, (first, r), (&hibiki, rh));
        assert!(!other.passed);
        assert!(
            other.similarity < SAME_SPEAKER_THRESHOLD,
            "{}",
            other.similarity
        );

        let mut wrong = ch.clone();
        wrong.words = ["zebra".into(), "quartz".into(), "igloo".into()];
        let replay = c.check(&wrong, (first, r), (&bria[bria.len() / 2..], r));
        assert!(
            !replay.passed,
            "a recording without the code words must fail"
        );
        assert!(replay.problems.iter().any(|p| p.contains("weren't heard")));
    }
}
