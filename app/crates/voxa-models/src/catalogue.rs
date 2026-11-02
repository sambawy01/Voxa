//! The signed model catalogue.
//!
//! Voxa publishes `catalogue.json` and `catalogue.json.sig` (base64 Ed25519 signature over
//! the exact bytes of the JSON). The app accepts a catalogue only if:
//! 1. the signature verifies against one of the public keys built into the app (several
//!    keys allow rotation),
//! 2. its `schema` is one this app understands,
//! 3. its `sequence` is not lower than the last catalogue the app accepted (rollback
//!    protection: an attacker cannot replay an old catalogue with a since-fixed model).

use base64::Engine as _;
use ed25519_dalek::{Signature, VerifyingKey};
use serde::{Deserialize, Serialize};
use thiserror::Error;

/// Catalogue schema versions this build understands.
pub const SCHEMA: u32 = 1;

#[derive(Debug, Error)]
pub enum CatalogueError {
    #[error("signature is not valid base64 or has the wrong length")]
    BadSignatureEncoding,
    #[error("signature does not match any trusted key")]
    UntrustedSignature,
    #[error("catalogue is not valid JSON: {0}")]
    Json(#[from] serde_json::Error),
    #[error("catalogue schema {0} is not supported by this app (supports {SCHEMA})")]
    UnsupportedSchema(u32),
    #[error("catalogue sequence {got} is older than the one already accepted ({have})")]
    Rollback { got: u64, have: u64 },
    #[error("catalogue is inconsistent: {0}")]
    Invalid(String),
}

/// Ed25519 public keys the app trusts for catalogues.
pub struct TrustedKeys(Vec<VerifyingKey>);

impl TrustedKeys {
    /// Keys as 32-byte arrays (built into the app at compile time).
    pub fn from_bytes(keys: &[[u8; 32]]) -> Result<Self, CatalogueError> {
        keys.iter()
            .map(|k| {
                VerifyingKey::from_bytes(k)
                    .map_err(|_| CatalogueError::Invalid("bad public key".into()))
            })
            .collect::<Result<Vec<_>, _>>()
            .map(TrustedKeys)
    }

