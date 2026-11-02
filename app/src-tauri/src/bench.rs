//! The device benchmark (roadmap M5): how fast and how heavy each installed model is on
//! this device. The numbers become the measured requirements on voice cards and in the
//! catalogue (`requirements.measured`), replacing the targets in the plan.

use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use serde::Serialize;

use crate::registry::Registry;

/// The fixed paragraph every run speaks, so results compare across devices.
pub const BENCH_TEXT: &str = "Voxa reads your text aloud on this device. The quick brown fox \
jumps over the lazy dog, and the five boxing wizards jump quickly. On March 4th, 2026, the \
jacket cost $49.99, about 20% less than before. Nothing you type ever leaves your device.";

/// The first sentence of [`BENCH_TEXT`], used for time to first audio.
const FIRST_SENTENCE: &str = "Voxa reads your text aloud on this device.";

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct Device {
    pub os: String,
    pub arch: String,
    pub cpus: usize,
    pub cpu: String,
    pub ram_mb: u64,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct ModelResult {
    pub model: String,
    pub version: String,
    pub family: String,
    pub voice: String,
    /// Loading the model from disk, cold.
    pub load_ms: u64,
    /// Generating the first sentence once loaded: what a user waits for.
    pub first_audio_ms: u64,
    /// Generation time / audio length for the whole paragraph (below 1 = faster than real
    /// time).
    pub rtf: f64,
    pub audio_seconds: f64,
    /// Highest resident memory of the app while loading and generating.
    pub peak_rss_mb: u64,
    /// Memory the model added over the app's resting level.
    pub model_rss_mb: u64,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct Report {
    pub app_version: String,
    pub device: Device,
    pub text_words: usize,
    pub results: Vec<ModelResult>,
}

pub fn device() -> Device {
    // sysinfo can fail on unusual platforms; the benchmark still runs without these fields.
    let sys = sysinfo::System::new().ok().map(|mut s| {
        s.refresh_memory();
        s.refresh_cpu_all();
        s
    });
    let os = format!(
        "{} {}",
        sysinfo::System::name().unwrap_or_else(|_| std::env::consts::OS.into()),
        sysinfo::System::os_version().unwrap_or_default()
    );
    Device {
        os: os.trim().to_string(),
        arch: std::env::consts::ARCH.into(),
        cpus: std::thread::available_parallelism()
            .map(|n| n.get())
            .unwrap_or(1),
        cpu: sys
            .as_ref()
            .and_then(|s| s.cpus().first().map(|c| c.brand().trim().to_string()))
            .unwrap_or_default(),
        ram_mb: sys.as_ref().map_or(0, |s| s.total_memory() / 1_000_000),
    }
}

fn rss_mb() -> u64 {
    memory_stats::memory_stats()
        .map(|m| m.physical_mem as u64 / 1_000_000)
        .unwrap_or(0)
}

/// Samples resident memory every 10 ms until dropped; reports the peak.
struct PeakMemory {
    peak: Arc<AtomicU64>,
    stop: Arc<AtomicBool>,
    thread: Option<std::thread::JoinHandle<()>>,
}

impl PeakMemory {
    fn start() -> Self {
        let peak = Arc::new(AtomicU64::new(rss_mb()));
        let stop = Arc::new(AtomicBool::new(false));
        let (p, s) = (Arc::clone(&peak), Arc::clone(&stop));
        let thread = std::thread::spawn(move || {
            while !s.load(Ordering::Relaxed) {
                p.fetch_max(rss_mb(), Ordering::Relaxed);
                std::thread::sleep(Duration::from_millis(10));
            }
        });
        Self {
            peak,
            stop,
            thread: Some(thread),
        }
    }

    fn finish(mut self) -> u64 {
        self.stop.store(true, Ordering::Relaxed);
        if let Some(t) = self.thread.take() {
            let _ = t.join();
        }
        self.peak.load(Ordering::Relaxed).max(rss_mb())
    }
}

/// Benchmarks every installed model (newest version each) with one of its voices.
/// `on_progress(done, total)` counts models.
pub fn run(reg: &Registry, on_progress: &mut dyn FnMut(usize, usize)) -> Result<Report, String> {
    let picks = reg.benchmark_voices()?;
    let total = picks.len();
    let mut results = Vec::new();
    on_progress(0, total);
    for (i, voice) in picks.into_iter().enumerate() {
        reg.unload(&voice.model, &voice.model_version);
        let base = rss_mb();
        let mem = PeakMemory::start();

        let t = Instant::now();
        let engine = reg.engine_for(&voice.model, &voice.model_version)?;
        let load_ms = t.elapsed().as_millis() as u64;

        let t = Instant::now();
        engine
            .synthesize_sentence(FIRST_SENTENCE, &voice.source, 1.0)
            .map_err(|e| e.to_string())?;
        let first_audio_ms = t.elapsed().as_millis() as u64;

        let req = voxa_core::SynthesisRequest {
            text: BENCH_TEXT.into(),
            voice: voice.source.clone(),
            speed: 1.0,
        };
        let t = Instant::now();
        let audio = engine
            .synthesize(&req, &mut |_, _| {})
            .map_err(|e| e.to_string())?;
        let gen_s = t.elapsed().as_secs_f64();

        let peak = mem.finish();
        results.push(ModelResult {
            model: voice.model.clone(),
            version: voice.model_version.clone(),
            family: voice.family.clone(),
            voice: voice.name.clone(),
            load_ms,
            first_audio_ms,
            rtf: round3(gen_s / audio.duration().max(1e-6)),
            audio_seconds: round3(audio.duration()),
            peak_rss_mb: peak,
            model_rss_mb: peak.saturating_sub(base),
        });
        on_progress(i + 1, total);
    }
    Ok(Report {
        app_version: env!("CARGO_PKG_VERSION").into(),
        device: device(),
        text_words: BENCH_TEXT.split_whitespace().count(),
        results,
    })
}

fn round3(x: f64) -> f64 {
    (x * 1000.0).round() / 1000.0
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn device_info_is_filled_in() {
        let d = device();
        assert!(d.cpus >= 1);
        assert!(d.ram_mb > 0, "{d:?}");
        assert!(!d.arch.is_empty());
    }

    #[test]
    fn peak_memory_sees_an_allocation() {
        let mem = PeakMemory::start();
        let block = vec![1u8; 64_000_000];
        std::thread::sleep(Duration::from_millis(50));
        let peak = mem.finish();
        assert!(block.iter().all(|b| *b == 1)); // keep it alive and touched
        assert!(peak >= 64, "peak {peak} MB");
    }

    #[test]
    fn bench_text_starts_with_the_first_sentence() {
        assert!(BENCH_TEXT.starts_with(FIRST_SENTENCE));
        assert_eq!(
            voxa_core::split_sentences(BENCH_TEXT)[0],
            FIRST_SENTENCE,
            "time to first audio must measure exactly the first sentence"
        );
    }

    #[test]
    fn empty_store_gives_an_empty_report() {
        let d = tempfile::tempdir().unwrap();
        let reg = Registry::open(d.path(), crate::registry::trusted_keys()).unwrap();
        let r = run(&reg, &mut |_, _| {}).unwrap();
        assert!(r.results.is_empty());
        assert_eq!(r.text_words, BENCH_TEXT.split_whitespace().count());
    }

    /// `VOXA_BENCH_PACK=<any signed pack> cargo test -p voxa-app -- --ignored --nocapture bench`
    #[test]
    #[ignore = "needs a signed voice pack (VOXA_BENCH_PACK)"]
    fn benchmarks_a_real_model() {
        let d = tempfile::tempdir().unwrap();
        let reg = Registry::open(d.path(), crate::registry::trusted_keys()).unwrap();
        reg.import(std::path::Path::new(
            &std::env::var("VOXA_BENCH_PACK").unwrap(),
        ))
        .unwrap();
        let mut seen = Vec::new();
        let r = run(&reg, &mut |a, b| seen.push((a, b))).unwrap();
        assert_eq!(seen, vec![(0, 1), (1, 1)]);
        let m = &r.results[0];
        assert!(m.family.starts_with("Voxa "), "{m:?}");
        assert!(m.audio_seconds > 8.0, "{m:?}");
        assert!(m.rtf > 0.0 && m.rtf < 5.0, "{m:?}");
        assert!(m.first_audio_ms > 0 && m.load_ms > 0, "{m:?}");
        assert!(m.peak_rss_mb > 0, "{m:?}");
        println!("{}", serde_json::to_string_pretty(&r).unwrap());
    }
}
