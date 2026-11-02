//! "Your voices" (roadmap M9b/M9c): voices cloned from the user's own live recordings.
//!
//! A voice is created only after the consent check in `voxa_engines::consent` passes. Each
//! one lives in `<data>/cloned/<id>/` as `reference.wav` (the voice sample), `consent.json`
//! (the consent record: sentence, transcript, similarity, recording hashes) and
//! `voice.json` (name, creation time). Everything spoken with a cloned voice carries the
//! inaudible watermark and, in saved files, a "made with a cloned voice" label.

use std::path::{Path, PathBuf};
use std::sync::Mutex;

use serde::{Deserialize, Serialize};
use voxa_core::{Audio, Encoding, SynthesisRequest, WavFile};
use voxa_engines::consent::{Challenge, ConsentRecord};
use voxa_engines::watermark;

use crate::registry::{Registry, Voice};

/// Prefix of cloned voice ids, so they never clash with catalogue voices.
pub const ID_PREFIX: &str = "clone-";
/// The label written into every file made with a cloned voice (RIFF INFO comment).
pub const LABEL: &str =
    "Made with a cloned voice in Voxa. Contains the Voxa watermark (check it in Voxa).";
/// Silence before cloned speech, in seconds.
const LEAD_IN_SECONDS: f32 = 0.15;
/// The family shown in the voice list.
pub const FAMILY: &str = "Your voices";

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct ClonedVoice {
    pub id: String,
    pub name: String,
    /// Seconds since 1970 (UTC).
    pub created: u64,
}

/// The outcome of [`Cloning::create`]: the new voice, or why the consent check failed.
#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct CreateResult {
    pub voice: Option<ClonedVoice>,
    pub problems: Vec<String>,
}

/// The watermark check of a WAV file.
#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct WatermarkReport {
    pub found: bool,
    /// The best channel's detection score; [`watermark::DETECT_Z`] and above means found.
    pub score: f32,
    pub threshold: f32,
    /// The file carries Voxa's "cloned voice" label.
    pub labelled: bool,
    pub seconds: f64,
}

pub struct Cloning {
    dir: PathBuf,
    /// The challenge the person was last shown; a consent must answer this one.
    pending: Mutex<Option<Challenge>>,
}

/// Today's date as people say it ("2 November 2026"), from the system clock (UTC).
pub fn today() -> String {
    let secs = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    date_of(secs)
}

fn date_of(secs: u64) -> String {
    // Days to civil date (Howard Hinnant's algorithm).
    let z = (secs / 86_400) as i64 + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = yoe + era * 400 + i64::from(month <= 2);
    const MONTHS: [&str; 12] = [
        "January",
        "February",
        "March",
        "April",
        "May",
        "June",
        "July",
        "August",
        "September",
        "October",
        "November",
        "December",
    ];
    format!("{day} {} {year}", MONTHS[(month - 1) as usize])
}

fn now() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

/// A RIFF `LIST/INFO` chunk body with a comment (ICMT) and the software (ISFT).
fn info_chunk(comment: &str) -> Vec<u8> {
    let mut b = b"INFO".to_vec();
    for (id, text) in [(b"ICMT", comment), (b"ISFT", "Voxa")] {
        let mut t = text.as_bytes().to_vec();
        t.push(0);
        b.extend_from_slice(id);
        b.extend_from_slice(&(t.len() as u32).to_le_bytes());
        if t.len() % 2 == 1 {
            t.push(0);
        }
        b.extend_from_slice(&t);
    }
    b
}

/// A mono 16-bit WAV of cloned-voice audio, labelled as such.
pub fn labelled_wav(audio: &Audio) -> Vec<u8> {
    WavFile {
        sample_rate: audio.sample_rate,
        encoding: Encoding::Pcm16,
        channels: vec![audio.samples.clone()],
        extra_chunks: vec![(*b"LIST", info_chunk(LABEL))],
    }
    .to_bytes(Encoding::Pcm16)
}

