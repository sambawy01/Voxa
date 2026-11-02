//! Tauri shell: exposes the registry (installed voices and models) to the UI as commands.
//! Logic lives in `registry` and the voxa-* crates so it can be tested without a window.

mod bench;
mod cleanup;
mod cloning;
mod licence;
mod registry;

use std::path::PathBuf;
use std::sync::Arc;

use registry::{AvailableModel, ModelInfo, Registry, Voice};
use serde::Serialize;
use tauri::ipc::Response;
use tauri::{Emitter, Manager};
use voxa_core::SampleFormat;

type Reg<'a> = tauri::State<'a, Arc<Registry>>;
type Lic<'a> = tauri::State<'a, Arc<licence::Licensing>>;
type Clean<'a> = tauri::State<'a, cleanup::Shared>;
type Clones<'a> = tauri::State<'a, Arc<cloning::Cloning>>;

#[tauri::command]
fn list_voices(reg: Reg<'_>, clones: Clones<'_>) -> Result<Vec<Voice>, String> {
    let mut v = reg.voices()?;
    v.extend(clones.voices(&reg));
    Ok(v)
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
    lic: Lic<'_>,
) -> Result<bench::Report, String> {
    lic.require()?;
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
    lic: Lic<'_>,
    clones: Clones<'_>,
    text: String,
    voice: String,
    speed: f32,
) -> Result<Response, String> {
    lic.require()?;
    let (reg, clones) = (Arc::clone(&reg), Arc::clone(&clones));
    tauri::async_runtime::spawn_blocking(move || {
        if voice.starts_with(cloning::ID_PREFIX) {
            // Cloned voices: watermarked, and labelled in the file.
            return clones
                .synthesize(&reg, &voice, &text, speed)
                .map(|a| Response::new(cloning::labelled_wav(&a)));
        }
        reg.synthesize(&voice, &text, speed)
            .map(|a| Response::new(a.to_wav(SampleFormat::Pcm16)))
    })
    .await
    .map_err(|e| e.to_string())?
}

#[tauri::command]
fn licence_status(lic: Lic<'_>) -> licence::Status {
    lic.status()
}

/// Activates this device with a licence key (online, once).
#[tauri::command]
async fn activate_licence(lic: Lic<'_>, key: String) -> Result<licence::Status, String> {
    let lic = Arc::clone(&lic);
    let name = std::env::var("HOSTNAME")
        .or_else(|_| std::env::var("COMPUTERNAME"))
        .unwrap_or_else(|_| std::env::consts::OS.to_string());
    tauri::async_runtime::spawn_blocking(move || lic.activate(&key, &name))
        .await
        .map_err(|e| e.to_string())?
}

/// Frees this device's activation so the licence can move to another device.
#[tauri::command]
async fn deactivate_licence(lic: Lic<'_>) -> Result<licence::Status, String> {
    let lic = Arc::clone(&lic);
    tauri::async_runtime::spawn_blocking(move || lic.deactivate())
        .await
        .map_err(|e| e.to_string())?
}

/// Releases every device on the licence (lost or broken machine). Rate-limited.
#[tauri::command]
async fn reset_licence(lic: Lic<'_>, key: String) -> Result<i64, String> {
    let lic = Arc::clone(&lic);
    tauri::async_runtime::spawn_blocking(move || lic.reset(&key))
        .await
        .map_err(|e| e.to_string())?
}

#[tauri::command]
fn cleanup_models(reg: Reg<'_>) -> Result<Vec<registry::CleanupModel>, String> {
    reg.cleanup_models()
}

#[tauri::command]
async fn cleanup_open(clean: Clean<'_>, path: String) -> Result<cleanup::OpenedFile, String> {
    let clean = Arc::clone(&clean);
    tauri::async_runtime::spawn_blocking(move || clean.open(std::path::Path::new(&path)))
        .await
        .map_err(|e| e.to_string())?
}

#[derive(Clone, Serialize)]
struct CleanupProgress {
    done: usize,
    total: usize,
}

