//! Voxa's on-device core: everything the app does that does not need a window.
//!
//! - [`text`]: checking and splitting input text into sentences.
//! - [`audio`]: PCM buffers and WAV encoding.
//! - [`engine`]: the interface every on-device voice model implements.
//!
//! Real model engines (sherpa-onnx, MLX, ...) arrive in later milestones; M1 ships a
//! placeholder tone engine so the app can be wired end to end and tested without weights.

pub mod audio;
pub mod engine;
pub mod text;

pub use audio::{Audio, SampleFormat};
pub use engine::{Engine, EngineError, SynthesisRequest, ToneEngine};
pub use text::{split_sentences, validate_text, TextError, MAX_CHARS};
