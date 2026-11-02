//! The on-disk model store.
//!
//! Layout under the app's data directory:
//!
//! ```text
//! models/<id>/<version>/<files...>       installed models, one directory per version
//! models/<id>/<version>/.voxa-model.json the catalogue entry, written last = "installed"
//! staging/<id>/<version>/<file>.part     resumable downloads
//! ```
//!
//! The "works forever" promise lives here: installing a new version never touches older
//! ones, and nothing is deleted except by [`ModelStore::remove`], which the UI only calls
//! when the user asks.

use std::fs;
use std::path::{Path, PathBuf};

use thiserror::Error;

use crate::catalogue::{safe_id, safe_rel_path, ModelEntry};
use crate::download::{sha256_file, DownloadError, Downloader};

const MARKER: &str = ".voxa-model.json";
/// Sequence of the last catalogue accepted, for rollback protection across restarts.
const SEQUENCE_FILE: &str = "catalogue-sequence";

#[derive(Debug, Error)]
pub enum StoreError {
    #[error(transparent)]
    Download(#[from] DownloadError),
    #[error("disk error: {0}")]
    Io(#[from] std::io::Error),
    #[error("model {0} {1} is not installed")]
    NotInstalled(String, String),
    #[error("model {0} is no longer offered for download")]
    Retired(String),
    #[error("unsafe model id, version or path")]
    Unsafe,
}

#[derive(Debug, Clone, PartialEq)]
pub struct InstalledModel {
    pub entry: ModelEntry,
    pub dir: PathBuf,
}

pub struct ModelStore {
    root: PathBuf,
}

impl ModelStore {
    pub fn open(root: impl Into<PathBuf>) -> Result<Self, StoreError> {
        let root = root.into();
        fs::create_dir_all(root.join("models"))?;
        fs::create_dir_all(root.join("staging"))?;
        Ok(Self { root })
    }

    /// Sequence of the last accepted catalogue (0 before the first).
    pub fn last_sequence(&self) -> u64 {
        fs::read_to_string(self.root.join(SEQUENCE_FILE))
            .ok()
            .and_then(|s| s.trim().parse().ok())
            .unwrap_or(0)
    }

    /// Records an accepted catalogue's sequence. Never moves backwards.
    pub fn record_sequence(&self, sequence: u64) -> Result<(), StoreError> {
        if sequence > self.last_sequence() {
            let tmp = self.root.join(format!("{SEQUENCE_FILE}.tmp"));
            fs::write(&tmp, sequence.to_string())?;
            fs::rename(tmp, self.root.join(SEQUENCE_FILE))?;
        }
        Ok(())
    }

    fn dir(&self, id: &str, version: &str) -> Result<PathBuf, StoreError> {
        if !safe_id(id) || !safe_id(version) {
            return Err(StoreError::Unsafe);
        }
        Ok(self.root.join("models").join(id).join(version))
    }

    pub fn is_installed(&self, id: &str, version: &str) -> bool {
        self.dir(id, version)
            .map(|d| d.join(MARKER).is_file())
            .unwrap_or(false)
    }

    /// Every installed model version. Half-finished installs (no marker) are not listed.
    pub fn installed(&self) -> Result<Vec<InstalledModel>, StoreError> {
        let mut out = Vec::new();
        for id in read_dirs(&self.root.join("models"))? {
            for ver in read_dirs(&id)? {
                let marker = ver.join(MARKER);
                let Ok(raw) = fs::read(&marker) else { continue };
                if let Ok(entry) = serde_json::from_slice::<ModelEntry>(&raw) {
                    out.push(InstalledModel { entry, dir: ver });
                }
            }
        }
        out.sort_by(|a, b| {
            (a.entry.id.as_str(), a.entry.version.as_str())
                .cmp(&(b.entry.id.as_str(), b.entry.version.as_str()))
        });
        Ok(out)
    }

    /// Path of one of an installed model's files.
    pub fn file(&self, id: &str, version: &str, path: &str) -> Result<PathBuf, StoreError> {
        if !safe_rel_path(path) {
            return Err(StoreError::Unsafe);
        }
        if !self.is_installed(id, version) {
            return Err(StoreError::NotInstalled(id.into(), version.into()));
        }
        Ok(self.dir(id, version)?.join(path))
    }

    /// Downloads and installs a model version. Already-installed versions are left alone;
    /// other versions of the same model are never touched. `on_progress(done, total)` is
    /// in bytes across all files. An interrupted install resumes on the next call.
    pub fn install(
        &self,
        entry: &ModelEntry,
        downloader: &Downloader,
        on_progress: &mut dyn FnMut(u64, u64),
    ) -> Result<InstalledModel, StoreError> {
        let dir = self.dir(&entry.id, &entry.version)?;
        if self.is_installed(&entry.id, &entry.version) {
            return Ok(InstalledModel {
                entry: entry.clone(),
                dir,
            });
        }
        if entry.retired {
            return Err(StoreError::Retired(entry.id.clone()));
        }
        if !entry.files.iter().all(|f| safe_rel_path(&f.path)) {
            return Err(StoreError::Unsafe);
        }
        let staging = self
            .root
            .join("staging")
            .join(&entry.id)
            .join(&entry.version);
        let total: u64 = entry.files.iter().map(|f| f.size).sum();
        let mut done_before = 0u64;
        for f in &entry.files {
            let dest = staging.join(&f.path);
            if let Some(parent) = dest.parent() {
                fs::create_dir_all(parent)?;
            }
            if !dest.is_file() {
                let part = PathBuf::from(format!("{}.part", dest.display()));
                downloader.fetch(f, &part, &dest, &mut |n, _| {
                    on_progress(done_before + n, total)
                })?;
            }
            done_before += f.size;
        }
        self.finish(entry, &staging, &dir)?;
        on_progress(total, total);
        Ok(InstalledModel {
            entry: entry.clone(),
            dir,
        })
    }

    /// Installs a model whose files are already on disk (an imported voice pack). Every
    /// file is copied into staging and checked against the entry's size and sha256 before
    /// anything is installed; the source directory is never modified.
    pub fn install_from_dir(
        &self,
        entry: &ModelEntry,
        src: &Path,
    ) -> Result<InstalledModel, StoreError> {
        let dir = self.dir(&entry.id, &entry.version)?;
        if self.is_installed(&entry.id, &entry.version) {
            return Ok(InstalledModel {
                entry: entry.clone(),
                dir,
            });
        }
        if !entry.files.iter().all(|f| safe_rel_path(&f.path)) {
            return Err(StoreError::Unsafe);
        }
        let staging = self
            .root
            .join("staging")
            .join(&entry.id)
            .join(&entry.version);
        if staging.exists() {
            fs::remove_dir_all(&staging)?;
        }
        for f in &entry.files {
            let from = src.join(&f.path);
            let to = staging.join(&f.path);
            if let Some(parent) = to.parent() {
                fs::create_dir_all(parent)?;
            }
            fs::copy(&from, &to).map_err(|e| {
                StoreError::Io(std::io::Error::new(e.kind(), format!("{}: {e}", f.path)))
            })?;
            let size = fs::metadata(&to)?.len();
            if size != f.size {
                fs::remove_dir_all(&staging)?;
                return Err(DownloadError::WrongSize {
                    path: f.path.clone(),
                    expected: f.size,
                    got: size,
                }
                .into());
            }
            if sha256_file(&to)? != f.sha256.to_ascii_lowercase() {
                fs::remove_dir_all(&staging)?;
                return Err(DownloadError::HashMismatch(f.path.clone()).into());
            }
        }
        self.finish(entry, &staging, &dir)?;
        Ok(InstalledModel {
            entry: entry.clone(),
            dir,
        })
    }

    /// Moves a fully verified staging directory into place, then writes the marker last so
    /// a crash mid-move never looks installed.
    fn finish(&self, entry: &ModelEntry, staging: &Path, dir: &Path) -> Result<(), StoreError> {
        if dir.exists() {
            fs::remove_dir_all(dir)?; // leftovers of a crashed move of this same version
        }
        fs::create_dir_all(dir.parent().expect("model dir has a parent"))?;
        fs::rename(staging, dir)?;
        fs::write(dir.join(MARKER), serde_json::to_vec_pretty(entry).unwrap())?;
        Ok(())
    }

    /// Deletes one installed version. Only called when the user asks.
    pub fn remove(&self, id: &str, version: &str) -> Result<(), StoreError> {
        if !self.is_installed(id, version) {
            return Err(StoreError::NotInstalled(id.into(), version.into()));
        }
        let dir = self.dir(id, version)?;
        // Remove the marker first so a crash mid-delete never leaves a broken "installed".
        fs::remove_file(dir.join(MARKER))?;
        fs::remove_dir_all(&dir)?;
        let parent = self.root.join("models").join(id);
        if fs::read_dir(&parent)?.next().is_none() {
            fs::remove_dir(&parent)?;
        }
        Ok(())
    }
}

fn read_dirs(p: &Path) -> std::io::Result<Vec<PathBuf>> {
    let mut v = Vec::new();
    if !p.is_dir() {
        return Ok(v);
    }
    for e in fs::read_dir(p)? {
        let e = e?;
        if e.file_type()?.is_dir() {
            v.push(e.path());
        }
    }
    Ok(v)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::catalogue::tests::model;
    use crate::download::tests::{blob, file_for, TestServer};
    use crate::ModelFile;

    fn entry_served(id: &str, version: &str, bodies: &[(&str, Vec<u8>)]) -> ModelEntry {
        let mut m = model(id, version, "0.1.0");
        m.files = bodies
            .iter()
            .map(|(path, body)| {
                let srv = TestServer::start(body.clone(), true, None);
                ModelFile {
                    path: path.to_string(),
                    ..file_for(body, &srv.url)
                }
            })
            .collect();
        m
    }

    #[test]
    fn installs_lists_and_finds_files() {
        let dir = tempfile::tempdir().unwrap();
        let store = ModelStore::open(dir.path()).unwrap();
        let e = entry_served(
            "kitten-nano",
            "0.8.0",
            &[("model.onnx", blob(5000)), ("voices/af.bin", blob(700))],
        );
        let mut last = (0, 0);
        store
            .install(&e, &Downloader::for_local_tests(), &mut |a, b| {
                last = (a, b)
            })
            .unwrap();
        assert_eq!(last, (5700, 5700));
        assert!(store.is_installed("kitten-nano", "0.8.0"));
        let f = store.file("kitten-nano", "0.8.0", "voices/af.bin").unwrap();
        assert_eq!(fs::read(f).unwrap(), blob(700));
        let list = store.installed().unwrap();
        assert_eq!(list.len(), 1);
        assert_eq!(list[0].entry, e);
    }

    #[test]
    fn new_versions_never_replace_old_ones() {
        let dir = tempfile::tempdir().unwrap();
        let store = ModelStore::open(dir.path()).unwrap();
        let d = Downloader::for_local_tests();
        let v1 = entry_served("kokoro", "1.0.0", &[("m.onnx", blob(100))]);
        let v2 = entry_served("kokoro", "1.1.0", &[("m.onnx", blob(200))]);
        store.install(&v1, &d, &mut |_, _| {}).unwrap();
        store.install(&v2, &d, &mut |_, _| {}).unwrap();
        let versions: Vec<String> = store
            .installed()
            .unwrap()
            .into_iter()
            .map(|m| m.entry.version)
            .collect();
        assert_eq!(versions, ["1.0.0", "1.1.0"]);
        assert_eq!(
            fs::read(store.file("kokoro", "1.0.0", "m.onnx").unwrap()).unwrap(),
            blob(100)
        );
    }

    #[test]
    fn reinstall_is_a_no_op_and_does_not_download() {
        let dir = tempfile::tempdir().unwrap();
        let store = ModelStore::open(dir.path()).unwrap();
        let body = blob(100);
        let srv = TestServer::start(body.clone(), true, None);
        let mut e = model("a", "1", "0.1.0");
        e.files = vec![file_for(&body, &srv.url)];
        let d = Downloader::for_local_tests();
        store.install(&e, &d, &mut |_, _| {}).unwrap();
        store.install(&e, &d, &mut |_, _| {}).unwrap();
        assert_eq!(
            srv.requests.load(std::sync::atomic::Ordering::SeqCst),
            1,
            "second install must not hit the network"
        );
    }

    #[test]
    fn failed_install_is_not_listed_and_resumes() {
        let dir = tempfile::tempdir().unwrap();
        let store = ModelStore::open(dir.path()).unwrap();
        let body = blob(300_000);
        let srv = TestServer::start(body.clone(), true, Some(120_000));
        let mut e = model("big", "1", "0.1.0");
        e.files = vec![file_for(&body, &srv.url)];
        let d = Downloader::for_local_tests();
        assert!(store.install(&e, &d, &mut |_, _| {}).is_err());
        assert!(!store.is_installed("big", "1"));
        assert!(store.installed().unwrap().is_empty());
        store.install(&e, &d, &mut |_, _| {}).unwrap();
        assert!(store.is_installed("big", "1"));
    }

    #[test]
    fn retired_models_are_not_downloaded_but_installed_copies_stay() {
        let dir = tempfile::tempdir().unwrap();
        let store = ModelStore::open(dir.path()).unwrap();
        let d = Downloader::for_local_tests();
        let mut e = entry_served("old", "1", &[("m", blob(10))]);
        store.install(&e, &d, &mut |_, _| {}).unwrap();
        e.retired = true;
        // Already installed: still fine.
        assert!(store.install(&e, &d, &mut |_, _| {}).is_ok());
        let mut e2 = entry_served("gone", "1", &[("m", blob(10))]);
        e2.retired = true;
        assert!(matches!(
            store.install(&e2, &d, &mut |_, _| {}),
            Err(StoreError::Retired(_))
        ));
    }

    #[test]
    fn remove_deletes_only_the_requested_version() {
        let dir = tempfile::tempdir().unwrap();
        let store = ModelStore::open(dir.path()).unwrap();
        let d = Downloader::for_local_tests();
        store
            .install(
                &entry_served("k", "1", &[("m", blob(10))]),
                &d,
                &mut |_, _| {},
            )
            .unwrap();
        store
            .install(
                &entry_served("k", "2", &[("m", blob(10))]),
                &d,
                &mut |_, _| {},
            )
            .unwrap();
        store.remove("k", "1").unwrap();
        assert!(!store.is_installed("k", "1"));
        assert!(store.is_installed("k", "2"));
        store.remove("k", "2").unwrap();
        assert!(!dir.path().join("models/k").exists());
        assert!(matches!(
            store.remove("k", "2"),
            Err(StoreError::NotInstalled(..))
        ));
    }

    #[test]
    fn catalogue_sequence_persists_and_never_goes_back() {
        let dir = tempfile::tempdir().unwrap();
        let store = ModelStore::open(dir.path()).unwrap();
        assert_eq!(store.last_sequence(), 0);
        store.record_sequence(7).unwrap();
        store.record_sequence(3).unwrap();
        assert_eq!(ModelStore::open(dir.path()).unwrap().last_sequence(), 7);
    }

    #[test]
    fn rejects_path_traversal() {
        let dir = tempfile::tempdir().unwrap();
        let store = ModelStore::open(dir.path()).unwrap();
        assert!(matches!(
            store.file("a", "1", "../../etc/passwd"),
            Err(StoreError::Unsafe)
        ));
        assert!(!store.is_installed("..", "1"));
        assert!(matches!(
            store.remove("../x", "1"),
            Err(StoreError::NotInstalled(..))
        ));
    }
}
