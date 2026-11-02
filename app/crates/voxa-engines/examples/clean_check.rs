//! Quality check for audio clean-up: mixes a clean speech WAV with noise (white noise plus
//! mains hum) at a chosen SNR, cleans it, and reports SNR before/after, the delay between
//! cleaned and clean speech (must be 0 for film sync) and the length (must be exact).
//!
//! ```text
//! cargo run --release -p voxa-engines --example clean_check -- \
//!     <studio|fast> <model.onnx> <clean-speech.wav> [snr-db=0]
//! ```
//! Exits non-zero if the length differs, the delay isn't 0, or SNR doesn't improve.

use std::path::Path;

use voxa_core::WavFile;
use voxa_engines::{Denoiser, DenoiserKind};

fn snr(clean: &[f32], test: &[f32]) -> f64 {
    let s: f64 = clean.iter().map(|x| (*x as f64).powi(2)).sum();
    let e: f64 = clean
        .iter()
        .zip(test)
        .map(|(c, t)| ((*c - *t) as f64).powi(2))
        .sum();
    10.0 * (s / e.max(1e-12)).log10()
}

/// Lag (in samples) that best aligns `b` with `a`, searched within ±`max`.
fn lag(a: &[f32], b: &[f32], max: i64) -> i64 {
    let n = a.len().min(b.len()).min(a.len().min(200_000));
    let mut best = (0, f64::MIN);
    for l in -max..=max {
        let mut c = 0.0f64;
        for (i, x) in a.iter().enumerate().take(n) {
            let j = i as i64 + l;
            if j >= 0 && (j as usize) < n {
                c += *x as f64 * b[j as usize] as f64;
            }
        }
        if c > best.1 {
            best = (l, c);
        }
    }
    best.0
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.len() < 3 {
        eprintln!("usage: clean_check <studio|fast> <model.onnx> <clean-speech.wav> [snr-db]");
        std::process::exit(2);
    }
    let kind = if args[0] == "fast" {
        DenoiserKind::Fast
    } else {
        DenoiserKind::Studio
    };
    let target_snr: f64 = args.get(3).and_then(|s| s.parse().ok()).unwrap_or(0.0);
    let wav = WavFile::parse(&std::fs::read(&args[2]).expect("read wav")).expect("parse wav");
    let rate = wav.sample_rate;
    let clean = &wav.channels[0];
    let p: f64 = clean.iter().map(|x| (*x as f64).powi(2)).sum::<f64>() / clean.len() as f64;
    let mut seed = 12345u32;
    let noise: Vec<f32> = (0..clean.len())
        .map(|i| {
            seed = seed.wrapping_mul(1_664_525).wrapping_add(1_013_904_223);
            let w = (seed >> 8) as f32 / 16_777_216.0 - 0.5;
            w + 0.3 * (2.0 * std::f32::consts::PI * 60.0 * i as f32 / rate as f32).sin()
        })
        .collect();
    let pn: f64 = noise.iter().map(|x| (*x as f64).powi(2)).sum::<f64>() / noise.len() as f64;
    let k = (p / pn / 10f64.powf(target_snr / 10.0)).sqrt() as f32;
    let noisy: Vec<f32> = clean.iter().zip(&noise).map(|(c, n)| c + k * n).collect();

    let d = Denoiser::load(kind, Path::new(&args[1]), 4).expect("load");
    let t = std::time::Instant::now();
    let out = d.clean(&noisy, rate).expect("clean");
    let secs = t.elapsed().as_secs_f64();
    let delay = lag(clean, &out, 2000);
    let (before, after) = (snr(clean, &noisy), snr(clean, &out));
    println!(
        "{kind:?} on {:.1} s at {rate} Hz: SNR {before:.1} -> {after:.1} dB, delay {delay} samples, \
         length {} -> {}, {:.1}x real time",
        clean.len() as f64 / rate as f64,
        noisy.len(),
        out.len(),
        clean.len() as f64 / rate as f64 / secs
    );
    if out.len() != noisy.len() || delay != 0 || after <= before + 3.0 {
        eprintln!("FAIL");
        std::process::exit(1);
    }
}
