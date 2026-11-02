//! "Clean up audio" (roadmap M8): open a WAV, clean it once, then audition and save with
//! any strength without re-running the model.
//!
//! Saved files keep the input's sample rate, channel count, exact length and every extra
//! chunk (BWF `bext` with its timecode reference, `iXML`), and are written as 24-bit PCM,
//! so the cleaned dialogue drops onto an editor's timeline in sync.

use std::path::Path;
use std::sync::{Arc, Mutex};

use serde::Serialize;
use voxa_core::{Encoding, WavFile};
use voxa_engines::mix;

use crate::registry::Registry;

/// Longest preview served to the A/B player, in seconds.
pub const PREVIEW_SECONDS: f64 = 30.0;

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct OpenedFile {
    pub name: String,
    pub sample_rate: u32,
    pub channels: usize,
    pub seconds: f64,
    /// Chunks that will be carried over, e.g. ["bext", "iXML"].
    pub kept_chunks: Vec<String>,
}

struct Session {
    name: String,
    wav: WavFile,
    /// (model id, cleaned channels) once cleaned.
    cleaned: Option<(String, Vec<Vec<f32>>)>,
}

#[derive(Default)]
pub struct Cleanup {
    session: Mutex<Option<Session>>,
}

impl Cleanup {
    pub fn open(&self, path: &Path) -> Result<OpenedFile, String> {
        let bytes = std::fs::read(path).map_err(|e| format!("{}: {e}", path.display()))?;
        let wav = WavFile::parse(&bytes).map_err(|e| e.to_string())?;
        if wav.frames() == 0 {
            return Err("the file has no audio".into());
        }
        let name = path
            .file_name()
            .map(|n| n.to_string_lossy().into_owned())
            .unwrap_or_default();
        let info = OpenedFile {
            name: name.clone(),
            sample_rate: wav.sample_rate,
            channels: wav.channels.len(),
            seconds: wav.duration(),
            kept_chunks: wav
                .extra_chunks
                .iter()
                .map(|(id, _)| String::from_utf8_lossy(id).trim().to_string())
                .collect(),
        };
        *self.session.lock().unwrap() = Some(Session {
            name,
            wav,
            cleaned: None,
        });
        Ok(info)
    }

    /// Cleans every channel with the chosen model. `on_progress(done, total)` counts
    /// channels.
    pub fn run(
        &self,
        reg: &Registry,
        model: &str,
        on_progress: &mut dyn FnMut(usize, usize),
    ) -> Result<(), String> {
        let (channels, rate) = {
            let s = self.session.lock().unwrap();
            let s = s.as_ref().ok_or("open a WAV file first")?;
            (s.wav.channels.clone(), s.wav.sample_rate)
        };
        let d = reg.denoiser(model)?;
        let total = channels.len();
        on_progress(0, total);
        let mut cleaned = Vec::with_capacity(total);
        for (i, ch) in channels.iter().enumerate() {
            cleaned.push(d.clean(ch, rate).map_err(|e| e.to_string())?);
            on_progress(i + 1, total);
        }
        let mut s = self.session.lock().unwrap();
        let s = s.as_mut().ok_or("the file was closed")?;
        s.cleaned = Some((model.to_string(), cleaned));
        Ok(())
    }

    /// The channels to play or save: the original, or cleaned mixed at `strength`.
    fn render(s: &Session, cleaned: bool, strength: f32) -> Result<Vec<Vec<f32>>, String> {
        if !cleaned {
            return Ok(s.wav.channels.clone());
        }
        let (_, c) = s.cleaned.as_ref().ok_or("clean the file first")?;
        Ok(s.wav
            .channels
            .iter()
            .zip(c)
            .map(|(o, c)| mix(o, c, strength))
            .collect())
    }

    /// A 16-bit WAV of up to [`PREVIEW_SECONDS`] from `start` seconds, for the A/B player.
    pub fn preview(&self, cleaned: bool, strength: f32, start: f64) -> Result<Vec<u8>, String> {
        let s = self.session.lock().unwrap();
        let s = s.as_ref().ok_or("open a WAV file first")?;
        let rate = s.wav.sample_rate as f64;
        let from = ((start.max(0.0) * rate) as usize).min(s.wav.frames());
        let to = (from + (PREVIEW_SECONDS * rate) as usize).min(s.wav.frames());
        let channels = Self::render(s, cleaned, strength)?
            .into_iter()
            .map(|c| c[from..to].to_vec())
            .collect();
        Ok(WavFile {
            sample_rate: s.wav.sample_rate,
            encoding: Encoding::Pcm16,
            channels,
            extra_chunks: Vec::new(),
        }
        .to_bytes(Encoding::Pcm16))
    }

