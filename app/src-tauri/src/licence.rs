//! Activation for copies sold directly. Enabled only when the build sets
//! `VOXA_LICENCE_URL` and `VOXA_LICENCE_PUBKEY`; development builds and App Store / Google
//! Play builds (whose stores handle the purchase) leave them unset and never ask.

use std::path::{Path, PathBuf};
use std::sync::Mutex;

use serde::Serialize;
use voxa_licence::{device_id, verify, Client, LicenceError, LicenceStore, Payload};

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct Status {
    /// False for builds that don't use activation (development, app stores).
    pub enabled: bool,
    pub active: bool,
    /// Why an existing licence file was not accepted, if it wasn't.
    pub problem: Option<String>,
    pub activation: Option<String>,
    pub issued: Option<String>,
    pub seats: Option<u32>,
}

pub struct Licensing {
    url: Option<String>,
    keys: Vec<[u8; 32]>,
    store: LicenceStore,
    key_path: PathBuf,
    device: String,
    /// Cached verification result; licences are checked once per start, offline.
    cached: Mutex<Option<Result<Payload, LicenceError>>>,
}

fn parse_key(hex_key: &str) -> Option<[u8; 32]> {
    hex::decode(hex_key.trim()).ok()?.try_into().ok()
}

impl Licensing {
    /// Uses the build's settings.
    pub fn from_build(data_dir: &Path) -> Self {
        Self::new(
            data_dir,
            option_env!("VOXA_LICENCE_URL").map(str::to_string),
            option_env!("VOXA_LICENCE_PUBKEY")
                .and_then(parse_key)
                .into_iter()
                .collect(),
            device_id(),
        )
    }

    pub fn new(data_dir: &Path, url: Option<String>, keys: Vec<[u8; 32]>, device: String) -> Self {
        Self {
            url,
            keys,
            store: LicenceStore::new(data_dir),
            key_path: data_dir.join("licence-key.txt"),
            device,
            cached: Mutex::new(None),
        }
    }

    pub fn enabled(&self) -> bool {
        self.url.is_some() && !self.keys.is_empty()
    }

    fn check(&self) -> Result<Payload, LicenceError> {
        let mut c = self.cached.lock().unwrap();
        if c.is_none() {
            *c = Some(match self.store.load() {
                Some(file) => verify(&file, &self.keys, &self.device),
                None => Err(LicenceError::Malformed),
            });
        }
        c.clone().unwrap()
    }

    pub fn status(&self) -> Status {
        if !self.enabled() {
            return Status {
                enabled: false,
                active: true,
                problem: None,
                activation: None,
                issued: None,
                seats: None,
            };
        }
        let has_file = self.store.load().is_some();
        match self.check() {
            Ok(p) => Status {
                enabled: true,
                active: true,
                problem: None,
                activation: Some(p.activation),
                issued: Some(p.issued),
                seats: Some(p.seats),
            },
            Err(e) => Status {
                enabled: true,
                active: false,
                problem: has_file.then(|| e.to_string()),
                activation: None,
                issued: None,
                seats: None,
            },
        }
    }

    /// Ok when the app may read aloud: activation not used, or a valid licence.
    pub fn require(&self) -> Result<(), String> {
        if !self.enabled() || self.check().is_ok() {
            Ok(())
        } else {
            Err(
                "Voxa is not activated on this device yet. Enter your licence key under \
                 Licence to start reading."
                    .into(),
            )
        }
    }

    fn client(&self) -> Result<Client, String> {
        self.url
            .as_deref()
            .map(Client::new)
            .ok_or_else(|| "this build does not use activation".to_string())
    }

    /// Activates this device online once; afterwards everything works offline.
    pub fn activate(&self, key: &str, device_name: &str) -> Result<Status, String> {
        let file = self
            .client()?
            .activate(key.trim(), &self.device, device_name)
            .map_err(|e| e.to_string())?;
        // Never store a licence the app itself would refuse.
        verify(&file, &self.keys, &self.device).map_err(|e| e.to_string())?;
        self.store.save(&file).map_err(|e| e.to_string())?;
        std::fs::write(&self.key_path, key.trim()).map_err(|e| e.to_string())?;
        *self.cached.lock().unwrap() = None;
        Ok(self.status())
    }

    /// Frees this device's activation so the licence can be used on another device.
    pub fn deactivate(&self) -> Result<Status, String> {
        let p = self.check().map_err(|e| e.to_string())?;
        let key = std::fs::read_to_string(&self.key_path)
            .map_err(|_| "the licence key for this device is missing".to_string())?;
        self.client()?
            .deactivate(key.trim(), &p.activation)
            .map_err(|e| e.to_string())?;
        self.store.clear().map_err(|e| e.to_string())?;
        let _ = std::fs::remove_file(&self.key_path);
        *self.cached.lock().unwrap() = None;
        Ok(self.status())
    }