/// Checks a WAV file for the watermark (any channel) and the label.
pub fn check_file(path: &Path) -> Result<WatermarkReport, String> {
    let bytes = std::fs::read(path).map_err(|e| format!("{}: {e}", path.display()))?;
    let wav = WavFile::parse(&bytes).map_err(|e| e.to_string())?;
    let score = wav
        .channels
        .iter()
        .map(|c| watermark::score(c, watermark::VOXA_KEY))
        .fold(0f32, f32::max);
    let labelled = wav.extra_chunks.iter().any(|(id, body)| {
        id == b"LIST" && body.windows(LABEL.len()).any(|w| w == LABEL.as_bytes())
    });
    Ok(WatermarkReport {
        found: score >= watermark::DETECT_Z,
        score,
        threshold: watermark::DETECT_Z,
        labelled,
        seconds: wav.duration(),
    })
}

/// Fresh randomness for a challenge's code words.
fn random_seed() -> u64 {
    let mut b = [0u8; 8];
    if getrandom::fill(&mut b).is_err() {
        // Never expected; the time still varies per attempt.
        return now() ^ u64::from(std::process::id()) << 32;
    }
    u64::from_le_bytes(b)
}

impl Cloning {
    pub fn open(data_root: &Path) -> Result<Self, String> {
        let dir = data_root.join("cloned");
        std::fs::create_dir_all(&dir).map_err(|e| format!("{}: {e}", dir.display()))?;
        Ok(Self {
            dir,
            pending: Mutex::new(None),
        })
    }

    /// A new consent sentence for `name`, with today's date and fresh code words.
    pub fn challenge(&self, name: &str) -> Result<Challenge, String> {
        let name = name.trim();
        if name.is_empty() || name.chars().count() > 60 {
            return Err("enter your name (up to 60 characters)".into());
        }
        if !name
            .chars()
            .all(|c| c.is_alphabetic() || c == ' ' || c == '-' || c == '\'')
        {
            return Err(
                "use letters only in your name, so it can be checked when you say it".into(),
            );
        }
        let c = Challenge::new(name, &today(), random_seed());
        *self.pending.lock().unwrap() = Some(c.clone());
        Ok(c)
    }

    /// Runs the consent check on the two live recordings and, if it passes, creates the
    /// voice. The challenge is used up either way, so every attempt gets new code words.
    pub fn create(
        &self,
        reg: &Registry,
        consent: &Audio,
        sample: &Audio,
    ) -> Result<CreateResult, String> {
        let challenge = self
            .pending
            .lock()
            .unwrap()
            .take()
            .ok_or("start again: ask for a new consent sentence first")?;
        if challenge.date != today() {
            return Err("the consent sentence has expired; start again".into());
        }
        // Make sure the voice model is installed before asking for anything else.
        reg.pocket()?;
        let checker = reg.consent_checker()?;
        let record = checker.check(
            &challenge,
            (&consent.samples, consent.sample_rate),
            (&sample.samples, sample.sample_rate),
        );
        if !record.passed {
            return Ok(CreateResult {
                voice: None,
                problems: record.problems,
            });
        }
        let id = format!("{ID_PREFIX}{:016x}", random_seed());
        let voice = ClonedVoice {
            id: id.clone(),
            name: challenge.name.clone(),
            created: now(),
        };
        self.save(&voice, sample, &record)?;
        Ok(CreateResult {
            voice: Some(voice),
            problems: Vec::new(),
        })
    }

    fn save(&self, v: &ClonedVoice, sample: &Audio, record: &ConsentRecord) -> Result<(), String> {
        let tmp = self.dir.join(format!(".{}.partial", v.id));
        let _ = std::fs::remove_dir_all(&tmp);
        std::fs::create_dir_all(&tmp).map_err(|e| e.to_string())?;
        let write = |name: &str, bytes: &[u8]| {
            std::fs::write(tmp.join(name), bytes).map_err(|e| format!("{name}: {e}"))
        };
        write(
            "reference.wav",
            &WavFile {
                sample_rate: sample.sample_rate,
                encoding: Encoding::Float32,
                channels: vec![sample.samples.clone()],
                extra_chunks: Vec::new(),
            }
            .to_bytes(Encoding::Float32),
        )?;
        write(
            "consent.json",
            &serde_json::to_vec_pretty(record).map_err(|e| e.to_string())?,
        )?;
        write(
            "voice.json",
            &serde_json::to_vec_pretty(v).map_err(|e| e.to_string())?,
        )?;
        std::fs::rename(&tmp, self.dir.join(&v.id)).map_err(|e| e.to_string())
    }