    /// Writes the cleaned file: same rate, channels, length and extra chunks; 24-bit PCM.
    pub fn save(&self, path: &Path, strength: f32) -> Result<(), String> {
        let bytes = {
            let s = self.session.lock().unwrap();
            let s = s.as_ref().ok_or("open a WAV file first")?;
            WavFile {
                sample_rate: s.wav.sample_rate,
                encoding: Encoding::Pcm24,
                channels: Self::render(s, true, strength)?,
                extra_chunks: s.wav.extra_chunks.clone(),
            }
            .to_bytes(Encoding::Pcm24)
        };
        std::fs::write(path, bytes).map_err(|e| format!("{}: {e}", path.display()))
    }

    /// Suggested file name for the cleaned copy: "take3.wav" -> "take3 (cleaned).wav".
    pub fn suggested_name(&self) -> Option<String> {
        let s = self.session.lock().unwrap();
        let n = &s.as_ref()?.name;
        let stem = n
            .strip_suffix(".wav")
            .or_else(|| n.strip_suffix(".WAV"))
            .unwrap_or(n);
        Some(format!("{stem} (cleaned).wav"))
    }
}

pub type Shared = Arc<Cleanup>;

#[cfg(test)]
mod tests {
    use super::*;

    fn write_take(dir: &Path) -> std::path::PathBuf {
        let w = WavFile {
            sample_rate: 48_000,
            encoding: Encoding::Pcm24,
            channels: vec![
                (0..48_123).map(|i| (i as f32 * 0.02).sin() * 0.3).collect(),
                (0..48_123).map(|i| (i as f32 * 0.05).sin() * 0.2).collect(),
            ],
            extra_chunks: vec![(*b"bext", b"Scene 12A take 3 TC 01:00:00:00".to_vec())],
        };
        let p = dir.join("take3.wav");
        std::fs::write(&p, w.to_bytes(Encoding::Pcm24)).unwrap();
        p
    }

    #[test]
    fn opens_previews_and_names() {
        let d = tempfile::tempdir().unwrap();
        let c = Cleanup::default();
        let info = c.open(&write_take(d.path())).unwrap();
        assert_eq!((info.sample_rate, info.channels), (48_000, 2));
        assert_eq!(info.kept_chunks, ["bext"]);
        assert!((info.seconds - 48_123.0 / 48_000.0).abs() < 1e-9);
        let p = WavFile::parse(&c.preview(false, 1.0, 0.5).unwrap()).unwrap();
        assert_eq!(p.frames(), 48_123 - 24_000);
        assert!(c
            .preview(true, 1.0, 0.0)
            .unwrap_err()
            .contains("clean the file first"));
        assert_eq!(c.suggested_name().unwrap(), "take3 (cleaned).wav");
    }

    #[test]
    fn refuses_non_wav_and_actions_before_open() {
        let d = tempfile::tempdir().unwrap();
        let c = Cleanup::default();
        assert!(c.save(&d.path().join("x.wav"), 1.0).is_err());
        let bad = d.path().join("notes.wav");
        std::fs::write(&bad, b"not audio").unwrap();
        assert!(c.open(&bad).unwrap_err().contains("not a WAV"));
    }

    /// `VOXA_DPDFNET_PACK=<signed dev pack> cargo test -p voxa-app -- --ignored cleanup_e2e`
    #[test]
    #[ignore = "needs a signed DPDFNet clean-up pack (VOXA_DPDFNET_PACK)"]
    fn cleanup_e2e_keeps_length_channels_rate_and_chunks() {
        let d = tempfile::tempdir().unwrap();
        let reg = Registry::open(d.path(), crate::registry::trusted_keys()).unwrap();
        reg.import(Path::new(&std::env::var("VOXA_DPDFNET_PACK").unwrap()))
            .unwrap();
        let models = reg.cleanup_models().unwrap();
        assert_eq!(models[0].kind, "studio");
        let c = Cleanup::default();
        let src = write_take(d.path());
        c.open(&src).unwrap();
        let mut seen = Vec::new();
        c.run(&reg, &models[0].id, &mut |a, b| seen.push((a, b)))
            .unwrap();
        assert_eq!(seen, vec![(0, 2), (1, 2), (2, 2)]);
        let out = d.path().join("take3 (cleaned).wav");
        c.save(&out, 0.8).unwrap();
        let a = WavFile::parse(&std::fs::read(&src).unwrap()).unwrap();
        let b = WavFile::parse(&std::fs::read(&out).unwrap()).unwrap();
        assert_eq!(
            (b.sample_rate, b.channels.len(), b.frames(), b.encoding),
            (a.sample_rate, a.channels.len(), a.frames(), Encoding::Pcm24)
        );
        assert_eq!(b.extra_chunks, a.extra_chunks, "bext/timecode must survive");
        // Strength 0 gives the original back (to 24-bit precision).
        c.save(&out, 0.0).unwrap();
        let z = WavFile::parse(&std::fs::read(&out).unwrap()).unwrap();
        for (x, y) in a.channels[0].iter().zip(&z.channels[0]) {
            assert!((x - y).abs() < 1e-6);
        }
    }
}
