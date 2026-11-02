//! On-device voice model engines. Each implements [`voxa_core::Engine`].
//!
//! - [`style`]: Kokoro-82M and Kitten TTS through sherpa-onnx, fed phonemes from Voxa's own
//!   G2P (never a sherpa lexicon: lexicon mode silently drops unknown words).

//! - [`supertonic`]: Supertonic 3 through sherpa-onnx, 31 languages from plain text (Voxa
//!   offers the ones it has checked).

pub mod style;
pub mod supertonic;

pub use style::{misaki_to_ipa, Family, StyleEngine, StylePackage};
pub use supertonic::SupertonicEngine;