    fn voice_dir(&self, id: &str) -> Result<PathBuf, String> {
        let ok = id
            .strip_prefix(ID_PREFIX)
            .is_some_and(|h| !h.is_empty() && h.chars().all(|c| c.is_ascii_hexdigit()));
        let dir = self.dir.join(id);
        if !ok || !dir.join("voice.json").is_file() {
            return Err(format!("unknown voice {id:?}"));
        }
        Ok(dir)
    }

    /// The cloned voices, oldest first.
    pub fn list(&self) -> Vec<ClonedVoice> {
        let mut v: Vec<ClonedVoice> = std::fs::read_dir(&self.dir)
            .into_iter()
            .flatten()
            .flatten()
            .filter_map(|e| {
                let b = std::fs::read(e.path().join("voice.json")).ok()?;
                serde_json::from_slice(&b).ok()
            })
            .collect();
        v.sort_by(|a, b| (a.created, &a.id).cmp(&(b.created, &b.id)));
        v
    }

    /// Cloned voices as entries of the voice list, when the voice model is installed.
    pub fn voices(&self, reg: &Registry) -> Vec<Voice> {
        let Ok(model) = reg.pocket_model() else {
            return Vec::new();
        };
        self.list()
            .into_iter()
            .map(|c| Voice {
                id: c.id,
                family: FAMILY.into(),
                name: c.name,
                gender: String::new(),
                accent: String::new(),
                language: "en-US".into(),
                model: model.entry.id.clone(),
                model_version: model.entry.version.clone(),
                tier: model.entry.tier,
                credit: model.entry.license.credit.clone(),
                default: false,
                cloned: true,
                source: String::new(),
            })
            .collect()
    }

    /// Deletes a cloned voice: its recording, consent record and settings.
    pub fn remove(&self, reg: &Registry, id: &str) -> Result<(), String> {
        let dir = self.voice_dir(id)?;
        if let Ok(p) = reg.pocket() {
            p.remove_voice(id);
        }
        std::fs::remove_dir_all(&dir).map_err(|e| e.to_string())
    }

