//! What the app can speak with: installed models, their voices under Voxa's names, and
//! engines loaded on first use. Independent of Tauri so it can be tested directly.

use std::collections::HashMap;
use std::path::Path;
use std::sync::{Arc, Mutex};

use serde::{Deserialize, Serialize};
use voxa_core::{Audio, Engine, EngineError, SynthesisRequest};
use voxa_engines::{Family, StyleEngine, StylePackage};
use voxa_g2p::EnglishG2p;
use voxa_models::{
    import_pack, Catalogue, Downloader, InstalledModel, ModelEntry, ModelStore, Tier, TrustedKeys,
};

/// This app's version, compared with each model's `min_app`.
const APP_VERSION: &str = env!("CARGO_PKG_VERSION");
/// Largest catalogue the app will read (it lists metadata only, not model files).
const MAX_CATALOGUE_BYTES: u64 = 2_000_000;

/// Voxa's voice catalogue (names for the underlying model voices), shared with the website.
const VOICES_JSON: &str = include_str!("../../../voices.json");

/// Kokoro language codes in voices.json -> the speaker prefixes and model language tag.
const KOKORO_LANGS: &[(&str, &[&str], &str)] = &[("a", &["af_", "am_"], "en-US")];

/// The product name of a model family, from the plan (Voxa Classic = Kokoro, ...).
pub fn family_name(engine: &str) -> &'static str {
    match engine {
        "kokoro" => "Voxa Classic",
        "kitten" => "Voxa Instant",
        _ => "Voxa",
    }
}

/// Kitten TTS speakers -> (Voxa id, name, gender). Kept here rather than in voices.json,
/// which the website also reads. Ids are checked against voices.json in the tests.
const KITTEN_VOICES: &[(&str, &str, &str, &str)] = &[
    ("expr-voice-2-f", "luna", "Luna", "female"),
    ("expr-voice-3-f", "nell", "Nell", "female"),
    ("expr-voice-4-f", "tess", "Tess", "female"),
    ("expr-voice-5-f", "zoe", "Zoe", "female"),
    ("expr-voice-2-m", "ben", "Ben", "male"),
    ("expr-voice-3-m", "leo", "Leo", "male"),
    ("expr-voice-4-m", "sam", "Sam", "male"),
    ("expr-voice-5-m", "max", "Max", "male"),
];

#[derive(Debug, Clone, Deserialize)]
struct CatalogueVoice {
    id: String,
    name: String,
    lang: String,
    gender: String,
    accent: String,
    /// A Kokoro voice name, or a weighted blend (not supported on device yet).
    source: serde_json::Value,
    #[serde(default)]
    default: bool,
}

#[derive(Debug, Deserialize)]
struct VoicesFile {
    voices: Vec<CatalogueVoice>,
}

/// A voice the user can pick.
#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct Voice {
    pub id: String,
    /// "Voxa Classic", "Voxa Instant", ...
    pub family: String,
    pub name: String,
    pub gender: String,
    pub accent: String,
    pub language: String,
    pub model: String,
    pub model_version: String,
    pub tier: Tier,
    pub credit: String,
    /// The language's default voice in voices.json (Harper for US English).
    pub default: bool,
    /// The model's own voice id (e.g. "af_heart").
    #[serde(skip)]
    pub source: String,
}

/// An installed model, for the voice library.
#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct ModelInfo {
    pub id: String,
    pub family: String,
    pub version: String,
    pub engine: String,
    pub tier: Tier,
    pub languages: Vec<String>,
    pub cloning: bool,
    pub size_bytes: u64,
    pub license: String,
    pub credit: String,
    pub voices: usize,
}

/// A model offered by the catalogue, for the "Available" list.
#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct AvailableModel {
    pub id: String,
    pub family: String,
    pub version: String,
    pub engine: String,
    pub tier: Tier,
    pub languages: Vec<String>,
    pub cloning: bool,
    pub size_bytes: u64,
    pub license: String,
    pub credit: String,
    pub voices: usize,
    pub installed: bool,
}

pub struct Registry {
    store: ModelStore,
    keys: TrustedKeys,
    downloader: Downloader,
    catalogue_url: Option<String>,
    catalogue: Mutex<Option<Catalogue>>,
    names: Vec<CatalogueVoice>,
    threads: i32,
    engines: Mutex<HashMap<String, Arc<dyn Engine>>>,
}

