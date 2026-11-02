//! Signed voice packs: a model shipped as a folder instead of downloaded from the mirror.
//!
//! ```text
//! <pack>/voxa-pack.json       the catalogue entry for the model (files with sha256 + size)
//! <pack>/voxa-pack.json.sig   base64 Ed25519 signature over voxa-pack.json
//! <pack>/<files...>           the model files, at the paths the entry lists
//! ```
//!
//! Packs let people move voices between their own devices offline (the plan's "export and
//! import model packs") and let Voxa hand out voices before the mirror exists. A pack is
//! trusted exactly like a catalogue: the signature must verify against a key built into
//! the app, and every file must match its sha256 before anything is installed.

use std::fs;
use std::path::Path;

use thiserror::Error;

use crate::catalogue::{CatalogueError, ModelEntry, TrustedKeys};
use crate::store::{InstalledModel, ModelStore, StoreError};

pub const MANIFEST: &str = "voxa-pack.json";
pub const SIGNATURE: &str = "voxa-pack.json.sig";

#[derive(Debug, Error)]
pub enum PackError {
    #[error("this folder is not a Voxa voice pack (no {MANIFEST})")]
    NotAPack,
    #[error("the voice pack is not signed by Voxa: {0}")]
    Signature(CatalogueError),
    #[error("the voice pack's manifest is invalid: {0}")]
    Manifest(String),
    #[error(transparent)]
    Store(#[from] StoreError),
    #[error("disk error: {0}")]
    Io(#[from] std::io::Error),
}

/// Reads and verifies a pack's manifest without installing anything.
pub fn read_pack(dir: &Path, keys: &TrustedKeys) -> Result<ModelEntry, PackError> {
    let manifest = dir.join(MANIFEST);
    if !manifest.is_file() {
        return Err(PackError::NotAPack);
    }
    let json = fs::read(&manifest)?;
    let sig = fs::read_to_string(dir.join(SIGNATURE))
        .map_err(|_| PackError::Signature(CatalogueError::BadSignatureEncoding))?;
    keys.verify_bytes(&json, &sig)
        .map_err(PackError::Signature)?;
    let entry: ModelEntry =
        serde_json::from_slice(&json).map_err(|e| PackError::Manifest(e.to_string()))?;
    entry
        .check()
        .map_err(|e| PackError::Manifest(e.to_string()))?;
    Ok(entry)
}

/// Verifies a pack and installs its model into the store.
pub fn import_pack(
    dir: &Path,
    keys: &TrustedKeys,
    store: &ModelStore,
) -> Result<InstalledModel, PackError> {
    let entry = read_pack(dir, keys)?;
    Ok(store.install_from_dir(&entry, dir)?)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::catalogue::tests::{model, sign, signing_key};
    use crate::download::tests::blob;
    use crate::ModelFile;
    use sha2::{Digest, Sha256};

    fn keys() -> TrustedKeys {
        TrustedKeys::from_bytes(&[signing_key(1).verifying_key().to_bytes()]).unwrap()
    }

    /// Writes a pack with two files, signed by `signer`.
    fn make_pack(dir: &Path, signer: u8) -> ModelEntry {
        let files = [("model.onnx", blob(3000)), ("g2p/us_gold.json", blob(500))];
        let mut entry = model("kokoro-en", "1.0.0", "0.1.0");
        entry.files = files
            .iter()
            .map(|(p, b)| {
                let path = dir.join(p);
                fs::create_dir_all(path.parent().unwrap()).unwrap();
                fs::write(&path, b).unwrap();
                ModelFile {
                    path: p.to_string(),
                    url: String::new(),
                    sha256: hex::encode(Sha256::digest(b)),
                    size: b.len() as u64,
                }
            })
            .collect();
        let json = serde_json::to_vec(&entry).unwrap();
        fs::write(dir.join(MANIFEST), &json).unwrap();
        fs::write(dir.join(SIGNATURE), sign(&signing_key(signer), &json)).unwrap();
        entry
    }

    #[test]
    fn imports_a_signed_pack() {
        let (pack, root) = (tempfile::tempdir().unwrap(), tempfile::tempdir().unwrap());
        let entry = make_pack(pack.path(), 1);
        let store = ModelStore::open(root.path()).unwrap();
        let m = import_pack(pack.path(), &keys(), &store).unwrap();
        assert_eq!(m.entry, entry);
        assert!(store.is_installed("kokoro-en", "1.0.0"));
        let f = store
            .file("kokoro-en", "1.0.0", "g2p/us_gold.json")
            .unwrap();
        assert_eq!(fs::read(f).unwrap(), blob(500));
        // The pack itself is untouched.
        assert!(pack.path().join("model.onnx").is_file());
    }

    #[test]
    fn rejects_unsigned_and_foreign_packs() {
        let (pack, root) = (tempfile::tempdir().unwrap(), tempfile::tempdir().unwrap());
        let store = ModelStore::open(root.path()).unwrap();
        assert!(matches!(
            import_pack(pack.path(), &keys(), &store),
            Err(PackError::NotAPack)
        ));
        make_pack(pack.path(), 9); // signed by someone else
        assert!(matches!(
            import_pack(pack.path(), &keys(), &store),
            Err(PackError::Signature(_))
        ));
        fs::remove_file(pack.path().join(SIGNATURE)).unwrap();
        assert!(matches!(
            import_pack(pack.path(), &keys(), &store),
            Err(PackError::Signature(_))
        ));
        assert!(store.installed().unwrap().is_empty());
    }

    #[test]
    fn rejects_a_tampered_file_and_installs_nothing() {
        let (pack, root) = (tempfile::tempdir().unwrap(), tempfile::tempdir().unwrap());
        make_pack(pack.path(), 1);
        let mut bytes = fs::read(pack.path().join("model.onnx")).unwrap();
        bytes[10] ^= 0xFF;
        fs::write(pack.path().join("model.onnx"), bytes).unwrap();
        let store = ModelStore::open(root.path()).unwrap();
        let err = import_pack(pack.path(), &keys(), &store).unwrap_err();
        assert!(err.to_string().contains("checksum"), "{err}");
        assert!(store.installed().unwrap().is_empty());
        assert!(!root.path().join("staging/kokoro-en/1.0.0").exists());
    }

    #[test]
    fn rejects_a_missing_file() {
        let (pack, root) = (tempfile::tempdir().unwrap(), tempfile::tempdir().unwrap());
        make_pack(pack.path(), 1);
        fs::remove_file(pack.path().join("g2p/us_gold.json")).unwrap();
        let store = ModelStore::open(root.path()).unwrap();
        assert!(import_pack(pack.path(), &keys(), &store).is_err());
        assert!(store.installed().unwrap().is_empty());
    }
}
