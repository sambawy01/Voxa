//! On-device voice model engines. Each implements [`voxa_core::Engine`].
//!
//! - [`style`]: Kokoro-82M and Kitten TTS through sherpa-onnx, fed phonemes from Voxa's own
//!   G2P (never a sherpa lexicon: lexicon mode silently drops unknown words).

//! - [`cleanup`]: noise removal for recorded speech (DPDFNet 48 kHz, GTCRN), exact length.
//! - [`consent`]: consent checks before voice cloning (speech recognition + speaker match).
//! - [`pocket`]: Pocket TTS, speech in a cloned voice from a reference recording (Preview).
//! - [`watermark`]: the inaudible watermark on cloned-voice audio, and its detector.
//! - [`supertonic`]: Supertonic 3 through sherpa-onnx, 31 languages from plain text (Voxa
//!   offers the ones it has checked).

pub mod cleanup;
pub mod consent;
pub mod pocket;
pub mod style;
pub mod supertonic;
pub mod watermark;

pub use cleanup::{mix, Denoiser, DenoiserKind};
pub use pocket::PocketEngine;
pub use style::{misaki_to_ipa, Family, StyleEngine, StylePackage};
pub use supertonic::SupertonicEngine;