impl Registry {
    pub fn open(root: &Path, keys: TrustedKeys) -> Result<Self, String> {
        let store = ModelStore::open(root).map_err(|e| e.to_string())?;
        let file: VoicesFile = serde_json::from_str(VOICES_JSON).map_err(|e| e.to_string())?;
        let threads = std::thread::available_parallelism()
            .map(|n| n.get().min(4) as i32)
            .unwrap_or(2);
        Ok(Self {
            store,
            keys,
            downloader: Downloader::new(),
            catalogue_url: option_env!("VOXA_CATALOGUE_URL").map(str::to_string),
            catalogue: Mutex::new(None),
            names: file.voices,
            threads,
            engines: Mutex::new(HashMap::new()),
        })
    }

    /// The newest installed version of each model.
    fn newest(&self) -> Result<Vec<InstalledModel>, String> {
        let mut best: HashMap<String, InstalledModel> = HashMap::new();
        for m in self.store.installed().map_err(|e| e.to_string())? {
            let newer = best.get(&m.entry.id).is_none_or(|cur| {
                voxa_models::catalogue::version_cmp(&m.entry.version, &cur.entry.version).is_gt()
            });
            if newer {
                best.insert(m.entry.id.clone(), m);
            }
        }
        let mut v: Vec<_> = best.into_values().collect();
        v.sort_by(|a, b| a.entry.id.cmp(&b.entry.id));
        Ok(v)
    }

    fn voices_of(&self, m: &InstalledModel) -> Vec<Voice> {
        let voice =
            |id: &str, name: &str, gender: &str, accent: &str, lang: &str, src: &str| Voice {
                id: id.into(),
                family: family_name(&m.entry.engine).into(),
                name: name.into(),
                gender: gender.into(),
                accent: accent.into(),
                language: lang.into(),
                model: m.entry.id.clone(),
                model_version: m.entry.version.clone(),
                tier: m.entry.tier,
                credit: m.entry.license.credit.clone(),
                default: false,
                source: src.into(),
            };
        if m.entry.engine == "kitten" {
            return KITTEN_VOICES
                .iter()
                .map(|(src, id, name, gender)| voice(id, name, gender, "American", "en-US", src))
                .collect();
        }
        if m.entry.engine != "kokoro" {
            return Vec::new();
        }
        KOKORO_LANGS
            .iter()
            .filter(|(_, _, tag)| m.entry.languages.iter().any(|l| l == tag))
            .flat_map(|(code, _, tag)| {
                self.names
                    .iter()
                    .filter(move |v| v.lang == *code)
                    .filter_map(move |v| {
                        Some(Voice {
                            id: v.id.clone(),
                            family: family_name(&m.entry.engine).into(),
                            name: v.name.clone(),
                            gender: v.gender.clone(),
                            accent: v.accent.clone(),
                            language: tag.to_string(),
                            model: m.entry.id.clone(),
                            model_version: m.entry.version.clone(),
                            tier: m.entry.tier,
                            credit: m.entry.license.credit.clone(),
                            default: v.default,
                            source: v.source.as_str()?.to_string(),
                        })
                    })
            })
            .collect()
    }

    /// Every voice of the newest installed models. Exactly one is the default: the
    /// catalogue's default (Harper) when installed, otherwise the first voice.
    pub fn voices(&self) -> Result<Vec<Voice>, String> {
        let mut v: Vec<Voice> = self
            .newest()?
            .iter()
            .flat_map(|m| self.voices_of(m))
            .collect();
        if !v.iter().any(|x| x.default) {
            if let Some(first) = v.first_mut() {
                first.default = true;
            }
        }
        Ok(v)
    }

    pub fn models(&self) -> Result<Vec<ModelInfo>, String> {
        Ok(self
            .store
            .installed()
            .map_err(|e| e.to_string())?
            .iter()
            .map(|m| ModelInfo {
                id: m.entry.id.clone(),
                family: family_name(&m.entry.engine).into(),
                version: m.entry.version.clone(),
                engine: m.entry.engine.clone(),
                tier: m.entry.tier,
                languages: m.entry.languages.clone(),
                cloning: m.entry.cloning,
                size_bytes: m.entry.files.iter().map(|f| f.size).sum(),
                license: m.entry.license.spdx.clone(),
                credit: m.entry.license.credit.clone(),
                voices: self.voices_of(m).len(),
            })
            .collect())
    }

