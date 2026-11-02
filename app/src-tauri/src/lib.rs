//! Tauri shell: exposes the registry (installed voices and models) to the UI as commands.
//! Logic lives in `registry` and the voxa-* crates so it can be tested without a window.

mod bench;
mod registry;

use std::path::PathBuf;
use std::sync::Arc;

use registry::{AvailableModel, ModelInfo, Registry, Voice};
use serde::Serialize;
use tauri::ipc::Response;
use tauri::{Emitter, Manager};
use voxa_core::SampleFormat;

type Reg<'a> = tauri::State<'a, Arc<Registry>>;

#[tauri::command]
fn list_voices(reg: Reg<'_>) -> Result<Vec<Voice>, String> {
    reg.voices()
}

#[tauri::command]
fn list_models(reg: Reg<'_>) -> Result<Vec<ModelInfo>, String> {
    reg.models()
}

/// Verifies and installs a signed voice pack from a folder. Copies and hashes hundreds of
/// MB, so it runs off the UI thread.
#[tauri::command]
async fn import_pack(reg: Reg<'_>, path: String) -> Result<ModelInfo, String> {
    let reg = Arc::clone(&reg);
    tauri::async_runtime::spawn_blocking(move || reg.import(&PathBuf::from(path)))
        .await
        .map_err(|e| e.to_string())?
}

/// Whether this build has a model catalogue to download from.
#[tauri::command]
fn catalogue_configured(reg: Reg<'_>) -> bool {
    reg.catalogue_configured()
}

/// Downloads and verifies the signed catalogue; returns what can be installed.
#[tauri::command]
async fn refresh_catalogue(reg: Reg<'_>) -> Result<Vec<AvailableModel>, String> {
    let reg = Arc::clone(&reg);
    tauri::async_runtime::spawn_blocking(move || reg.refresh_catalogue())
        .await
        .map_err(|e| e.to_string())?
}

#[derive(Clone, Serialize)]
struct InstallProgress {
    id: String,
    version: String,
    done: u64,
    total: u64,
}

/// Downloads a catalogue model, emitting `install-progress` events (at most ~10 a second).
#[tauri::command]
async fn install_model<R: tauri::Runtime>(
    app: tauri::AppHandle<R>,
    reg: Reg<'_>,
    id: String,
    version: String,
) -> Result<ModelInfo, String> {
    let reg = Arc::clone(&reg);
    tauri::async_runtime::spawn_blocking(move || {
        let mut last = std::time::Instant::now() - std::time::Duration::from_secs(1);
        reg.install(&id, &version, &mut |done, total| {
            if done == total || last.elapsed().as_millis() >= 100 {
                last = std::time::Instant::now();
                let _ = app.emit(
                    "install-progress",
                    InstallProgress {
                        id: id.clone(),
                        version: version.clone(),
                        done,
                        total,
                    },
                );
            }
        })
    })
    .await
    .map_err(|e| e.to_string())?
}

#[derive(Clone, Serialize)]
struct BenchProgress {
    done: usize,
    total: usize,
}

/// Runs the device benchmark over every installed model, emitting `bench-progress`.
#[tauri::command]
async fn run_benchmark<R: tauri::Runtime>(
    app: tauri::AppHandle<R>,
    reg: Reg<'_>,
) -> Result<bench::Report, String> {
    let reg = Arc::clone(&reg);
    tauri::async_runtime::spawn_blocking(move || {
        bench::run(&reg, &mut |done, total| {
            let _ = app.emit("bench-progress", BenchProgress { done, total });
        })
    })
    .await
    .map_err(|e| e.to_string())?
}

#[tauri::command]
fn remove_model(reg: Reg<'_>, id: String, version: String) -> Result<(), String> {
    reg.remove(&id, &version)
}

