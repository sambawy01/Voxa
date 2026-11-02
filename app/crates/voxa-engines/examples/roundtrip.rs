//! Quality gate for English voices: speak test sentences with Kokoro + Voxa's G2P, transcribe
//! them with Whisper (through sherpa-onnx), and report the word error rate.
//!
//! ```text
//! cargo run --release -p voxa-engines --example roundtrip -- \
//!     <kokoro-package> <misaki-dir> <whisper-tiny.en-dir> [voice] [max-wer]
//! ```
//!
//! `<misaki-dir>` holds us_gold.json and us_silver.json. Exits non-zero when the overall
//! WER is above `max-wer` (default 0.15), so it can gate releases of a voice package.

use std::path::Path;
use std::sync::Arc;
use std::time::Instant;

use sherpa_onnx::{OfflineRecognizer, OfflineRecognizerConfig};
use voxa_core::{Engine, SynthesisRequest};
use voxa_engines::{KokoroEngine, KokoroPackage};
use voxa_g2p::{lexicon::Lexicon, normalize::normalize, EnglishG2p, Source, Token};

const SENTENCES: &[&str] = &[
    "The quick brown fox jumps over the lazy dog.",
    "She sells seashells by the seashore every summer.",
    "Doctor Smith walked to the station and bought two tickets.",
    "Our meeting starts at 3:30 on Tuesday.",
    "The jacket costs $49.99 after a 20% discount.",
    "In 1999 the population was about 2,500 people.",
    "My cats were sleeping while the dogs played outside.",
    "Please call me tomorrow morning if anything changes.",
    "The scientists published their findings in the journal last week.",
    "Rain is expected tonight, so bring an umbrella.",
    "He finished the 21st chapter before midnight.",
    "Voxa reads your text aloud on this device.",
];

fn words(s: &str) -> Vec<String> {
    normalize(s)
        .to_lowercase()
        .replace(['-', '—'], " ")
        .split_whitespace()
        .map(|w| {
            w.chars()
                .filter(|c| c.is_alphanumeric() || *c == '\'')
                .collect::<String>()
        })
        .filter(|w| !w.is_empty())
        .collect()
}

/// Word-level Levenshtein distance.
fn edits(a: &[String], b: &[String]) -> usize {
    let mut prev: Vec<usize> = (0..=b.len()).collect();
    for (i, x) in a.iter().enumerate() {
        let mut cur = vec![i + 1];
        for (j, y) in b.iter().enumerate() {
            let sub = prev[j] + usize::from(x != y);
            cur.push(sub.min(prev[j + 1] + 1).min(cur[j] + 1));
        }
        prev = cur;
    }
    prev[b.len()]
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.len() < 3 {
        eprintln!("usage: roundtrip <kokoro-package> <misaki-dir> <whisper-dir> [voice] [max-wer]");
        std::process::exit(2);
    }
    let voice = args.get(3).cloned().unwrap_or_else(|| "af_heart".into());
    let max_wer: f64 = args.get(4).and_then(|s| s.parse().ok()).unwrap_or(0.15);

    let misaki = Path::new(&args[1]);
    let read = |n: &str| std::fs::read(misaki.join(n)).expect(n);
    let lex = Lexicon::from_json(&[&read("us_silver.json"), &read("us_gold.json")]).unwrap();
    let g2p = Arc::new(EnglishG2p::new(lex));

    let pkg = KokoroPackage::open(Path::new(&args[0])).unwrap();
    let t = Instant::now();
    let engine = KokoroEngine::load("kokoro-en-us", &pkg, &["af_", "am_"], g2p.clone(), 4).unwrap();
    eprintln!("Kokoro loaded in {:?}", t.elapsed());

    let w = Path::new(&args[2]);
    let mut cfg = OfflineRecognizerConfig::default();
    cfg.model_config.whisper.encoder =
        Some(w.join("tiny.en-encoder.int8.onnx").display().to_string());
    cfg.model_config.whisper.decoder =
        Some(w.join("tiny.en-decoder.int8.onnx").display().to_string());
    cfg.model_config.tokens = Some(w.join("tiny.en-tokens.txt").display().to_string());
    cfg.model_config.num_threads = 4;
    let asr = OfflineRecognizer::create(&cfg).expect("load whisper");

    let (mut errs, mut total, mut audio_s, mut gen_s) = (0usize, 0usize, 0f64, 0f64);
    let mut rules_words = Vec::new();
    for s in SENTENCES {
        for t in g2p.analyse(s) {
            if let Token::Word {
                text,
                source: Source::Rules,
                ..
            } = t
            {
                rules_words.push(text);
            }
        }
        let t = Instant::now();
        let req = SynthesisRequest {
            text: s.to_string(),
            voice: voice.clone(),
            speed: 1.0,
        };
        let a = engine.synthesize(&req, &mut |_, _| {}).unwrap();
        gen_s += t.elapsed().as_secs_f64();
        audio_s += a.duration();
        let stream = asr.create_stream();
        stream.accept_waveform(a.sample_rate as i32, &a.samples);
        asr.decode(&stream);
        let heard = stream.get_result().map(|r| r.text).unwrap_or_default();
        let (want, got) = (words(s), words(&heard));
        let e = edits(&want, &got);
        errs += e;
        total += want.len();
        println!(
            "{:>5.2}  {s}\n       heard: {}",
            e as f64 / want.len() as f64,
            heard.trim()
        );
    }
    let wer = errs as f64 / total as f64;
    println!(
        "\nWER {wer:.3} ({errs}/{total} words), voice {voice}, RTF {:.2}, letter-to-sound words: {:?}",
        gen_s / audio_s,
        rules_words
    );
    if wer > max_wer {
        eprintln!("FAIL: WER {wer:.3} > {max_wer}");
        std::process::exit(1);
    }
}