    /// Speaks with a cloned voice. The result is always watermarked.
    pub fn synthesize(
        &self,
        reg: &Registry,
        id: &str,
        text: &str,
        speed: f32,
    ) -> Result<Audio, String> {
        let dir = self.voice_dir(id)?;
        let engine = reg.pocket()?;
        if !voxa_core::Engine::voices(engine.as_ref())
            .iter()
            .any(|v| v == id)
        {
            let w = WavFile::parse(
                &std::fs::read(dir.join("reference.wav")).map_err(|e| e.to_string())?,
            )
            .map_err(|e| e.to_string())?;
            engine.set_voice(id, Audio::new(w.sample_rate, w.channels[0].clone()));
        }
        let req = SynthesisRequest {
            text: text.to_string(),
            voice: id.to_string(),
            speed,
        };
        let mut a = voxa_core::Engine::synthesize(engine.as_ref(), &req, &mut |_, _| {})
            .map_err(|e| e.to_string())?;
        // Pocket starts speaking on the first sample; a short lead-in keeps players and
        // speech recognisers from clipping the first word.
        let lead = (a.sample_rate as f32 * LEAD_IN_SECONDS) as usize;
        a.samples.splice(0..0, std::iter::repeat_n(0.0, lead));
        watermark::embed(&mut a.samples, watermark::VOXA_KEY);
        Ok(a)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const E2E_SENTENCES: [&str; 4] = [
        "Thank you for calling. Please leave a message after the tone.",
        "The meeting has moved to Thursday afternoon at noon.",
        "Turn left at the bakery and the station is on your right.",
        "We will send the new schedule to everyone by Friday.",
    ];
    const SHORT_SAMPLE: &str = "I've lived by the sea for most of my life. In the mornings I \
        walk the dog along the harbour, and in the evenings I read until it gets dark.";
    const LONG_SAMPLE: &str = "I've lived by the sea for most of my life. In the mornings I \
        walk the dog along the harbour, and in the evenings I read until it gets dark. My \
        favourite season is autumn, when the light turns gold.";

    fn norm_words(s: &str) -> Vec<String> {
        s.to_lowercase()
            .split(|c: char| !c.is_alphanumeric())
            .filter(|w| !w.is_empty())
            .map(str::to_string)
            .collect()
    }

    /// Word-level edit distance (substitutions, insertions, deletions).
    fn word_edits(a: &[String], b: &[String]) -> usize {
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
        prev[b.len()]
    }

    #[test]
    fn word_edits_counts_word_errors() {
        let w = |s: &str| norm_words(s);
        assert_eq!(
            word_edits(&w("thank you for calling"), &w("Thank you, for calling.")),
            0
        );
        assert_eq!(
            word_edits(&w("thank you for calling"), &w("but keep recalling")),
            4
        );
        assert_eq!(word_edits(&w("a b c"), &w("a c")), 1);
    }

    #[test]
    fn dates_read_naturally() {
        assert_eq!(date_of(0), "1 January 1970");
        assert_eq!(date_of(1_793_577_600), "2 November 2026");
        assert_eq!(date_of(951_782_400), "29 February 2000");
    }

    #[test]
    fn challenge_needs_a_sayable_name_and_is_used_once() {
        let d = tempfile::tempdir().unwrap();
        let c = Cloning::open(d.path()).unwrap();
        assert!(c.challenge("  ").is_err());
        assert!(c.challenge("R2-D2").unwrap_err().contains("letters"));
        let a = c.challenge("Sam O'Neil").unwrap();
        assert_eq!(a.date, today());
        assert!(a.sentence.contains("Sam O'Neil"));
        let b = c.challenge("Sam O'Neil").unwrap();
        assert_ne!(a.words, b.words, "fresh code words every time");
    }

    #[test]
    fn create_without_a_challenge_or_models_fails_cleanly() {
        let d = tempfile::tempdir().unwrap();
        let reg = Registry::open(d.path(), crate::registry::trusted_keys()).unwrap();
        let c = Cloning::open(d.path()).unwrap();
        let a = Audio::new(16_000, vec![0.0; 16_000 * 12]);
        assert!(c
            .create(&reg, &a, &a)
            .unwrap_err()
            .contains("consent sentence"));
        c.challenge("Sam").unwrap();
        let err = c.create(&reg, &a, &a).unwrap_err();
        assert!(err.contains("not installed"), "{err}");
        // The challenge was used up by that attempt.
        assert!(c
            .create(&reg, &a, &a)
            .unwrap_err()
            .contains("consent sentence"));
        assert!(c.voices(&reg).is_empty());
    }

    #[test]
    fn ids_cannot_escape_the_store() {
        let d = tempfile::tempdir().unwrap();
        let c = Cloning::open(d.path()).unwrap();
        for id in ["../x", "clone-", "clone-../../etc", "harper"] {
            assert!(c.voice_dir(id).is_err(), "{id}");
        }
    }

    #[test]
    fn labelled_wav_is_found_by_the_checker() {
        let d = tempfile::tempdir().unwrap();
        let mut a = Audio::new(
            24_000,
            (0..24_000 * 4)
                .map(|i| 0.2 * (i as f32 * 0.03).sin() * (i as f32 * 0.0007).sin())
                .collect(),
        );
        let plain = d.path().join("plain.wav");
        std::fs::write(&plain, a.to_wav(voxa_core::SampleFormat::Pcm16)).unwrap();
        watermark::embed(&mut a.samples, watermark::VOXA_KEY);
        let marked = d.path().join("marked.wav");
        std::fs::write(&marked, labelled_wav(&a)).unwrap();

        let r = check_file(&marked).unwrap();
        assert!(r.found && r.labelled, "{r:?}");
        let r = check_file(&plain).unwrap();
        assert!(!r.found && !r.labelled, "{r:?}");
        // The label survives a round trip through the WAV parser (other tools keep LIST).
        let w = WavFile::parse(&std::fs::read(&marked).unwrap()).unwrap();
        assert_eq!(w.extra_chunks[0].0, *b"LIST");
    }

    /// End to end with the real models. Pocket is sampled, and in Voxa's checks it dropped
    /// a short opening word in about 1 of 12 generations ("Hello there." -> "There."), so a
    /// rare failure on the transcription step is that Preview limitation, not a new bug.
    /// Kokoro stands in for a person (the same voice reads
    /// the consent sentence and the sample):
    /// `VOXA_CLONE_PACKS=<pocket pack>:<consent pack>:<kokoro pack> \
    ///  cargo test -p voxa-app -- --ignored cloning_e2e`
    #[test]
    #[ignore = "needs signed Pocket, consent and Kokoro packs (VOXA_CLONE_PACKS)"]
    fn cloning_e2e_consent_create_speak_watermark_remove() {
        let d = tempfile::tempdir().unwrap();
        let reg = Registry::open(d.path(), crate::registry::trusted_keys()).unwrap();
        let packs = std::env::var("VOXA_CLONE_PACKS").unwrap();
        for p in packs.split(':') {
            reg.import(Path::new(p)).unwrap();
        }
        let c = Cloning::open(d.path()).unwrap();
        let person = "harper";
        let sample = reg.synthesize(person, LONG_SAMPLE, 1.0).unwrap();
        assert!(sample.duration() >= 10.0, "{}", sample.duration());

        // Reading out an old sentence (other code words) fails, and uses the challenge up.
        let stale = Challenge::new("Sam Taylor", &today(), 42);
        let fresh = c.challenge("Sam Taylor").unwrap();
        if fresh.words != stale.words {
            let old = reg.synthesize(person, &stale.sentence, 1.0).unwrap();
            let r = c.create(&reg, &old, &sample).unwrap();
            assert!(r.voice.is_none());
            assert!(
                r.problems.iter().any(|p| p.contains("weren't heard")),
                "{:?}",
                r.problems
            );
        }

        let ch = c.challenge("Sam Taylor").unwrap();
        let consent = reg.synthesize(person, &ch.sentence, 1.0).unwrap();
        let r = c.create(&reg, &consent, &sample).unwrap();
        assert!(r.problems.is_empty(), "{:?}", r.problems);
        let v = r.voice.unwrap();
        let listed = c.voices(&reg);
        assert_eq!(listed.len(), 1);
        assert_eq!(
            (listed[0].name.as_str(), listed[0].cloned),
            ("Sam Taylor", true)
        );
        let dir = d.path().join("cloned").join(&v.id);
        let record: serde_json::Value =
            serde_json::from_slice(&std::fs::read(dir.join("consent.json")).unwrap()).unwrap();
        assert_eq!(record["passed"], true);

        // Intelligibility: the word error rate over several sentences, as in the round-trip
        // quality gate. Pocket is sampled, so a single clip can slur a word ("thank you"
        // heard as "but keep" at full length); the gate is on the rate, not one clip.
        let sentences = E2E_SENTENCES;
        let asr = reg.consent_checker().unwrap();
        let (mut errors, mut total, mut worst) = (0usize, 0usize, 0f32);
        for (k, text) in sentences.iter().enumerate() {
            let a = c.synthesize(&reg, &v.id, text, 1.0).unwrap();
            if k == 0 {
                let out = d.path().join("message.wav");
                std::fs::write(&out, labelled_wav(&a)).unwrap();
                let rep = check_file(&out).unwrap();
                assert!(rep.found && rep.labelled, "{rep:?}");
                worst = rep.score;
            }
            let heard = asr.transcribe(&a.samples, a.sample_rate);
            let (want, got) = (norm_words(text), norm_words(&heard));
            let e = word_edits(&want, &got);
            eprintln!(
                "clone said {text:?}, heard {heard:?} ({e} errors, {:.2} s)",
                a.duration()
            );
            errors += e;
            total += want.len();
        }
        let wer = errors as f32 / total as f32;
        eprintln!(
            "clone: similarity {:.2}, watermark score {worst:.1}, WER {:.1}%",
            record["similarity"],
            100.0 * wer
        );
        assert!(wer <= 0.15, "word error rate {:.1}%", 100.0 * wer);

        c.remove(&reg, &v.id).unwrap();
        assert!(c.voices(&reg).is_empty());
        assert!(!dir.exists());
    }

    /// Diagnostic: how often each code word, and the first word of a cloned sentence, is
    /// heard. `VOXA_CLONE_PACKS=... cargo test -p voxa-app -- --ignored code_word_sweep`
    #[test]
    #[ignore = "diagnostic; needs VOXA_CLONE_PACKS"]
    fn code_word_sweep() {
        let d = tempfile::tempdir().unwrap();
        let reg = Registry::open(d.path(), crate::registry::trusted_keys()).unwrap();
        for p in std::env::var("VOXA_CLONE_PACKS").unwrap().split(':') {
            reg.import(Path::new(p)).unwrap();
        }
        let asr = reg.consent_checker().unwrap();
        let words = voxa_engines::consent::CODE_WORDS;
        let all = reg.voices().unwrap();
        let voices: Vec<&str> = all
            .iter()
            .step_by(3)
            .take(8)
            .map(|v| v.id.as_str())
            .collect();
        let mut misses: std::collections::BTreeMap<&str, Vec<&str>> = Default::default();
        for (vi, v) in voices.iter().enumerate() {
            for k in (0..words.len()).step_by(3) {
                let tri: Vec<&str> = (0..3).map(|j| words[(k + j + vi) % words.len()]).collect();
                let ch = Challenge {
                    name: "Sam Taylor".into(),
                    date: today(),
                    words: [tri[0], tri[1], tri[2]].map(str::to_string),
                    sentence: format!(
                        "I am Sam Taylor, and today, {}, I agree that Voxa may make a copy of my voice. My code words are {}, {}, {}.",
                        today(), tri[0], tri[1], tri[2]
                    ),
                };
                let a = reg.synthesize(v, &ch.sentence, 1.0).unwrap();
                let heard = asr.transcribe(&a.samples, a.sample_rate);
                for m in voxa_engines::consent::missing_words(&ch, &heard) {
                    eprintln!("{v}: missing {m:?} in {heard:?}");
                    misses
                        .entry(words.iter().find(|w| **w == m).copied().unwrap_or("name"))
                        .or_default()
                        .push(v);
                }
            }
        }
        eprintln!("MISSES {misses:?}");
    }

    /// Diagnostic: how intelligible are Pocket's clones, by reference? Speaks sentences in a
    /// voice made from `VOXA_POCKET_REF` (a WAV, or `kokoro:<voice>` to use a Kokoro voice as
    /// the person, read over a short and a long sample) and prints the word error rate.
    #[test]
    #[ignore = "diagnostic; needs VOXA_CLONE_PACKS (pocket:consent[:kokoro]) and VOXA_POCKET_REF"]
    fn pocket_wer() {
        let d = tempfile::tempdir().unwrap();
        let reg = Registry::open(d.path(), crate::registry::trusted_keys()).unwrap();
        for p in std::env::var("VOXA_CLONE_PACKS").unwrap().split(':') {
            reg.import(Path::new(p)).unwrap();
        }
        let src = std::env::var("VOXA_POCKET_REF").unwrap();
        let references: Vec<(&str, Audio)> = match src.strip_prefix("kokoro:") {
            Some(v) => [("short", SHORT_SAMPLE), ("long", LONG_SAMPLE)]
                .into_iter()
                .map(|(n, t)| (n, reg.synthesize(v, t, 1.0).unwrap()))
                .collect(),
            None => {
                let w = WavFile::parse(&std::fs::read(&src).unwrap()).unwrap();
                vec![("file", Audio::new(w.sample_rate, w.channels[0].clone()))]
            }
        };
        let e = reg.pocket().unwrap();
        let asr = reg.consent_checker().unwrap();
        for (name, reference) in references {
            let secs = reference.duration();
            e.set_voice("r", reference);
            let (mut errors, mut total, mut marked_errors) = (0, 0, 0);
            for _ in 0..3 {
                for t in E2E_SENTENCES {
                    let req = SynthesisRequest {
                        text: t.into(),
                        voice: "r".into(),
                        speed: 1.0,
                    };
                    let mut a =
                        voxa_core::Engine::synthesize(e.as_ref(), &req, &mut |_, _| {}).unwrap();
                    a.samples
                        .splice(0..0, std::iter::repeat_n(0.0, a.sample_rate as usize / 5));
                    let heard = asr.transcribe(&a.samples, a.sample_rate);
                    let n = word_edits(&norm_words(t), &norm_words(&heard));
                    errors += n;
                    total += norm_words(t).len();
                    if n > 0 {
                        eprintln!("POCKET [{name}] {t:?} -> {heard:?}");
                    }
                    // The same clip, watermarked as the app ships it (a paired comparison).
                    watermark::embed(&mut a.samples, watermark::VOXA_KEY);
                    let heard = asr.transcribe(&a.samples, a.sample_rate);
                    let n = word_edits(&norm_words(t), &norm_words(&heard));
                    marked_errors += n;
                    if n > 0 {
                        eprintln!("POCKET [{name}, watermarked] {t:?} -> {heard:?}");
                    }
                }
            }
            eprintln!(
                "POCKET [{name}, reference {secs:.1} s] WER {:.1}% ({errors}/{total}), \
                 watermarked {:.1}% ({marked_errors}/{total})",
                100.0 * errors as f32 / total as f32,
                100.0 * marked_errors as f32 / total as f32
            );
        }
    }
}