/// Generates speech and returns a 16-bit WAV as raw bytes (an ArrayBuffer in the UI).
#[tauri::command]
async fn synthesize(
    reg: Reg<'_>,
    text: String,
    voice: String,
    speed: f32,
) -> Result<Response, String> {
    let reg = Arc::clone(&reg);
    tauri::async_runtime::spawn_blocking(move || {
        reg.synthesize(&voice, &text, speed)
            .map(|a| Response::new(a.to_wav(SampleFormat::Pcm16)))
    })
    .await
    .map_err(|e| e.to_string())?
}

/// Registers Voxa's state and commands. `root` overrides the data directory (tests).
fn setup<R: tauri::Runtime>(
    builder: tauri::Builder<R>,
    root: Option<PathBuf>,
) -> tauri::Builder<R> {
    let builder = match root {
        // Known data directory (tests): register the state up front.
        Some(root) => builder.manage(Arc::new(
            Registry::open(&root, registry::trusted_keys()).expect("open model store"),
        )),
        // The app's data directory is only known once the app exists.
        None => builder.setup(|app| {
            let root = app.path().app_data_dir()?;
            let reg = Registry::open(&root, registry::trusted_keys())?;
            app.manage(Arc::new(reg));
            Ok(())
        }),
    };
    builder.invoke_handler(tauri::generate_handler![
        list_voices,
        list_models,
        import_pack,
        remove_model,
        synthesize,
        catalogue_configured,
        refresh_catalogue,
        install_model,
        run_benchmark
    ])
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    setup(
        tauri::Builder::default().plugin(tauri_plugin_dialog::init()),
        None,
    )
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

    fn window(
        root: &std::path::Path,
    ) -> (
        tauri::App<tauri::test::MockRuntime>,
        tauri::WebviewWindow<tauri::test::MockRuntime>,
    ) {
        let app = setup(mock_builder(), Some(root.to_path_buf()))
            .build(mock_context(noop_assets()))
            .expect("build mock app");
        let w = WebviewWindowBuilder::new(&app, "main", Default::default())
            .build()
            .expect("build mock window");
        (app, w)
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
    fn fresh_install_has_no_voices_or_models() {
        let d = tempfile::tempdir().unwrap();
        let (_app, w) = window(d.path());
        for cmd in ["list_voices", "list_models"] {
            let v: serde_json::Value = call(&w, cmd, serde_json::json!({}))
                .unwrap()
                .deserialize()
                .unwrap();
            assert_eq!(v, serde_json::json!([]), "{cmd}");
        }
    }

    #[test]
    fn synthesize_without_voices_explains_why() {
        let d = tempfile::tempdir().unwrap();
        let (_app, w) = window(d.path());
        let err = call(
            &w,
            "synthesize",
            serde_json::json!({"text": "Hello.", "voice": "harper", "speed": 1.0}),
        )
        .expect_err("no voices installed");
        assert!(err.as_str().unwrap().contains("unknown voice"), "{err}");
    }

    #[test]
    fn catalogue_commands_without_a_catalogue() {
        let d = tempfile::tempdir().unwrap();
        let (_app, w) = window(d.path());
        let configured: bool = call(&w, "catalogue_configured", serde_json::json!({}))
            .unwrap()
            .deserialize()
            .unwrap();
        assert_eq!(configured, option_env!("VOXA_CATALOGUE_URL").is_some());
        if !configured {
            let err = call(&w, "refresh_catalogue", serde_json::json!({})).unwrap_err();
            assert!(err.as_str().unwrap().contains("no model catalogue"));
        }
        let err = call(
            &w,
            "install_model",
            serde_json::json!({"id": "kokoro-en-us", "version": "1.0.0"}),
        )
        .unwrap_err();
        assert!(err.as_str().unwrap().contains("not in the catalogue"));
    }

    #[test]
    fn import_of_a_plain_folder_is_refused() {
        let d = tempfile::tempdir().unwrap();
        let (_app, w) = window(d.path());
        let empty = tempfile::tempdir().unwrap();
        let err = call(
            &w,
            "import_pack",
            serde_json::json!({"path": empty.path().to_string_lossy()}),
        )
        .expect_err("not a pack");
        assert!(err.as_str().unwrap().contains("not a Voxa voice pack"));
    }
}
