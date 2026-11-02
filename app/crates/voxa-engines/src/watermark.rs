//! An inaudible watermark on cloned-voice audio, and a detector (roadmap M9c).
//!
//! Every clip made with a cloned voice carries a keyed pseudo-random noise pattern about
//! 40 dB below the speech. The pattern repeats every [`BLOCK`] samples, so the detector can
//! find it in any excerpt (a cropped clip) by folding the audio onto one block and
//! correlating against the pattern at every offset. Anyone with Voxa can check a file.
//!
//! What it is and isn't: it survives volume changes, cropping and mixing with other sound;
//! it is not designed to survive heavy compression, resampling or deliberate removal. It
//! marks honest use and helps people check; it does not make misuse impossible, which is
//! why consent is checked before a voice exists (see `consent`).

/// Pattern length in samples.
pub const BLOCK: usize = 4096;
/// Watermark level relative to the clip's RMS (-40 dB).
const LEVEL: f32 = 0.01;
/// Detection threshold on the normalised correlation (a z-score).
pub const DETECT_Z: f32 = 6.0;

/// The pattern: ±1 chips from a keyed xorshift generator. The key is public (it lets
/// anyone check a file); it identifies the watermark as Voxa's, not who made the clip.
fn pattern(key: u64) -> Vec<f32> {
    let mut s = key | 1;
    (0..BLOCK)
        .map(|_| {
            s ^= s << 13;
            s ^= s >> 7;
            s ^= s << 17;
            if s & 1 == 0 {
                1.0
            } else {
                -1.0
            }
        })
        .collect()
}

pub const VOXA_KEY: u64 = 0x564f_5841_434c_4f4e; // "VOXACLON"

fn rms(x: &[f32]) -> f32 {
    (x.iter().map(|v| v * v).sum::<f32>() / x.len().max(1) as f32).sqrt()
}

/// Adds the watermark in place.
pub fn embed(samples: &mut [f32], key: u64) {
    let p = pattern(key);
    let a = LEVEL * rms(samples).max(1e-4);
    for (i, s) in samples.iter_mut().enumerate() {
        *s += a * p[i % BLOCK];
    }
}

/// Cyclic first difference: a high-pass that removes most of speech's (low-frequency)
/// energy while keeping the white watermark, so the correlation sees far less "noise".
fn diff_cyclic(x: &[f64]) -> Vec<f64> {
    let n = x.len();
    (0..n).map(|i| x[i] - x[(i + n - 1) % n]).collect()
}

/// The detection score: the best normalised correlation over every offset. Unmarked audio
/// scores around 0-4; marked audio of a few seconds scores well above [`DETECT_Z`].
pub fn score(samples: &[f32], key: u64) -> f32 {
    if samples.len() < BLOCK * 4 {
        return 0.0;
    }
    // Fold onto one block, then whiten both the folded audio and the pattern.
    let mut acc = vec![0f64; BLOCK];
    for (i, s) in samples.iter().enumerate() {
        acc[i % BLOCK] += *s as f64;
    }
    let acc = diff_cyclic(&acc);
    let p = diff_cyclic(&pattern(key).iter().map(|v| *v as f64).collect::<Vec<_>>());
    let mean = acc.iter().sum::<f64>() / BLOCK as f64;
    let sd = (acc.iter().map(|v| (v - mean).powi(2)).sum::<f64>() / BLOCK as f64).sqrt();
    let pn = p.iter().map(|v| v * v).sum::<f64>().sqrt();
    if sd == 0.0 {
        return 0.0;
    }
    let mut best = 0f64;
    for off in 0..BLOCK {
        let mut c = 0f64;
        for (j, pj) in p.iter().enumerate() {
            c += (acc[(j + off) % BLOCK] - mean) * pj;
        }
        // Under no watermark, c / (sd * |p|) is roughly standard normal.
        best = best.max(c / (sd * pn));
    }
    best as f32
}

pub fn detect(samples: &[f32], key: u64) -> bool {
    score(samples, key) >= DETECT_Z
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A speech-like test signal: harmonic tones with a slow envelope plus some noise.
    fn speechy(n: usize, seed: u32) -> Vec<f32> {
        let mut s = seed;
        (0..n)
            .map(|i| {
                s = s.wrapping_mul(1_664_525).wrapping_add(1_013_904_223);
                let t = i as f32 / 24_000.0;
                let env = (0.5 + 0.5 * (t * 3.0).sin()).powi(2);
                let tone: f32 = (1..6)
                    .map(|h| (2.0 * std::f32::consts::PI * 140.0 * h as f32 * t).sin() / h as f32)
                    .sum();
                0.3 * env * tone + 0.02 * ((s >> 8) as f32 / 16_777_216.0 - 0.5)
            })
            .collect()
    }

    #[test]
    fn detects_its_own_mark_and_not_clean_audio() {
        let clean = speechy(24_000 * 5, 1);
        let mut marked = clean.clone();
        embed(&mut marked, VOXA_KEY);
        let (z_clean, z_marked) = (score(&clean, VOXA_KEY), score(&marked, VOXA_KEY));
        assert!(z_clean < DETECT_Z, "clean scored {z_clean}");
        assert!(z_marked > 2.0 * DETECT_Z, "marked scored {z_marked}");
        assert!(
            !detect(&marked, VOXA_KEY ^ 0xFF),
            "another key must not match"
        );
    }

    #[test]
    fn survives_cropping_gain_and_added_noise() {
        let mut marked = speechy(24_000 * 8, 2);
        embed(&mut marked, VOXA_KEY);
        // A 3-second excerpt starting at an odd offset, quieter, with other sound mixed in.
        let other = speechy(24_000 * 3, 9);
        let excerpt: Vec<f32> = marked[12_345..12_345 + 24_000 * 3]
            .iter()
            .zip(&other)
            .map(|(m, o)| 0.4 * m + 0.2 * o)
            .collect();
        assert!(
            detect(&excerpt, VOXA_KEY),
            "score {}",
            score(&excerpt, VOXA_KEY)
        );
    }

    #[test]
    fn is_quiet() {
        let clean = speechy(24_000 * 3, 3);
        let mut marked = clean.clone();
        embed(&mut marked, VOXA_KEY);
        let diff: Vec<f32> = marked.iter().zip(&clean).map(|(a, b)| a - b).collect();
        let db = 20.0 * (rms(&diff) / rms(&clean)).log10();
        assert!((db + 40.0).abs() < 0.5, "watermark at {db:.1} dB");
    }

    #[test]
    fn short_clips_are_not_judged() {
        assert_eq!(score(&[0.1; 1000], VOXA_KEY), 0.0);
    }
}