    /// Points the registry at a catalogue and downloader (tests use a local server).
    #[cfg(test)]
    fn with_catalogue(mut self, url: &str, downloader: Downloader) -> Self {
        self.catalogue_url = Some(url.into());
        self.downloader = downloader;
        self
    }

    pub fn catalogue_configured(&self) -> bool {
        self.catalogue_url.is_some()
    }

    /// Downloads `catalogue.json` and its signature, verifies them (trusted key, schema,
    /// never older than the last accepted one) and remembers the result.
    pub fn refresh_catalogue(&self) -> Result<Vec<AvailableModel>, String> {
        let url = self
            .catalogue_url
            .as_deref()
            .ok_or("this build has no model catalogue configured")?;
        let json = self
            .downloader
            .get_text(url, MAX_CATALOGUE_BYTES)
            .map_err(|e| format!("catalogue: {e}"))?;
        let sig = self
            .downloader
            .get_text(&format!("{url}.sig"), 1024)
            .map_err(|e| format!("catalogue signature: {e}"))?;
        let sig = String::from_utf8_lossy(&sig);
        let cat = Catalogue::verify(&json, &sig, &self.keys, self.store.last_sequence())
            .map_err(|e| e.to_string())?;
        self.store
            .record_sequence(cat.sequence)
            .map_err(|e| e.to_string())?;
        *self.catalogue.lock().unwrap() = Some(cat);
        self.available()
    }

    /// Models the last accepted catalogue offers for this app version.
    pub fn available(&self) -> Result<Vec<AvailableModel>, String> {
        let guard = self.catalogue.lock().unwrap();
        let Some(cat) = guard.as_ref() else {
            return Ok(Vec::new());
        };
        Ok(cat
            .available(APP_VERSION)
            .into_iter()
            .map(|e| {
                let probe = InstalledModel {
                    entry: e.clone(),
                    dir: Default::default(),
                };
                AvailableModel {
                    id: e.id.clone(),
                    family: family_name(&e.engine).into(),
                    version: e.version.clone(),
                    engine: e.engine.clone(),
                    tier: e.tier,
                    languages: e.languages.clone(),
                    cloning: e.cloning,
                    size_bytes: e.files.iter().map(|f| f.size).sum(),
                    license: e.license.spdx.clone(),
                    credit: e.license.credit.clone(),
                    voices: self.voices_of(&probe).len(),
                    installed: self.store.is_installed(&e.id, &e.version),
                }
            })
            .collect())
    }

    /// Downloads and installs a catalogue model. `on_progress(done, total)` is in bytes.
    pub fn install(
        &self,
        id: &str,
        version: &str,
        on_progress: &mut dyn FnMut(u64, u64),
    ) -> Result<ModelInfo, String> {
        let entry: ModelEntry = self
            .catalogue
            .lock()
            .unwrap()
            .as_ref()
            .and_then(|c| c.find(id, version).cloned())
            .ok_or_else(|| format!("{id} {version} is not in the catalogue"))?;
        self.store
            .install(&entry, &self.downloader, on_progress)
            .map_err(|e| e.to_string())?;
        self.models()?
            .into_iter()
            .find(|m| m.id == id && m.version == version)
            .ok_or_else(|| "installed model not found".to_string())
    }

    pub fn import(&self, dir: &Path) -> Result<ModelInfo, String> {
        let m = import_pack(dir, &self.keys, &self.store).map_err(|e| e.to_string())?;
        self.models()?
            .into_iter()
            .find(|x| x.id == m.entry.id && x.version == m.entry.version)
            .ok_or_else(|| "imported model not found".to_string())
    }

    pub fn remove(&self, id: &str, version: &str) -> Result<(), String> {
        self.engines
            .lock()
            .unwrap()
            .remove(&format!("{id}@{version}"));
        self.store.remove(id, version).map_err(|e| e.to_string())
    }