    /// Verifies a base64 Ed25519 signature over `msg` against any trusted key.
    pub fn verify_bytes(&self, msg: &[u8], sig_b64: &str) -> Result<(), CatalogueError> {
        let raw = base64::engine::general_purpose::STANDARD
            .decode(sig_b64.trim())
            .map_err(|_| CatalogueError::BadSignatureEncoding)?;
        let sig = Signature::from_slice(&raw).map_err(|_| CatalogueError::BadSignatureEncoding)?;
        if self.0.iter().any(|k| k.verify_strict(msg, &sig).is_ok()) {
            Ok(())
        } else {
            Err(CatalogueError::UntrustedSignature)
        }
    }
}

/// The device label from the plan: 📱 phone, 💻 computer, 🖥️ GPU.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Tier {
    Phone,
    Computer,
    Gpu,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct License {
    /// SPDX id or a LicenseRef (e.g. "Apache-2.0", "LicenseRef-OpenRAIL-M").
    pub spdx: String,
    /// Credit line shown on the voice card and the Credits screen.
    pub credit: String,
    pub url: String,
    /// Use restrictions Voxa's terms must pass on to users (OpenRAIL-M, CC-BY clauses).
    #[serde(default)]
    pub restrictions: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ModelFile {
    /// Path inside the model's install directory. Plain relative path, no "..".
    pub path: String,
    /// Download URL on Voxa's mirror.
    pub url: String,
    pub sha256: String,
    pub size: u64,
}

/// Measured requirements; `measured: false` means targets not yet confirmed on devices.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Requirements {
    pub ram_mb: u32,
    #[serde(default)]
    pub vram_mb: Option<u32>,
    pub measured: bool,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ModelEntry {
    pub id: String,
    pub version: String,
    /// Which engine runs it, e.g. "kokoro", "kitten", "supertonic".
    pub engine: String,
    pub tier: Tier,
    pub languages: Vec<String>,
    pub cloning: bool,
    pub license: License,
    /// Oldest app version that can run this model.
    pub min_app: String,
    pub requirements: Requirements,
    pub files: Vec<ModelFile>,
    /// Upstream pulled or relicensed it: installed copies keep working, but it is no
    /// longer offered for download.
    #[serde(default)]
    pub retired: bool,
}

impl ModelEntry {
    /// Safe ids and paths, well-formed hashes, at least one file.
    pub fn check(&self) -> Result<(), CatalogueError> {
        let bad = |why: String| Err(CatalogueError::Invalid(format!("{}: {why}", self.id)));
        if !safe_id(&self.id) || !safe_id(&self.version) {
            return bad("id and version must be [a-z0-9._-]".into());
        }
        if self.files.is_empty() {
            return bad("no files".into());
        }
        let mut paths = std::collections::HashSet::new();
        for f in &self.files {
            if !safe_rel_path(&f.path) {
                return bad(format!("unsafe file path {:?}", f.path));
            }
            if !paths.insert(f.path.as_str()) {
                return bad(format!("file {} listed twice", f.path));
            }
            if f.sha256.len() != 64 || hex::decode(&f.sha256).is_err() {
                return bad(format!("bad sha256 for {}", f.path));
            }
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Catalogue {
    pub schema: u32,
    pub sequence: u64,
    pub issued: String,
    pub models: Vec<ModelEntry>,
}

impl Catalogue {
    /// Verifies and parses a downloaded catalogue. `last_sequence` is the sequence of the
    /// catalogue the app accepted before (0 on first run).
    pub fn verify(
        json: &[u8],
        sig_b64: &str,
        keys: &TrustedKeys,
        last_sequence: u64,
    ) -> Result<Self, CatalogueError> {
        keys.verify_bytes(json, sig_b64)?;
        let cat: Catalogue = serde_json::from_slice(json)?;
        if cat.schema != SCHEMA {
            return Err(CatalogueError::UnsupportedSchema(cat.schema));
        }
        if cat.sequence < last_sequence {
            return Err(CatalogueError::Rollback {
                got: cat.sequence,
                have: last_sequence,
            });
        }
        cat.check()?;
        Ok(cat)
    }

    /// Structural checks a signature cannot catch (mistakes when the catalogue was built).
    fn check(&self) -> Result<(), CatalogueError> {
        let mut seen = std::collections::HashSet::new();
        for m in &self.models {
            m.check()?;
            if !seen.insert((m.id.as_str(), m.version.as_str())) {
                return Err(CatalogueError::Invalid(format!(
                    "{}: version {} listed twice",
                    m.id, m.version
                )));
            }
        }
        Ok(())
    }

    /// Models this app version can run and that are still offered, newest version per id.
    pub fn available(&self, app_version: &str) -> Vec<&ModelEntry> {
        let mut best: std::collections::BTreeMap<&str, &ModelEntry> = Default::default();
        for m in self.models.iter().filter(|m| !m.retired) {
            if version_cmp(&m.min_app, app_version).is_gt() {
                continue;
            }
            match best.get(m.id.as_str()) {
                Some(cur) if version_cmp(&cur.version, &m.version).is_ge() => {}
                _ => {
                    best.insert(&m.id, m);
                }
            }
        }
        best.into_values().collect()
    }

    pub fn find(&self, id: &str, version: &str) -> Option<&ModelEntry> {
        self.models
            .iter()
            .find(|m| m.id == id && m.version == version)
    }
}

pub(crate) fn safe_id(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= 64
        && s != "."
        && s != ".."
        && s.bytes()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b"._-".contains(&b))
}

/// A relative path with no "..", no absolute root, no backslashes and no empty segments.
pub(crate) fn safe_rel_path(p: &str) -> bool {
    !p.is_empty()
        && !p.starts_with('/')
        && !p.contains('\\')
        && !p.contains(':')
        && p.split('/')
            .all(|seg| !seg.is_empty() && seg != "." && seg != "..")
}

/// Compares dotted numeric versions ("1.10.0" > "1.9"). Non-numeric parts compare as 0.
pub fn version_cmp(a: &str, b: &str) -> std::cmp::Ordering {
    let parse = |s: &str| -> Vec<u64> {
        s.split('.')
            .map(|p| p.parse::<u64>().unwrap_or(0))
            .collect()
    };
    let (mut x, mut y) = (parse(a), parse(b));
    let n = x.len().max(y.len());
    x.resize(n, 0);
    y.resize(n, 0);
    x.cmp(&y)
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    use ed25519_dalek::{Signer, SigningKey};

    pub fn signing_key(seed: u8) -> SigningKey {
        SigningKey::from_bytes(&[seed; 32])
    }

    pub fn sign(key: &SigningKey, msg: &[u8]) -> String {
        base64::engine::general_purpose::STANDARD.encode(key.sign(msg).to_bytes())
    }

    pub fn model(id: &str, version: &str, min_app: &str) -> ModelEntry {
        ModelEntry {
            id: id.into(),
            version: version.into(),
            engine: "kitten".into(),
            tier: Tier::Phone,
            languages: vec!["en".into()],
            cloning: false,
            license: License {
                spdx: "Apache-2.0".into(),
                credit: "Kitten TTS by KittenML".into(),
                url: "https://huggingface.co/KittenML".into(),
                restrictions: None,
            },
            min_app: min_app.into(),
            requirements: Requirements {
                ram_mb: 512,
                vram_mb: None,
                measured: false,
            },
            files: vec![ModelFile {
                path: "model.onnx".into(),
                url: "https://models.voxa.app/kitten/model.onnx".into(),
                sha256: "0".repeat(64),
                size: 10,
            }],
            retired: false,
        }
    }

    pub fn catalogue(sequence: u64, models: Vec<ModelEntry>) -> Vec<u8> {
        serde_json::to_vec(&Catalogue {
            schema: SCHEMA,
            sequence,
            issued: "2026-11-02T00:00:00Z".into(),
            models,
        })
        .unwrap()
    }

    fn keys(seeds: &[u8]) -> TrustedKeys {
        let pubs: Vec<[u8; 32]> = seeds
            .iter()
            .map(|s| signing_key(*s).verifying_key().to_bytes())
            .collect();
        TrustedKeys::from_bytes(&pubs).unwrap()
    }

    #[test]
    fn accepts_a_correctly_signed_catalogue() {
        let json = catalogue(5, vec![model("kitten-nano", "0.8.0", "0.1.0")]);
        let cat = Catalogue::verify(&json, &sign(&signing_key(1), &json), &keys(&[1]), 4).unwrap();
        assert_eq!(cat.sequence, 5);
        assert_eq!(cat.models.len(), 1);
    }

    #[test]
    fn accepts_any_trusted_key_for_rotation() {
        let json = catalogue(1, vec![model("a", "1", "0.1.0")]);
        let sig = sign(&signing_key(2), &json);
        assert!(Catalogue::verify(&json, &sig, &keys(&[1, 2]), 0).is_ok());
    }

    #[test]
    fn rejects_wrong_key_and_tampering() {
        let json = catalogue(1, vec![model("a", "1", "0.1.0")]);
        let sig = sign(&signing_key(9), &json);
        assert!(matches!(
            Catalogue::verify(&json, &sig, &keys(&[1]), 0),
            Err(CatalogueError::UntrustedSignature)
        ));
        let good = sign(&signing_key(1), &json);
        let mut tampered = json.clone();
        let i = tampered.iter().position(|&b| b == b'1').unwrap();
        tampered[i] = b'2';
        assert!(matches!(
            Catalogue::verify(&tampered, &good, &keys(&[1]), 0),
            Err(CatalogueError::UntrustedSignature)
        ));
        assert!(matches!(
            Catalogue::verify(&json, "not base64!", &keys(&[1]), 0),
            Err(CatalogueError::BadSignatureEncoding)
        ));
    }

    #[test]
    fn rejects_rollback_and_unknown_schema() {
        let json = catalogue(3, vec![model("a", "1", "0.1.0")]);
        let sig = sign(&signing_key(1), &json);
        assert!(matches!(
            Catalogue::verify(&json, &sig, &keys(&[1]), 4),
            Err(CatalogueError::Rollback { got: 3, have: 4 })
        ));
        assert!(Catalogue::verify(&json, &sig, &keys(&[1]), 3).is_ok());

        let mut v: serde_json::Value = serde_json::from_slice(&json).unwrap();
        v["schema"] = 2.into();
        let json2 = serde_json::to_vec(&v).unwrap();
        let sig2 = sign(&signing_key(1), &json2);
        assert!(matches!(
            Catalogue::verify(&json2, &sig2, &keys(&[1]), 0),
            Err(CatalogueError::UnsupportedSchema(2))
        ));
    }

    #[test]
    fn rejects_unsafe_paths_ids_and_hashes() {
        for (mutate, what) in [
            (
                Box::new(|m: &mut ModelEntry| m.files[0].path = "../evil".into())
                    as Box<dyn Fn(&mut ModelEntry)>,
                "dotdot",
            ),
            (
                Box::new(|m: &mut ModelEntry| m.files[0].path = "/etc/passwd".into()),
                "absolute",
            ),
            (
                Box::new(|m: &mut ModelEntry| m.files[0].path = "a\\..\\b".into()),
                "backslash",
            ),
            (Box::new(|m: &mut ModelEntry| m.id = "../x".into()), "id"),
            (
                Box::new(|m: &mut ModelEntry| m.files[0].sha256 = "xyz".into()),
                "hash",
            ),
            (Box::new(|m: &mut ModelEntry| m.files.clear()), "no files"),
        ] {
            let mut m = model("a", "1", "0.1.0");
            mutate(&mut m);
            let json = catalogue(1, vec![m]);
            let sig = sign(&signing_key(1), &json);
            assert!(
                matches!(
                    Catalogue::verify(&json, &sig, &keys(&[1]), 0),
                    Err(CatalogueError::Invalid(_))
                ),
                "{what} should be rejected"
            );
        }
    }

    #[test]
    fn rejects_duplicate_versions() {
        let json = catalogue(1, vec![model("a", "1", "0.1.0"), model("a", "1", "0.1.0")]);
        let sig = sign(&signing_key(1), &json);
        assert!(Catalogue::verify(&json, &sig, &keys(&[1]), 0).is_err());
    }

    #[test]
    fn available_picks_newest_runnable_unretired() {
        let mut retired = model("b", "2.0.0", "0.1.0");
        retired.retired = true;
        let cat = Catalogue {
            schema: SCHEMA,
            sequence: 1,
            issued: String::new(),
            models: vec![
                model("a", "1.9.0", "0.1.0"),
                model("a", "1.10.0", "0.1.0"),
                model("a", "2.0.0", "0.5.0"), // needs a newer app
                model("b", "1.0.0", "0.1.0"),
                retired,
            ],
        };
        let got: Vec<(&str, &str)> = cat
            .available("0.1.0")
            .iter()
            .map(|m| (m.id.as_str(), m.version.as_str()))
            .collect();
        assert_eq!(got, vec![("a", "1.10.0"), ("b", "1.0.0")]);
    }

    #[test]
    fn version_ordering() {
        use std::cmp::Ordering::*;
        assert_eq!(version_cmp("1.10.0", "1.9"), Greater);
        assert_eq!(version_cmp("1.0", "1.0.0"), Equal);
        assert_eq!(version_cmp("0.1.0", "0.2"), Less);
    }
}
