//! Reading and writing WAV files for audio clean-up (film dialogue, recordings).
//!
//! What film work needs:
//! - any common PCM format in: 16/24/32-bit integer and 32-bit float, any channel count;
//! - **every other chunk kept**: BWF `bext` (which carries the timecode reference), `iXML`
//!   and `LIST`, written back in their original order, so the cleaned file lines up on
//!   an editor's timeline;
//! - the same sample rate, channel count and number of frames out as in.

use thiserror::Error;

#[derive(Debug, Error, PartialEq, Eq)]
pub enum WavError {
    #[error("not a WAV file")]
    NotWav,
    #[error("the WAV file is damaged: {0}")]
    Damaged(&'static str),
    #[error("unsupported WAV format: {0}")]
    Unsupported(String),
}

/// Sample encoding of a WAV file.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Encoding {
    Pcm16,
    Pcm24,
    Pcm32,
    Float32,
}

impl Encoding {
    fn bytes(self) -> usize {
        match self {
            Encoding::Pcm16 => 2,
            Encoding::Pcm24 => 3,
            Encoding::Pcm32 | Encoding::Float32 => 4,
        }
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct WavFile {
    pub sample_rate: u32,
    pub encoding: Encoding,
    /// One vector per channel, samples in [-1, 1].
    pub channels: Vec<Vec<f32>>,
    /// Every chunk other than `fmt ` and `data`, in file order (bext, iXML, LIST, ...).
    pub extra_chunks: Vec<([u8; 4], Vec<u8>)>,
}

fn u16_at(b: &[u8], i: usize) -> u16 {
    u16::from_le_bytes([b[i], b[i + 1]])
}
fn u32_at(b: &[u8], i: usize) -> u32 {
    u32::from_le_bytes([b[i], b[i + 1], b[i + 2], b[i + 3]])
}

impl WavFile {
    pub fn frames(&self) -> usize {
        self.channels.first().map_or(0, Vec::len)
    }

    pub fn duration(&self) -> f64 {
        self.frames() as f64 / self.sample_rate.max(1) as f64
    }

    pub fn parse(b: &[u8]) -> Result<Self, WavError> {
        if b.len() < 12 || &b[0..4] != b"RIFF" || &b[8..12] != b"WAVE" {
            return Err(WavError::NotWav);
        }
        let mut fmt: Option<(u16, u16, u32, u16)> = None; // (tag, channels, rate, bits)
        let mut data: Option<&[u8]> = None;
        let mut extra = Vec::new();
        let mut i = 12;
        while i + 8 <= b.len() {
            let id: [u8; 4] = b[i..i + 4].try_into().unwrap();
            let size = u32_at(b, i + 4) as usize;
            let start = i + 8;
            // Recorders sometimes write a data size larger than the file; take what's there.
            let end = start.saturating_add(size).min(b.len());
            let body = &b[start..end];
            match &id {
                b"fmt " => {
                    if body.len() < 16 {
                        return Err(WavError::Damaged("fmt chunk too short"));
                    }
                    let mut tag = u16_at(body, 0);
                    if tag == 0xFFFE && body.len() >= 26 {
                        tag = u16_at(body, 24); // WAVE_FORMAT_EXTENSIBLE sub-format
                    }
                    fmt = Some((tag, u16_at(body, 2), u32_at(body, 4), u16_at(body, 14)));
                }
                b"data" => data = Some(body),
                _ => extra.push((id, body.to_vec())),
            }
            i = start + size + (size & 1); // chunks are word-aligned
        }
        let (tag, nch, rate, bits) = fmt.ok_or(WavError::Damaged("no fmt chunk"))?;
        let data = data.ok_or(WavError::Damaged("no data chunk"))?;
        let encoding = match (tag, bits) {
            (1, 16) => Encoding::Pcm16,
            (1, 24) => Encoding::Pcm24,
            (1, 32) => Encoding::Pcm32,
            (3, 32) => Encoding::Float32,
            _ => return Err(WavError::Unsupported(format!("format {tag}, {bits}-bit"))),
        };
        let nch = nch as usize;
        if nch == 0 || rate == 0 {
            return Err(WavError::Damaged("no channels or no sample rate"));
        }
        let frame = encoding.bytes() * nch;
        let frames = data.len() / frame;
        let mut channels = vec![Vec::with_capacity(frames); nch];
        for f in 0..frames {
            for (c, ch) in channels.iter_mut().enumerate() {
                let o = f * frame + c * encoding.bytes();
                let s = &data[o..o + encoding.bytes()];
                ch.push(match encoding {
                    Encoding::Pcm16 => i16::from_le_bytes([s[0], s[1]]) as f32 / 32768.0,
                    Encoding::Pcm24 => {
                        (i32::from_le_bytes([0, s[0], s[1], s[2]]) >> 8) as f32 / 8_388_608.0
                    }
                    Encoding::Pcm32 => {
                        i32::from_le_bytes([s[0], s[1], s[2], s[3]]) as f32 / 2_147_483_648.0
                    }
                    Encoding::Float32 => f32::from_le_bytes([s[0], s[1], s[2], s[3]]),
                });
            }
        }
        Ok(Self {
            sample_rate: rate,
            encoding,
            channels,
            extra_chunks: extra,
        })
    }

    /// Writes the file with `encoding` (16-bit for previews, 24-bit for deliverables). Extra
    /// chunks are written back in their original order, before the audio.
    pub fn to_bytes(&self, encoding: Encoding) -> Vec<u8> {
        let nch = self.channels.len().max(1);
        let bps = encoding.bytes();
        let frames = self.frames();
        let data_len = frames * nch * bps;
        let (tag, bits): (u16, u16) = match encoding {
            Encoding::Float32 => (3, 32),
            e => (1, (e.bytes() * 8) as u16),
        };
        let mut out = Vec::with_capacity(data_len + 1024);
        out.extend_from_slice(b"RIFF\0\0\0\0WAVE");
        out.extend_from_slice(b"fmt ");
        out.extend_from_slice(&16u32.to_le_bytes());
        out.extend_from_slice(&tag.to_le_bytes());
        out.extend_from_slice(&(nch as u16).to_le_bytes());
        out.extend_from_slice(&self.sample_rate.to_le_bytes());
        out.extend_from_slice(&(self.sample_rate * (nch * bps) as u32).to_le_bytes());
        out.extend_from_slice(&((nch * bps) as u16).to_le_bytes());
        out.extend_from_slice(&bits.to_le_bytes());
        for (id, body) in &self.extra_chunks {
            out.extend_from_slice(id);
            out.extend_from_slice(&(body.len() as u32).to_le_bytes());
            out.extend_from_slice(body);
            if body.len() % 2 == 1 {
                out.push(0);
            }
        }
        out.extend_from_slice(b"data");
        out.extend_from_slice(&(data_len as u32).to_le_bytes());
        for f in 0..frames {
            for ch in &self.channels {
                let s = ch[f];
                let s = if s.is_nan() { 0.0 } else { s };
                match encoding {
                    Encoding::Pcm16 => out.extend_from_slice(
                        &((s.clamp(-1.0, 1.0) * 32767.0).round() as i16).to_le_bytes(),
                    ),
                    Encoding::Pcm24 => {
                        let v = (s.clamp(-1.0, 1.0) * 8_388_607.0).round() as i32;
                        out.extend_from_slice(&v.to_le_bytes()[..3]);
                    }
                    Encoding::Pcm32 => out.extend_from_slice(
                        &((s.clamp(-1.0, 1.0) as f64 * 2_147_483_647.0).round() as i32)
                            .to_le_bytes(),
                    ),
                    Encoding::Float32 => out.extend_from_slice(&s.to_le_bytes()),
                }
            }
        }
        let riff = (out.len() - 8) as u32;
        out[4..8].copy_from_slice(&riff.to_le_bytes());
        out
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn stereo(frames: usize) -> Vec<Vec<f32>> {
        vec![
            (0..frames).map(|i| (i as f32 * 0.01).sin() * 0.8).collect(),
            (0..frames).map(|i| (i as f32 * 0.03).cos() * 0.5).collect(),
        ]
    }

    #[test]
    fn round_trips_every_encoding_and_keeps_length() {
        for enc in [
            Encoding::Pcm16,
            Encoding::Pcm24,
            Encoding::Pcm32,
            Encoding::Float32,
        ] {
            let w = WavFile {
                sample_rate: 48_000,
                encoding: enc,
                channels: stereo(1001),
                extra_chunks: vec![],
            };
            let back = WavFile::parse(&w.to_bytes(enc)).unwrap();
            assert_eq!(back.encoding, enc);
            assert_eq!(
                (back.sample_rate, back.channels.len(), back.frames()),
                (48_000, 2, 1001)
            );
            let tol = match enc {
                Encoding::Pcm16 => 1e-4,
                Encoding::Pcm24 => 1e-6,
                _ => 1e-7,
            };
            for (a, b) in w
                .channels
                .iter()
                .flatten()
                .zip(back.channels.iter().flatten())
            {
                assert!((a - b).abs() < tol, "{enc:?}: {a} vs {b}");
            }
        }
    }

    #[test]
    fn keeps_bwf_and_ixml_chunks_in_order_including_odd_sizes() {
        let bext = b"Voxa test\0 timecode ref 00:01:00:00".to_vec(); // odd length
        let ixml = b"<BWFXML><SCENE>12A</SCENE></BWFXML>".to_vec();
        let w = WavFile {
            sample_rate: 48_000,
            encoding: Encoding::Pcm24,
            channels: stereo(10),
            extra_chunks: vec![(*b"bext", bext.clone()), (*b"iXML", ixml.clone())],
        };
        let back = WavFile::parse(&w.to_bytes(Encoding::Pcm24)).unwrap();
        assert_eq!(back.extra_chunks, vec![(*b"bext", bext), (*b"iXML", ixml)]);
        assert_eq!(back.frames(), 10);
    }

    #[test]
    fn reads_wave_format_extensible() {
        let w = WavFile {
            sample_rate: 44_100,
            encoding: Encoding::Pcm24,
            channels: stereo(5),
            extra_chunks: vec![],
        };
        let mut b = w.to_bytes(Encoding::Pcm24);
        // Turn the plain fmt chunk into an extensible one (size 40, tag 0xFFFE, sub-format 1).
        let fmt_end = 12 + 8 + 16;
        let mut ext = b[..fmt_end].to_vec();
        ext[16..20].copy_from_slice(&40u32.to_le_bytes());
        ext[20..22].copy_from_slice(&0xFFFEu16.to_le_bytes());
        ext.extend_from_slice(&22u16.to_le_bytes()); // cbSize
        ext.extend_from_slice(&24u16.to_le_bytes()); // valid bits
        ext.extend_from_slice(&3u32.to_le_bytes()); // channel mask
        ext.extend_from_slice(&1u16.to_le_bytes()); // sub-format: PCM
        ext.extend_from_slice(&[0u8; 14]);
        ext.extend_from_slice(&b[fmt_end..]);
        let riff = (ext.len() - 8) as u32;
        ext[4..8].copy_from_slice(&riff.to_le_bytes());
        b = ext;
        let back = WavFile::parse(&b).unwrap();
        assert_eq!((back.encoding, back.frames()), (Encoding::Pcm24, 5));
    }

    #[test]
    fn rejects_non_wav_and_unsupported_formats() {
        assert_eq!(WavFile::parse(b"hello"), Err(WavError::NotWav));
        let mut b = WavFile {
            sample_rate: 8000,
            encoding: Encoding::Pcm16,
            channels: vec![vec![0.0; 4]],
            extra_chunks: vec![],
        }
        .to_bytes(Encoding::Pcm16);
        b[20] = 2; // ADPCM
        assert!(matches!(WavFile::parse(&b), Err(WavError::Unsupported(_))));
    }

    #[test]
    fn tolerates_an_oversized_data_length() {
        let w = WavFile {
            sample_rate: 16_000,
            encoding: Encoding::Pcm16,
            channels: vec![vec![0.25; 100]],
            extra_chunks: vec![],
        };
        let mut b = w.to_bytes(Encoding::Pcm16);
        let data_size_at = b.len() - 200 - 4;
        b[data_size_at..data_size_at + 4].copy_from_slice(&0xFFFF_FFF0u32.to_le_bytes());
        assert_eq!(WavFile::parse(&b).unwrap().frames(), 100);
    }
}