/// Cleans the open file with a clean-up model, emitting `cleanup-progress` per channel.
#[tauri::command]
async fn cleanup_run<R: tauri::Runtime>(
    app: tauri::AppHandle<R>,
    reg: Reg<'_>,
    lic: Lic<'_>,
    clean: Clean<'_>,
    model: String,
) -> Result<(), String> {
    lic.require()?;
    let (reg, clean) = (Arc::clone(&reg), Arc::clone(&clean));
    tauri::async_runtime::spawn_blocking(move || {
        clean.run(&reg, &model, &mut |done, total| {
            let _ = app.emit("cleanup-progress", CleanupProgress { done, total });
        })
    })
    .await
    .map_err(|e| e.to_string())?
}

/// Up to 30 s of the original or the cleaned mix, as a WAV for the A/B player.
#[tauri::command]
async fn cleanup_preview(
    clean: Clean<'_>,
    cleaned: bool,
    strength: f32,
    start: f64,
) -> Result<Response, String> {
    let clean = Arc::clone(&clean);
    tauri::async_runtime::spawn_blocking(move || {
        clean.preview(cleaned, strength, start).map(Response::new)
    })
    .await
    .map_err(|e| e.to_string())?
}

#[tauri::command]
fn cleanup_suggested_name(clean: Clean<'_>) -> Option<String> {
    clean.suggested_name()
}

/// Saves the cleaned file (same rate, channels, length and BWF/iXML chunks; 24-bit).
#[tauri::command]
async fn cleanup_save(
    lic: Lic<'_>,
    clean: Clean<'_>,
    path: String,
    strength: f32,
) -> Result<(), String> {
    lic.require()?;
    let clean = Arc::clone(&clean);
    tauri::async_runtime::spawn_blocking(move || clean.save(std::path::Path::new(&path), strength))
        .await
        .map_err(|e| e.to_string())?
}

/// A new consent sentence to read aloud (name, today's date, fresh code words).
#[tauri::command]
fn clone_challenge(
    clones: Clones<'_>,
    name: String,
) -> Result<voxa_engines::consent::Challenge, String> {
    clones.challenge(&name)
}

fn recording(samples: Vec<i16>, rate: u32) -> voxa_core::Audio {
    voxa_core::Audio::new(
        rate,
        samples.into_iter().map(|s| s as f32 / 32768.0).collect(),
    )
}

/// Checks the two live recordings (consent sentence, voice sample; 16-bit samples) and
/// creates the voice if the consent passes.
#[tauri::command]
async fn clone_create(
    reg: Reg<'_>,
    lic: Lic<'_>,
    clones: Clones<'_>,
    consent: Vec<i16>,
    consent_rate: u32,
    sample: Vec<i16>,
    sample_rate: u32,
) -> Result<cloning::CreateResult, String> {
    lic.require()?;
    let (reg, clones) = (Arc::clone(&reg), Arc::clone(&clones));
    tauri::async_runtime::spawn_blocking(move || {
        clones.create(
            &reg,
            &recording(consent, consent_rate),
            &recording(sample, sample_rate),
        )
    })
    .await
    .map_err(|e| e.to_string())?
}

/// Whether both cloning models (voice cloning, consent check) are installed.
#[tauri::command]
fn cloning_ready(reg: Reg<'_>) -> bool {
    reg.cloning_ready()
}

#[tauri::command]
fn remove_cloned(reg: Reg<'_>, clones: Clones<'_>, id: String) -> Result<(), String> {
    clones.remove(&reg, &id)
}

/// Checks a WAV file for the cloned-voice watermark and label.
#[tauri::command]
async fn watermark_check(path: String) -> Result<cloning::WatermarkReport, String> {
    tauri::async_runtime::spawn_blocking(move || cloning::check_file(std::path::Path::new(&path)))
        .await
        .map_err(|e| e.to_string())?
}

/// The webview may use the microphone (live recordings for "Your voices"; the OS still asks
/// the person on macOS, Windows and mobile) and nothing else: no camera, screen, location.
fn permission(kind: tauri::webview::PermissionKind) -> tauri::webview::PermissionResponse {
    use tauri::webview::{PermissionKind, PermissionResponse};
    match kind {
        PermissionKind::Microphone => PermissionResponse::Allow,
        _ => PermissionResponse::Deny,
    }
}