    fn engine(&self, model: &InstalledModel) -> Result<Arc<dyn Engine>, EngineError> {
        let key = format!("{}@{}", model.entry.id, model.entry.version);
        if let Some(e) = self.engines.lock().unwrap().get(&key) {
            return Ok(Arc::clone(e));
        }
        let e: Arc<dyn Engine> = match model.entry.engine.as_str() {
            "kokoro" | "kitten" => {
                let (family, prefixes): (Family, &[&str]) = if model.entry.engine == "kitten" {
                    (Family::Kitten, &[""])
                } else {
                    (Family::Kokoro, KOKORO_LANGS[0].1)
                };
                let pkg = StylePackage::open(&model.dir, family)?;
                let g2p = Arc::new(EnglishG2p::from_dir(&model.dir.join("g2p"))?);
                Arc::new(StyleEngine::load(&key, &pkg, prefixes, g2p, self.threads)?)
            }
            other => {
                return Err(EngineError::Failed(format!(
                    "this version of Voxa cannot run {other} models yet"
                )))
            }
        };
        self.engines.lock().unwrap().insert(key, Arc::clone(&e));
        Ok(e)
    }

    /// Speaks `text` with a Voxa voice.
    pub fn synthesize(&self, voice_id: &str, text: &str, speed: f32) -> Result<Audio, String> {
        let newest = self.newest()?;
        let (model, voice) = newest
            .iter()
            .find_map(|m| {
                self.voices_of(m)
                    .into_iter()
                    .find(|v| v.id == voice_id)
                    .map(|v| (m, v))
            })
            .ok_or_else(|| format!("unknown voice {voice_id:?}"))?;
        let engine = self.engine(model).map_err(|e| e.to_string())?;
        let req = SynthesisRequest {
            text: text.to_string(),
            voice: voice.source,
            speed,
        };
        engine
            .synthesize(&req, &mut |_, _| {})
            .map_err(|e| e.to_string())
    }
}