    /// Releases every device on the licence (for a lost or broken machine). Rate-limited by
    /// the server; returns the resets left this year.
    pub fn reset(&self, key: &str) -> Result<i64, String> {
        self.client()?.reset(key.trim()).map_err(|e| e.to_string())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn vector() -> (voxa_licence::LicenceFile, [u8; 32]) {
        let v: serde_json::Value = serde_json::from_str(include_str!(
            "../../../licence-server/tests/licence_vector.json"
        ))
        .unwrap();
        (
            serde_json::from_value(v["licence"].clone()).unwrap(),
            parse_key(v["public_key"].as_str().unwrap()).unwrap(),
        )
    }

    #[test]
    fn disabled_builds_never_ask() {
        let d = tempfile::tempdir().unwrap();
        let l = Licensing::new(d.path(), None, vec![], "d".repeat(32));
        assert!(!l.enabled());
        assert!(l.require().is_ok());
        let s = l.status();
        assert!(!s.enabled && s.active);
        assert!(l.activate("VOXA-X", "Mac").is_err());
    }

    #[test]
    fn enabled_builds_need_a_valid_licence_for_this_device() {
        let d = tempfile::tempdir().unwrap();
        let (file, key) = vector();
        let url = Some("http://127.0.0.1:1".to_string());
        let l = Licensing::new(d.path(), url.clone(), vec![key], "d".repeat(32));
        assert!(l.require().is_err());
        assert_eq!(l.status().problem, None, "no licence yet is not a problem");

        LicenceStore::new(d.path()).save(&file).unwrap();
        let l = Licensing::new(d.path(), url.clone(), vec![key], "d".repeat(32));
        assert!(l.require().is_ok());
        let s = l.status();
        assert!(s.active);
        assert_eq!(s.seats, Some(2));

        // The same file copied to another machine is refused, with a reason.
        let other = Licensing::new(d.path(), url, vec![key], "e".repeat(32));
        assert!(other.require().is_err());
        assert!(other.status().problem.unwrap().contains("another device"));
    }

    /// Full cycle against the real Python licence server. Needs python3 with
    /// `cryptography`: `cargo test -p voxa-app -- --ignored licence_e2e`.
    #[test]
    #[ignore = "starts the Python licence server (needs python3 + cryptography)"]
    fn licence_e2e() {
        use std::process::{Command, Stdio};
        let d = tempfile::tempdir().unwrap();
        let server = concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/../../licence-server/licence_server.py"
        );
        let run = |args: &[&str]| {
            let out = Command::new("python3")
                .arg(server)
                .args(args)
                .output()
                .unwrap();
            assert!(
                out.status.success(),
                "{}",
                String::from_utf8_lossy(&out.stderr)
            );
            String::from_utf8(out.stdout).unwrap().trim().to_string()
        };
        let secret = d.path().join("secret");
        let db = d.path().join("l.db");
        let public = run(&["keygen", secret.to_str().unwrap()]);
        let key = run(&["create", db.to_str().unwrap(), "e2e"]);
        let port = std::net::TcpListener::bind("127.0.0.1:0")
            .unwrap()
            .local_addr()
            .unwrap()
            .port();
        let mut child = Command::new("python3")
            .arg(server)
            .args(["serve", db.to_str().unwrap(), secret.to_str().unwrap()])
            .arg(port.to_string())
            .stdout(Stdio::null())
            .spawn()
            .unwrap();
        let url = format!("http://127.0.0.1:{port}");
        for _ in 0..50 {
            if std::net::TcpStream::connect(("127.0.0.1", port)).is_ok() {
                break;
            }
            std::thread::sleep(std::time::Duration::from_millis(100));
        }
        let keys = vec![parse_key(&public).unwrap()];
        let dev = |c: char| c.to_string().repeat(32);
        let a = tempfile::tempdir().unwrap();
        let b = tempfile::tempdir().unwrap();
        let c = tempfile::tempdir().unwrap();
        let mac = Licensing::new(a.path(), Some(url.clone()), keys.clone(), dev('a'));
        let pc = Licensing::new(b.path(), Some(url.clone()), keys.clone(), dev('b'));
        let third = Licensing::new(c.path(), Some(url.clone()), keys.clone(), dev('c'));

        assert!(mac.activate(&key, "Mac").unwrap().active);
        assert!(pc.activate(&key.to_lowercase(), "PC").unwrap().active);
        let err = third.activate(&key, "Laptop").unwrap_err();
        assert!(err.contains("2 devices"), "{err}");
        // Moving: deactivate the Mac, then the laptop can activate.
        assert!(!mac.deactivate().unwrap().active);
        assert!(third.activate(&key, "Laptop").unwrap().active);
        // Lost machine: reset releases everything.
        assert_eq!(mac.reset(&key).unwrap(), 2);
        assert!(mac.activate(&key, "Mac").unwrap().active);
        // Offline use: a fresh instance with an unreachable server still runs.
        let offline = Licensing::new(a.path(), Some("http://127.0.0.1:1".into()), keys, dev('a'));
        assert!(offline.require().is_ok());
        child.kill().unwrap();
        child.wait().unwrap();
    }
}