/// Registers Voxa's state and commands. `root` overrides the data directory (tests).
fn setup<R: tauri::Runtime>(
    builder: tauri::Builder<R>,
    root: Option<PathBuf>,
) -> tauri::Builder<R> {
    let builder = match root {
        // Known data directory (tests): register the state up front.
        Some(root) => builder
            .manage(Arc::new(
                Registry::open(&root, registry::trusted_keys()).expect("open model store"),
            ))
            .manage(Arc::new(licence::Licensing::new(
                &root,
                None,
                Vec::new(),
                voxa_licence::device_id(),
            )))
            .manage(Arc::new(
                cloning::Cloning::open(&root).expect("open cloned voices"),
            )),
        // The app's data directory is only known once the app exists.
        None => builder.setup(|app| {
            let root = app.path().app_data_dir()?;
            let reg = Registry::open(&root, registry::trusted_keys())?;
            app.manage(Arc::new(reg));
            app.manage(Arc::new(licence::Licensing::from_build(&root)));
            app.manage(Arc::new(cloning::Cloning::open(&root)?));
            Ok(())
        }),
    };
    let builder = builder
        .manage(cleanup::Shared::default())
        .on_permission_request(|_, kind| permission(kind));
    builder.invoke_handler(tauri::generate_handler![
        list_voices,
        list_models,
        import_pack,
        remove_model,
        synthesize,
        catalogue_configured,
        refresh_catalogue,
        install_model,
        run_benchmark,
        licence_status,
        activate_licence,
        deactivate_licence,
        reset_licence,
        cleanup_models,
        cleanup_open,
        cleanup_run,
        cleanup_preview,
        cleanup_suggested_name,
        cleanup_save,
        cloning_ready,
        clone_challenge,
        clone_create,
        remove_cloned,
        watermark_check
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
    fn only_the_microphone_is_allowed() {
        use tauri::webview::{PermissionKind, PermissionResponse};
        assert!(matches!(
            permission(PermissionKind::Microphone),
            PermissionResponse::Allow
        ));
        for k in [
            PermissionKind::Camera,
            PermissionKind::DisplayCapture,
            PermissionKind::Geolocation,
        ] {
            assert!(matches!(permission(k), PermissionResponse::Deny));
        }
    }

    #[test]
    fn cloning_commands_without_models() {
        let d = tempfile::tempdir().unwrap();
        let (_app, w) = window(d.path());
        let ready: bool = call(&w, "cloning_ready", serde_json::json!({}))
            .unwrap()
            .deserialize()
            .unwrap();
        assert!(!ready);
        let c: serde_json::Value = call(&w, "clone_challenge", serde_json::json!({"name": "Sam"}))
            .unwrap()
            .deserialize()
            .unwrap();
        assert!(c["sentence"].as_str().unwrap().contains("I am Sam"));
        let rec = vec![0i16; 16_000 * 11];
        let err = call(
            &w,
            "clone_create",
            serde_json::json!({"consent": rec, "consentRate": 16000, "sample": rec, "sampleRate": 16000}),
        )
        .unwrap_err();
        assert!(err.as_str().unwrap().contains("not installed"), "{err}");
        let err = call(
            &w,
            "synthesize",
            serde_json::json!({"text": "Hi.", "voice": "clone-00ff", "speed": 1.0}),
        )
        .unwrap_err();
        assert!(err.as_str().unwrap().contains("unknown voice"), "{err}");
        let err = call(&w, "remove_cloned", serde_json::json!({"id": "../models"})).unwrap_err();
        assert!(err.as_str().unwrap().contains("unknown voice"), "{err}");
        let err = call(
            &w,
            "watermark_check",
            serde_json::json!({"path": "/nonexistent.wav"}),
        )
        .unwrap_err();
        assert!(err.as_str().unwrap().contains("nonexistent"), "{err}");
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
    fn test_builds_do_not_use_activation() {
        let d = tempfile::tempdir().unwrap();
        let (_app, w) = window(d.path());
        let s: serde_json::Value = call(&w, "licence_status", serde_json::json!({}))
            .unwrap()
            .deserialize()
            .unwrap();
        assert_eq!(s["enabled"], false);
        assert_eq!(s["active"], true);
        let err = call(&w, "activate_licence", serde_json::json!({"key": "VOXA-X"})).unwrap_err();
        assert!(err.as_str().unwrap().contains("does not use activation"));
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
