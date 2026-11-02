//! Mono PCM audio and WAV encoding.
//!
//! Samples are kept as `f32` in [-1, 1]. WAV output is 16-bit for playback or 24-bit for
//! film work (the clean-up feature keeps 48 kHz / 24-bit end to end).

/// Bit depth of an encoded WAV.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SampleFormat {
    Pcm16,
    Pcm24,
}

impl SampleFormat {
    fn bytes(self) -> u16 {
        match self {
            SampleFormat::Pcm16 => 2,
            SampleFormat::Pcm24 => 3,
        }
    }
}

/// A mono audio clip.
#[derive(Debug, Clone, PartialEq)]
pub struct Audio {
    pub sample_rate: u32,
    pub samples: Vec<f32>,
}

impl Audio {
    pub fn new(sample_rate: u32, samples: Vec<f32>) -> Self {
        Self {
            sample_rate,
            samples,
        }
    }

    /// Length in seconds.
    pub fn duration(&self) -> f64 {
        if self.sample_rate == 0 {
            return 0.0;
        }
        self.samples.len() as f64 / self.sample_rate as f64
    }

    /// Appends `other`, with `gap_s` seconds of silence between, as the server does when it
    /// joins per-sentence audio. Panics if the sample rates differ: engines must agree.
    pub fn append(&mut self, other: &Audio, gap_s: f32) {
        assert_eq!(
            self.sample_rate, other.sample_rate,
            "cannot join audio at different sample rates"
        );
        if !self.samples.is_empty() && gap_s > 0.0 {
            let gap = (gap_s * self.sample_rate as f32).round() as usize;
            self.samples.extend(std::iter::repeat_n(0.0, gap));
        }
        self.samples.extend_from_slice(&other.samples);
    }

    /// Encodes as a RIFF/WAVE file (mono, little-endian PCM). Samples outside [-1, 1] are
    /// clipped and NaN becomes silence.
    pub fn to_wav(&self, format: SampleFormat) -> Vec<u8> {
        let bps = format.bytes();
        let data_len = self.samples.len() as u32 * bps as u32;
        let mut out = Vec::with_capacity(44 + data_len as usize);
        out.extend_from_slice(b"RIFF");
        out.extend_from_slice(&(36 + data_len).to_le_bytes());
        out.extend_from_slice(b"WAVE");
        out.extend_from_slice(b"fmt ");
        out.extend_from_slice(&16u32.to_le_bytes());
        out.extend_from_slice(&1u16.to_le_bytes()); // PCM
        out.extend_from_slice(&1u16.to_le_bytes()); // mono
        out.extend_from_slice(&self.sample_rate.to_le_bytes());
        out.extend_from_slice(&(self.sample_rate * bps as u32).to_le_bytes()); // byte rate
        out.extend_from_slice(&bps.to_le_bytes()); // block align
        out.extend_from_slice(&(bps * 8).to_le_bytes());
        out.extend_from_slice(b"data");
        out.extend_from_slice(&data_len.to_le_bytes());
        for &s in &self.samples {
            let s = if s.is_nan() { 0.0 } else { s.clamp(-1.0, 1.0) };
            match format {
                SampleFormat::Pcm16 => {
                    out.extend_from_slice(&((s * i16::MAX as f32).round() as i16).to_le_bytes())
                }
                SampleFormat::Pcm24 => {
                    let v = (s * 8_388_607.0).round() as i32;
                    out.extend_from_slice(&v.to_le_bytes()[..3]);
                }
            }
        }
        out
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn u16_at(b: &[u8], i: usize) -> u16 {
        u16::from_le_bytes([b[i], b[i + 1]])
    }
    fn u32_at(b: &[u8], i: usize) -> u32 {
        u32::from_le_bytes([b[i], b[i + 1], b[i + 2], b[i + 3]])
    }

    #[test]
    fn wav_header_is_correct_16_bit() {
        let a = Audio::new(24_000, vec![0.0; 100]);
        let w = a.to_wav(SampleFormat::Pcm16);
        assert_eq!(&w[0..4], b"RIFF");
        assert_eq!(&w[8..12], b"WAVE");
        assert_eq!(u32_at(&w, 4) as usize, w.len() - 8);
        assert_eq!(u16_at(&w, 22), 1); // channels
        assert_eq!(u32_at(&w, 24), 24_000);
        assert_eq!(u32_at(&w, 28), 48_000); // byte rate
        assert_eq!(u16_at(&w, 34), 16);
        assert_eq!(u32_at(&w, 40), 200);
        assert_eq!(w.len(), 44 + 200);
    }

    #[test]
    fn wav_24_bit_keeps_length_and_full_scale() {
        let a = Audio::new(48_000, vec![1.0, -1.0, 0.0]);
        let w = a.to_wav(SampleFormat::Pcm24);
        assert_eq!(u16_at(&w, 34), 24);
        assert_eq!(u32_at(&w, 40), 9);
        assert_eq!(&w[44..47], &[0xFF, 0xFF, 0x7F]); // +8388607
        assert_eq!(&w[47..50], &[0x01, 0x00, 0x80]); // -8388607
        assert_eq!(&w[50..53], &[0, 0, 0]);
    }

    #[test]
    fn clips_and_silences_bad_samples() {
        let a = Audio::new(8_000, vec![2.0, f32::NAN]);
        let w = a.to_wav(SampleFormat::Pcm16);
        assert_eq!(i16::from_le_bytes([w[44], w[45]]), i16::MAX);
        assert_eq!(i16::from_le_bytes([w[46], w[47]]), 0);
    }

    #[test]
    fn append_inserts_gap_only_between_clips() {
        let mut a = Audio::new(10, vec![]);
        a.append(&Audio::new(10, vec![1.0; 5]), 0.5);
        assert_eq!(a.samples.len(), 5); // no leading gap
        a.append(&Audio::new(10, vec![1.0; 5]), 0.5);
        assert_eq!(a.samples.len(), 15);
        assert_eq!(a.samples[5..10], [0.0; 5]);
        assert!((a.duration() - 1.5).abs() < 1e-9);
    }

    #[test]
    #[should_panic(expected = "different sample rates")]
    fn append_rejects_mismatched_rates() {
        let mut a = Audio::new(24_000, vec![0.0]);
        a.append(&Audio::new(48_000, vec![0.0]), 0.0);
    }
}
