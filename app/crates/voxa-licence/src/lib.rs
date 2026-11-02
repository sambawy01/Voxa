//! Licences for copies of Voxa sold directly (App Store and Google Play copies don't use
//! this: the stores handle those purchases).
//!
//! From the plan (docs/NATIVE_APP_VOICES.md): activate once online, then run **offline
//! forever** by checking a licence file signed by Voxa's licence server
//! (`licence-server/licence_server.py`); two activations per licence; deactivate to move.
//!
//! The licence file is `{"payload": "<JSON>", "sig": "<base64 Ed25519 over payload>"}`.
//! Verification needs no network: the signature must match a public key built into the
//! app, and the payload must name this product and this device.

use std::path::{Path, PathBuf};

use base64::Engine as _;
use ed25519_dalek::{Signature, VerifyingKey};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use thiserror::Error;

pub const PRODUCT: &str = "voxa-desktop";

#[derive(Debug, Clone, Error, PartialEq)]
pub enum LicenceError {
    #[error("the licence file is damaged")]
    Malformed,
    #[error("the licence was not issued by Voxa")]
    BadSignature,
    #[error("this licence belongs to another device")]
    OtherDevice,
    #[error("this licence is for another product")]
    OtherProduct,
    #[error("{0}")]
    Server(String),
    #[error("could not reach the licence server: {0}")]
    Network(String),
    #[error("disk error: {0}")]
    Io(String),
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct LicenceFile {
    pub payload: String,
    pub sig: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Payload {
    pub v: u32,
    pub product: String,
    /// Short hash prefix naming the licence (never the key itself).
    pub licence: String,
    pub device: String,
    pub activation: String,
    pub seats: u32,
    pub issued: String,
}

/// A stable, anonymous id for this device: a hash of a device identifier with a Voxa
/// prefix, so it can't be matched with ids other apps send.
///
/// Desktops use the OS machine id (falling back to a random id kept in the home folder).
/// Phones expose no machine id to apps, so there it is a random id kept in `app_dir`, the
/// app's own data folder: reinstalling the app makes a new id, which counts as a new
/// device until the old one is deactivated or released.
pub fn device_id(app_dir: &Path) -> String {
    device_id_from(&raw_device_id(app_dir))
}

#[cfg(not(any(target_os = "android", target_os = "ios")))]
fn raw_device_id(_app_dir: &Path) -> String {
    machine_uid::get().unwrap_or_else(|_| {
        let home = std::env::var_os("HOME")
            .or_else(|| std::env::var_os("USERPROFILE"))
            .map(PathBuf::from)
            .unwrap_or_else(std::env::temp_dir);
        stored_id(&home, ".voxa-machine-id")
    })
}

#[cfg(any(target_os = "android", target_os = "ios"))]
fn raw_device_id(app_dir: &Path) -> String {
    stored_id(app_dir, "device-id")
}

pub fn device_id_from(machine: &str) -> String {
    let h = Sha256::digest(format!("voxa-device-v1:{}", machine.trim()).as_bytes());
    hex::encode(&h[..16])
}

/// A random id kept in `dir/name`, created on first use, so it stays stable for this
/// user account (desktop fallback) or this app install (phones).
fn stored_id(dir: &Path, name: &str) -> String {
    let path = dir.join(name);
    if let Ok(s) = std::fs::read_to_string(&path) {
        if !s.trim().is_empty() {
            return s.trim().to_string();
        }
    }
    // Time, process id and a stack address: enough for a per-install identifier.
    let t = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos();
    let marker = 0u8;
    let digest = Sha256::digest(format!("{t}:{}:{:p}", std::process::id(), &marker));
    let id = hex::encode(&digest[..16]);
    let _ = std::fs::create_dir_all(dir);
    let _ = std::fs::write(&path, &id);
    id
}

/// Verifies a licence offline against the built-in public keys and this device.
pub fn verify(
    file: &LicenceFile,
    keys: &[[u8; 32]],
    device: &str,
) -> Result<Payload, LicenceError> {
    let sig = base64::engine::general_purpose::STANDARD
        .decode(file.sig.trim())
        .map_err(|_| LicenceError::Malformed)?;
    let sig = Signature::from_slice(&sig).map_err(|_| LicenceError::Malformed)?;
    let ok = keys.iter().any(|k| {
        VerifyingKey::from_bytes(k)
            .map(|vk| vk.verify_strict(file.payload.as_bytes(), &sig).is_ok())
            .unwrap_or(false)
    });
    if !ok {
        return Err(LicenceError::BadSignature);
    }
    let p: Payload = serde_json::from_str(&file.payload).map_err(|_| LicenceError::Malformed)?;
    if p.v != 1 || p.product != PRODUCT {
        return Err(LicenceError::OtherProduct);
    }
    if p.device != device {
        return Err(LicenceError::OtherDevice);
    }
    Ok(p)
}

/// Where the activated licence lives on this device.
pub struct LicenceStore {
    path: PathBuf,
}

impl LicenceStore {
    pub fn new(data_dir: &Path) -> Self {
        Self {
            path: data_dir.join("licence.json"),
        }
    }

    pub fn load(&self) -> Option<LicenceFile> {
        serde_json::from_slice(&std::fs::read(&self.path).ok()?).ok()
    }

    pub fn save(&self, file: &LicenceFile) -> Result<(), LicenceError> {
        if let Some(dir) = self.path.parent() {
            std::fs::create_dir_all(dir).map_err(|e| LicenceError::Io(e.to_string()))?;
        }
        let tmp = self.path.with_extension("json.tmp");
        std::fs::write(&tmp, serde_json::to_vec_pretty(file).unwrap())
            .and_then(|_| std::fs::rename(&tmp, &self.path))
            .map_err(|e| LicenceError::Io(e.to_string()))
    }

    pub fn clear(&self) -> Result<(), LicenceError> {
        match std::fs::remove_file(&self.path) {
            Ok(()) => Ok(()),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(e) => Err(LicenceError::Io(e.to_string())),
        }
    }
}

/// Talks to the licence server. Only used to activate, deactivate or reset: running the
/// app never needs it.
pub struct Client {
    base: String,
    agent: ureq::Agent,
}

#[derive(Deserialize)]
struct ActivateReply {
    licence: LicenceFile,
}

#[derive(Deserialize)]
struct ErrorReply {
    error: String,
}

#[derive(Deserialize)]
struct ResetReply {
    resets_left: i64,
}

impl Client {
    pub fn new(base_url: &str) -> Self {
        let agent = ureq::Agent::config_builder()
            .http_status_as_error(false)
            .timeout_global(Some(std::time::Duration::from_secs(30)))
            .build()
            .into();
        Self {
            base: base_url.trim_end_matches('/').to_string(),
            agent,
        }
    }

    fn post<T: serde::de::DeserializeOwned>(
        &self,
        path: &str,
        body: serde_json::Value,
    ) -> Result<T, LicenceError> {
        let mut resp = self
            .agent
            .post(&format!("{}{path}", self.base))
            .send_json(body)
            .map_err(|e| LicenceError::Network(e.to_string()))?;
        let status = resp.status().as_u16();
        let text = resp
            .body_mut()
            .read_to_string()
            .map_err(|e| LicenceError::Network(e.to_string()))?;
        if status != 200 {
            let msg = serde_json::from_str::<ErrorReply>(&text)
                .map(|e| e.error)
                .unwrap_or_else(|_| format!("the licence server answered {status}"));
            return Err(LicenceError::Server(msg));
        }
        serde_json::from_str(&text).map_err(|_| LicenceError::Malformed)
    }

    pub fn activate(
        &self,
        key: &str,
        device: &str,
        name: &str,
    ) -> Result<LicenceFile, LicenceError> {
        let r: ActivateReply = self.post(
            "/v1/activate",
            serde_json::json!({"key": key, "device_id": device, "device_name": name}),
        )?;
        Ok(r.licence)
    }

    pub fn deactivate(&self, key: &str, activation: &str) -> Result<(), LicenceError> {
        let _: serde_json::Value = self.post(
            "/v1/deactivate",
            serde_json::json!({"key": key, "activation": activation}),
        )?;
        Ok(())
    }

    /// Releases every activation of the licence (self-service, rate-limited by the
    /// server). Returns the resets left this year.
    pub fn reset(&self, key: &str) -> Result<i64, LicenceError> {
        let r: ResetReply = self.post("/v1/reset", serde_json::json!({"key": key}))?;
        Ok(r.resets_left)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The vector the Python licence server signs (licence-server/tests).
    fn vector() -> (LicenceFile, [u8; 32]) {
        let v: serde_json::Value = serde_json::from_str(include_str!(
            "../../../../licence-server/tests/licence_vector.json"
        ))
        .unwrap();
        let file: LicenceFile = serde_json::from_value(v["licence"].clone()).unwrap();
        let key: [u8; 32] = hex::decode(v["public_key"].as_str().unwrap())
            .unwrap()
            .try_into()
            .unwrap();
        (file, key)
    }

    #[test]
    fn verifies_a_licence_signed_by_the_python_server() {
        let (file, key) = vector();
        let p = verify(&file, &[key], &"d".repeat(32)).unwrap();
        assert_eq!(p.product, PRODUCT);
        assert_eq!(p.seats, 2);
    }

    #[test]
    fn rejects_tampering_other_devices_and_unknown_keys() {
        let (file, key) = vector();
        let mut tampered = file.clone();
        tampered.payload = tampered.payload.replace("\"seats\":2", "\"seats\":9");
        assert_eq!(
            verify(&tampered, &[key], &"d".repeat(32)),
            Err(LicenceError::BadSignature)
        );
        assert_eq!(
            verify(&file, &[key], &"e".repeat(32)),
            Err(LicenceError::OtherDevice)
        );
        assert_eq!(
            verify(&file, &[[7u8; 32]], &"d".repeat(32)),
            Err(LicenceError::BadSignature)
        );
        let mut broken = file;
        broken.sig = "!!".into();
        assert_eq!(
            verify(&broken, &[key], &"d".repeat(32)),
            Err(LicenceError::Malformed)
        );
    }

    #[test]
    fn device_id_is_stable_hex_and_salted() {
        let a = device_id_from("machine-123");
        assert_eq!(a, device_id_from(" machine-123\n"));
        assert_ne!(a, device_id_from("machine-124"));
        assert_eq!(a.len(), 32);
        assert!(a.chars().all(|c| c.is_ascii_hexdigit()));
        // The raw machine id is not recoverable from, nor equal to, the hash.
        assert!(!a.contains("machine"));
        let d = tempfile::tempdir().unwrap();
        let here = device_id(d.path());
        assert_eq!(here, device_id(d.path()), "same machine, same id");
    }

    #[test]
    fn stored_ids_are_created_once_and_kept() {
        let d = tempfile::tempdir().unwrap();
        let dir = d.path().join("app-data");
        let a = stored_id(&dir, "device-id");
        assert_eq!(a.len(), 32);
        assert_eq!(std::fs::read_to_string(dir.join("device-id")).unwrap(), a);
        assert_eq!(stored_id(&dir, "device-id"), a, "kept across launches");
        let other = tempfile::tempdir().unwrap();
        assert_ne!(
            stored_id(other.path(), "device-id"),
            a,
            "a new install, a new id"
        );
    }

    #[test]
    fn store_saves_loads_and_clears() {
        let d = tempfile::tempdir().unwrap();
        let s = LicenceStore::new(d.path());
        assert!(s.load().is_none());
        let (file, _) = vector();
        s.save(&file).unwrap();
        assert_eq!(s.load(), Some(file));
        s.clear().unwrap();
        assert!(s.load().is_none());
        s.clear().unwrap(); // clearing twice is fine
    }

    /// A canned HTTP server for the client tests.
    fn serve(status: u16, body: &'static str) -> String {
        use std::io::{BufRead, BufReader, Read, Write};
        let l = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let base = format!("http://{}", l.local_addr().unwrap());
        std::thread::spawn(move || {
            for s in l.incoming() {
                let Ok(mut s) = s else { continue };
                let mut r = BufReader::new(s.try_clone().unwrap());
                let mut len = 0usize;
                loop {
                    let mut line = String::new();
                    if r.read_line(&mut line).unwrap_or(0) == 0 || line == "\r\n" {
                        break;
                    }
                    if let Some(v) = line.to_ascii_lowercase().strip_prefix("content-length:") {
                        len = v.trim().parse().unwrap_or(0);
                    }
                }
                let mut buf = vec![0; len];
                let _ = r.read_exact(&mut buf);
                let _ = write!(
                    s,
                    "HTTP/1.1 {status} X\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                    body.len()
                );
            }
        });
        base
    }

    #[test]
    fn client_reports_server_errors_in_plain_words() {
        let base = serve(
            409,
            r#"{"error": "this licence is already active on 2 devices; deactivate one"}"#,
        );
        let err = Client::new(&base)
            .activate("VOXA-AAAA", &"d".repeat(32), "Mac")
            .unwrap_err();
        assert_eq!(
            err,
            LicenceError::Server(
                "this licence is already active on 2 devices; deactivate one".into()
            )
        );
    }

    #[test]
    fn client_activates_and_resets() {
        let (file, _) = vector();
        let body: &'static str = Box::leak(
            serde_json::json!({ "licence": file })
                .to_string()
                .into_boxed_str(),
        );
        let got = Client::new(&serve(200, body))
            .activate("VOXA-AAAA", &"d".repeat(32), "Mac")
            .unwrap();
        assert_eq!(got, file);
        let left = Client::new(&serve(200, r#"{"ok": true, "resets_left": 2}"#))
            .reset("VOXA-AAAA")
            .unwrap();
        assert_eq!(left, 2);
        assert!(matches!(
            Client::new("http://127.0.0.1:1").reset("x"),
            Err(LicenceError::Network(_))
        ));
    }
}
