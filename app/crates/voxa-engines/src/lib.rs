//! On-device voice model engines. Each implements [`voxa_core::Engine`].
//!
//! - [`kokoro`]: Kokoro-82M through sherpa-onnx, fed phonemes from Voxa's own G2P (never a
//!   sherpa lexicon: lexicon mode silently drops unknown words).

pub mod kokoro;

pub use kokoro::{KokoroEngine, KokoroPackage};