/// Keys this build trusts for catalogues and voice packs: the development key in debug
/// builds, and the production key when one was provided at build time.
pub fn trusted_keys() -> TrustedKeys {
    let mut keys: Vec<[u8; 32]> = Vec::new();
    let mut add = |hex_key: &str| {
        if let Ok(Ok(k)) = hex::decode(hex_key.trim()).map(<[u8; 32]>::try_from) {
            keys.push(k);
        }
    };
    if cfg!(debug_assertions) {
        add(include_str!("../../keys/dev-catalogue.pub"));
    }
    if let Some(k) = option_env!("VOXA_CATALOGUE_PUBKEY") {
        add(k);
    }
    TrustedKeys::from_bytes(&keys).expect("valid built-in keys")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn registry() -> (tempfile::TempDir, Registry) {
        let d = tempfile::tempdir().unwrap();
        let r = Registry::open(d.path(), trusted_keys()).unwrap();
        (d, r)
    }

    #[test]
    fn voices_json_parses_and_maps_us_kokoro_voices() {
        let file: VoicesFile = serde_json::from_str(VOICES_JSON).unwrap();
        let us_single: Vec<_> = file
            .voices
            .iter()
            .filter(|v| v.lang == "a" && v.source.is_string())
            .collect();
        assert!(us_single.len() >= 20);
        assert!(us_single
            .iter()
            .any(|v| v.id == "harper" && v.source == "af_heart"));
    }

    #[test]
    fn kitten_names_do_not_clash_with_the_catalogue() {
        let file: VoicesFile = serde_json::from_str(VOICES_JSON).unwrap();
        let raw: serde_json::Value = serde_json::from_str(VOICES_JSON).unwrap();
        let lite: Vec<&str> = raw["lite"]["voices"]
            .as_array()
            .map(|a| a.iter().filter_map(|v| v["id"].as_str()).collect())
            .unwrap_or_default();
        for (_, id, _, _) in KITTEN_VOICES {
            assert!(
                !file.voices.iter().any(|v| v.id == *id),
                "{id} is in voices.json"
            );
            assert!(!lite.contains(id), "{id} is a Lite voice");
        }
    }

    /// `VOXA_KITTEN_PACK=<signed dev pack> cargo test -p voxa-app -- --ignored`
    #[test]
    #[ignore = "needs a signed Kitten voice pack (VOXA_KITTEN_PACK)"]
    fn imports_kitten_and_speaks() {
        let (_d, r) = registry();
        let pack = std::env::var("VOXA_KITTEN_PACK").expect("set VOXA_KITTEN_PACK");
        let m = r.import(Path::new(&pack)).unwrap();
        assert_eq!((m.engine.as_str(), m.voices), ("kitten", 8));
        let voices = r.voices().unwrap();
        assert_eq!(voices.iter().filter(|v| v.default).count(), 1);
        let a = r.synthesize("luna", "The quick brown fox.", 1.0).unwrap();
        assert!(a.duration() > 0.5);
    }

    /// Serves fixed paths over plain http on 127.0.0.1 (test-only downloader allows it).
    fn serve(files: Vec<(String, Vec<u8>)>) -> String {
        use std::io::{BufRead, BufReader, Write};
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let base = format!("http://{}", listener.local_addr().unwrap());
        std::thread::spawn(move || {
            for stream in listener.incoming() {
                let Ok(mut s) = stream else { continue };
                let mut reader = BufReader::new(s.try_clone().unwrap());
                let mut first = String::new();
                reader.read_line(&mut first).unwrap_or(0);
                loop {
                    let mut l = String::new();
                    if reader.read_line(&mut l).unwrap_or(0) == 0 || l == "\r\n" {
                        break;
                    }
                }
                let path = first.split_whitespace().nth(1).unwrap_or("").to_string();
                let body = files
                    .iter()
                    .find(|(p, _)| *p == path)
                    .map(|(_, b)| b.clone());
                let (status, body) = match body {
                    Some(b) => ("200 OK", b),
                    None => ("404 Not Found", Vec::new()),
                };
                let _ = write!(
                    s,
                    "HTTP/1.1 {status}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                    body.len()
                );
                let _ = s.write_all(&body);
            }
        });
        base
    }

    fn dev_sign(msg: &[u8]) -> String {
        use base64::Engine as _;
        use ed25519_dalek::Signer;
        let secret = include_str!("../../keys/dev-catalogue.key");
        let seed: [u8; 32] = hex::decode(secret.trim()).unwrap().try_into().unwrap();
        let sig = ed25519_dalek::SigningKey::from_bytes(&seed).sign(msg);
        base64::engine::general_purpose::STANDARD.encode(sig.to_bytes())
    }

    fn sha(b: &[u8]) -> String {
        use sha2::Digest;
        hex::encode(sha2::Sha256::digest(b))
    }

    #[test]
    fn catalogue_lists_downloads_and_installs_with_progress() {
        let model = b"not really a model".to_vec();
        let speakers = br#"["expr-voice-2-f"]"#.to_vec();
        // One server for the model files; the catalogue (served by a second one) points
        // at it.
        let files = serve(vec![
            ("/m".into(), model.clone()),
            ("/s".into(), speakers.clone()),
        ]);
        let entry = |id: &str, min_app: &str, files_json: serde_json::Value| {
            serde_json::json!({
                "id": id, "version": "0.8.0", "engine": "kitten",
                "tier": "phone", "languages": ["en-US"], "cloning": false,
                "license": {"spdx": "Apache-2.0", "credit": "Kitten TTS by KittenML",
                            "url": "https://huggingface.co/KittenML"},
                "min_app": min_app,
                "requirements": {"ram_mb": 200, "measured": false},
                "files": files_json
            })
        };
        let kitten_files = serde_json::json!([
            {"path": "model.int8.onnx", "url": format!("{files}/m"),
             "sha256": sha(&model), "size": model.len()},
            {"path": "speakers.json", "url": format!("{files}/s"),
             "sha256": sha(&speakers), "size": speakers.len()}
        ]);
        let cat = serde_json::to_vec(&serde_json::json!({
            "schema": 1, "sequence": 3, "issued": "2026-11-02T00:00:00Z",
            "models": [
                entry("kitten-nano-en", "0.1.0", kitten_files.clone()),
                // Needs a newer app: must not be offered.
                entry("future-model", "99.0.0", kitten_files),
            ]
        }))
        .unwrap();
        let server = serve(vec![
            ("/catalogue.json".into(), cat.clone()),
            ("/catalogue.json.sig".into(), dev_sign(&cat).into_bytes()),
        ]);

        let d = tempfile::tempdir().unwrap();
        let r = Registry::open(d.path(), trusted_keys())
            .unwrap()
            .with_catalogue(
                &format!("{server}/catalogue.json"),
                Downloader::for_local_tests(),
            );
        let avail = r.refresh_catalogue().unwrap();
        assert_eq!(avail.len(), 1, "the model needing app 99 is hidden");
        assert_eq!(avail[0].id, "kitten-nano-en");
        assert_eq!(avail[0].voices, 8);
        assert!(!avail[0].installed);

        let mut last = (0, 0);
        let m = r
            .install("kitten-nano-en", "0.8.0", &mut |a, b| last = (a, b))
            .unwrap();
        assert_eq!(m.voices, 8);
        let total = (model.len() + speakers.len()) as u64;
        assert_eq!(last, (total, total));
        assert!(r.available().unwrap()[0].installed);
        assert!(r.install("nope", "1", &mut |_, _| {}).is_err());
    }

    #[test]
    fn catalogue_rejects_bad_signatures_and_rollback() {
        let cat = |seq: u64| {
            serde_json::to_vec(&serde_json::json!({
                "schema": 1, "sequence": seq, "issued": "", "models": []
            }))
            .unwrap()
        };
        let (new, old) = (cat(5), cat(4));
        let base = serve(vec![
            ("/new.json".into(), new.clone()),
            ("/new.json.sig".into(), dev_sign(&new).into_bytes()),
            ("/old.json".into(), old.clone()),
            ("/old.json.sig".into(), dev_sign(&old).into_bytes()),
            ("/forged.json".into(), new.clone()),
            (
                "/forged.json.sig".into(),
                dev_sign(b"something else").into_bytes(),
            ),
        ]);
        let d = tempfile::tempdir().unwrap();
        let open = |name: &str| {
            Registry::open(d.path(), trusted_keys())
                .unwrap()
                .with_catalogue(&format!("{base}/{name}"), Downloader::for_local_tests())
        };
        assert!(open("forged.json").refresh_catalogue().is_err());
        assert!(open("new.json").refresh_catalogue().is_ok());
        let err = open("old.json").refresh_catalogue().unwrap_err();
        assert!(err.contains("older"), "{err}");
        assert!(!Registry::open(d.path(), trusted_keys())
            .unwrap()
            .catalogue_configured());
    }

    #[test]
    fn empty_store_has_no_voices() {
        let (_d, r) = registry();
        assert!(r.voices().unwrap().is_empty());
        assert!(r.models().unwrap().is_empty());
        assert!(r
            .synthesize("harper", "hi", 1.0)
            .unwrap_err()
            .contains("unknown voice"));
    }

    #[test]
    fn importing_a_non_pack_fails_cleanly() {
        let (_d, r) = registry();
        let empty = tempfile::tempdir().unwrap();
        assert!(r
            .import(empty.path())
            .unwrap_err()
            .contains("not a Voxa voice pack"));
    }

    #[test]
    fn debug_builds_trust_the_dev_key() {
        // A pack signed with the committed dev key must verify in tests (debug builds).
        let keys = trusted_keys();
        let msg = b"hello";
        let secret = include_str!("../../keys/dev-catalogue.key");
        let seed: [u8; 32] = hex::decode(secret.trim()).unwrap().try_into().unwrap();
        use base64::Engine as _;
        use ed25519_dalek::Signer;
        let sig = ed25519_dalek::SigningKey::from_bytes(&seed).sign(msg);
        let b64 = base64::engine::general_purpose::STANDARD.encode(sig.to_bytes());
        assert!(keys.verify_bytes(msg, &b64).is_ok());
    }

    /// `VOXA_KOKORO_PACK=<signed dev pack> cargo test -p voxa-app -- --ignored`
    #[test]
    #[ignore = "needs a signed Kokoro voice pack (VOXA_KOKORO_PACK)"]
    fn imports_a_real_pack_and_speaks() {
        let (_d, r) = registry();
        let pack = std::env::var("VOXA_KOKORO_PACK").expect("set VOXA_KOKORO_PACK");
        let m = r.import(Path::new(&pack)).unwrap();
        assert_eq!(m.engine, "kokoro");
        assert!(m.voices >= 20, "{} voices", m.voices);
        let voices = r.voices().unwrap();
        let harper = voices.iter().find(|v| v.id == "harper").unwrap();
        assert_eq!(harper.name, "Harper");
        assert!(harper.default);
        assert_eq!(voices.iter().filter(|v| v.default).count(), 1);
        let a = r
            .synthesize("harper", "Hello from Voxa. It costs $5.", 1.0)
            .unwrap();
        assert!(a.duration() > 1.0);
        // Second call reuses the loaded engine.
        assert!(r.synthesize("miles", "Second voice.", 1.0).is_ok());
        r.remove(&m.id, &m.version).unwrap();
        assert!(r.voices().unwrap().is_empty());
    }
}
