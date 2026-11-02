//! On-device voice model engines. Each implements [`voxa_core::Engine`].
//!
//! - [`style`]: Kokoro-82M and Kitten TTS through sherpa-onnx, fed phonemes from Voxa's own
//!   G2P (never a sherpa lexicon: lexicon mode silently drops unknown words).

pub mod style;

pub use style::{misaki_to_ipa, Family, StyleEngine, StylePackage};
