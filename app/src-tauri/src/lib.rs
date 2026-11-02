//! Tauri shell: exposes voxa-core to the UI as commands. All logic lives in voxa-core so it
//! can be tested without a window; this file only adapts types.

use std::sync::Arc;

use serde::Serialize;
use tauri::ipc::Response;
use voxa_core::{Engine, SampleFormat, SynthesisRequest, ToneEngine};

/// The engines available on this device. M1 has only the placeholder.
struct Engines {
    default: Arc<dyn Engine>,
}

#[derive(Serialize)]
struct VoiceInfo {
    id: String,
    engine: String,
}

#[tauri::command]
fn list_voices(engines: tauri::State<'_, Engines>) -> Vec<VoiceInfo> {
    let e = &engines.default;
    e.voices()
        .into_iter()
        .map(|id| VoiceInfo {
            id,
            engine: e.id().to_string(),
        })
        .collect()
}

/// Generates speech and returns a 16-bit WAV as raw bytes (an ArrayBuffer in the UI).
/// Runs off the UI thread so long text never freezes the window.
#[tauri::command]
async fn synthesize(
    engines: tauri::State<'_, Engines>,
    text: String,
    voice: String,
    speed: f32,
) -> Result<Response, String> {
    let engine = Arc::clone(&engines.default);
    let req = SynthesisRequest { text, voice, speed };
    tauri::async_runtime::spawn_blocking(move || {
        engine
            .synthesize(&req, &mut |_, _| {})
            .map(|a| Response::new(a.to_wav(SampleFormat::Pcm16)))
            .map_err(|e| e.to_string())
    })
    .await
    .map_err(|e| e.to_string())?
}

/// Registers Voxa's state and commands. Shared by the app and the tests (which use Tauri's
/// mock runtime), so tests exercise the same wiring the window uses.
fn setup<R: tauri::Runtime>(builder: tauri::Builder<R>) -> tauri::Builder<R> {
    builder
        .manage(Engines {
            default: Arc::new(ToneEngine),
        })
        .invoke_handler(tauri::generate_handler![list_voices, synthesize])
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    setup(tauri::Builder::default())
        .run(tauri::generate_context!())
        .expect("error while running Voxa");
}

#[cfg(test)]
mod tests {
    use super::*;
    use tauri::ipc::{CallbackFn, InvokeBody, InvokeResponseBody};
    use tauri::test::{get_ipc_response, mock_builder, mock_context, noop_assets, INVOKE_KEY};
    use tauri::webview::InvokeRequest;
    use tauri::WebviewWindowBuilder;

    fn window() -> tauri::WebviewWindow<tauri::test::MockRuntime> {
        let app = setup(mock_builder())
            .build(mock_context(noop_assets()))
            .expect("build mock app");
        WebviewWindowBuilder::new(&app, "main", Default::default())
            .build()
            .expect("build mock window")
    }

    fn call(
        w: &tauri::WebviewWindow<tauri::test::MockRuntime>,
        cmd: &str,
        args: serde_json::Value,
    ) -> Result<InvokeResponseBody, serde_json::Value> {
        get_ipc_response(
            w,
            InvokeRequest {
                cmd: cmd.into(),
                callback: CallbackFn(0),
                error: CallbackFn(1),
                url: "tauri://localhost".parse().unwrap(),
                body: InvokeBody::Json(args),
                headers: Default::default(),
                invoke_key: INVOKE_KEY.to_string(),
            },
        )
    }

    #[test]
    fn list_voices_returns_engine_voices() {
        let w = window();
        let body = call(&w, "list_voices", serde_json::json!({})).expect("ok");
        let v: serde_json::Value = body.deserialize().expect("json");
        let ids: Vec<&str> = v
            .as_array()
            .unwrap()
            .iter()
            .map(|x| x["id"].as_str().unwrap())
            .collect();
        assert_eq!(ids, ["low", "high"]);
    }

    #[test]
    fn synthesize_returns_wav_bytes() {
        let w = window();
        let body = call(
            &w,
            "synthesize",
            serde_json::json!({"text": "Hello there.", "voice": "low", "speed": 1.0}),
        )
        .expect("ok");
        match body {
            InvokeResponseBody::Raw(bytes) => {
                assert_eq!(&bytes[0..4], b"RIFF");
                assert_eq!(&bytes[8..12], b"WAVE");
                assert!(bytes.len() > 44);
            }
            other => panic!("expected raw bytes, got {other:?}"),
        }
    }

    #[test]
    fn synthesize_reports_errors_as_text() {
        let w = window();
        let err = call(
            &w,
            "synthesize",
            serde_json::json!({"text": "  ", "voice": "low", "speed": 1.0}),
        )
        .expect_err("empty text must fail");
        assert_eq!(err, serde_json::json!("text is empty"));
    }
}
