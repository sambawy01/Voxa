//! Owner tool for publishing model catalogues.
//!
//! ```text
//! voxa-catalogue keygen <secret-key-file>          new Ed25519 key; prints the public key
//! voxa-catalogue sign <catalogue.json> <secret>    writes catalogue.json.sig
//! voxa-catalogue verify <catalogue.json> <public-key-hex>
//! voxa-catalogue hash <file>                       sha256 and size for a catalogue entry
//! voxa-catalogue pack <dir> <entry.json> <secret>  turn a folder into a signed voice pack
//! ```
//!
//! For `pack`, `<entry.json>` is a catalogue entry without `files` (id, version, engine,
//! tier, languages, cloning, license, min_app, requirements); the tool lists and hashes
//! every file in `<dir>`, then writes and signs `<dir>/voxa-pack.json`.
//!
//! Keep the secret key offline (a password manager or hardware key), never in the repo.
//! The public key goes into the app build so it can verify catalogues.

use std::fs;
use std::process::ExitCode;

use base64::Engine as _;
use ed25519_dalek::{Signer, SigningKey};
use voxa_models::{Catalogue, TrustedKeys};

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let result = match args.iter().map(String::as_str).collect::<Vec<_>>()[..] {
        ["keygen", out] => keygen(out),
        ["sign", cat, secret] => sign(cat, secret),
        ["verify", cat, public] => verify(cat, public),
        ["hash", file] => hash(file),
        ["pack", dir, entry, secret] => pack(dir, entry, secret),
        _ => Err(USAGE.to_string()),
    };
    match result {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("{e}");
            ExitCode::FAILURE
        }
    }
}

const USAGE: &str = "usage:
  voxa-catalogue keygen <secret-key-file>
  voxa-catalogue sign <catalogue.json> <secret-key-file>
  voxa-catalogue verify <catalogue.json> <public-key-hex>
  voxa-catalogue hash <file>
  voxa-catalogue pack <dir> <entry.json> <secret-key-file>";

fn keygen(out: &str) -> Result<(), String> {
    if fs::metadata(out).is_ok() {
        return Err(format!("{out} exists; refusing to overwrite a key"));
    }
    let mut seed = [0u8; 32];
    getrandom::fill(&mut seed).map_err(|e| e.to_string())?;
    let key = SigningKey::from_bytes(&seed);
    fs::write(out, hex::encode(seed)).map_err(|e| e.to_string())?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = fs::set_permissions(out, fs::Permissions::from_mode(0o600));
    }
    println!("{}", hex::encode(key.verifying_key().to_bytes()));
    eprintln!("secret key written to {out}; the line above is the public key for the app");
    Ok(())
}

fn read_secret(path: &str) -> Result<SigningKey, String> {
    let raw = fs::read_to_string(path).map_err(|e| e.to_string())?;
    let bytes: [u8; 32] = hex::decode(raw.trim())
        .map_err(|_| "secret key file is not hex".to_string())?
        .try_into()
        .map_err(|_| "secret key must be 32 bytes".to_string())?;
    Ok(SigningKey::from_bytes(&bytes))
}

fn sign(cat: &str, secret: &str) -> Result<(), String> {
    let json = fs::read(cat).map_err(|e| e.to_string())?;
    let key = read_secret(secret)?;
    // Refuse to sign something the app would reject anyway.
    let sig = base64::engine::general_purpose::STANDARD.encode(key.sign(&json).to_bytes());
    let keys =
        TrustedKeys::from_bytes(&[key.verifying_key().to_bytes()]).map_err(|e| e.to_string())?;
    let parsed = Catalogue::verify(&json, &sig, &keys, 0).map_err(|e| e.to_string())?;
    fs::write(format!("{cat}.sig"), &sig).map_err(|e| e.to_string())?;
    println!(
        "signed {cat}: sequence {}, {} model versions",
        parsed.sequence,
        parsed.models.len()
    );
    Ok(())
}

fn verify(cat: &str, public: &str) -> Result<(), String> {
    let json = fs::read(cat).map_err(|e| e.to_string())?;
    let sig = fs::read_to_string(format!("{cat}.sig")).map_err(|e| e.to_string())?;
    let pk: [u8; 32] = hex::decode(public.trim())
        .map_err(|_| "public key is not hex".to_string())?
        .try_into()
        .map_err(|_| "public key must be 32 bytes".to_string())?;
    let keys = TrustedKeys::from_bytes(&[pk]).map_err(|e| e.to_string())?;
    let parsed = Catalogue::verify(&json, &sig, &keys, 0).map_err(|e| e.to_string())?;
    println!("ok: sequence {}", parsed.sequence);
    Ok(())
}

fn hash(file: &str) -> Result<(), String> {
    let size = fs::metadata(file).map_err(|e| e.to_string())?.len();
    let sha = voxa_models::download::sha256_file(std::path::Path::new(file))
        .map_err(|e| e.to_string())?;
    println!("{{\"sha256\": \"{sha}\", \"size\": {size}}}");
    Ok(())
}

fn pack(dir: &str, entry: &str, secret: &str) -> Result<(), String> {
    use voxa_models::pack::{MANIFEST, SIGNATURE};
    let root = std::path::Path::new(dir);
    let mut template: serde_json::Value =
        serde_json::from_slice(&fs::read(entry).map_err(|e| e.to_string())?)
            .map_err(|e| format!("{entry}: {e}"))?;
    let mut files = Vec::new();
    let mut stack = vec![root.to_path_buf()];
    while let Some(d) = stack.pop() {
        for e in fs::read_dir(&d).map_err(|e| e.to_string())? {
            let p = e.map_err(|e| e.to_string())?.path();
            if p.is_dir() {
                stack.push(p);
                continue;
            }
            let rel = p
                .strip_prefix(root)
                .unwrap()
                .to_string_lossy()
                .replace('\\', "/");
            if rel == MANIFEST || rel == SIGNATURE {
                continue;
            }
            let sha = voxa_models::download::sha256_file(&p).map_err(|e| e.to_string())?;
            let size = fs::metadata(&p).map_err(|e| e.to_string())?.len();
            files.push(serde_json::json!({"path": rel, "url": "", "sha256": sha, "size": size}));
        }
    }
    files.sort_by(|a, b| a["path"].as_str().cmp(&b["path"].as_str()));
    template["files"] = serde_json::Value::Array(files);
    let entry: voxa_models::ModelEntry =
        serde_json::from_value(template).map_err(|e| format!("{entry}: {e}"))?;
    entry.check().map_err(|e| e.to_string())?;
    let json = serde_json::to_vec_pretty(&entry).map_err(|e| e.to_string())?;
    let key = read_secret(secret)?;
    let sig = base64::engine::general_purpose::STANDARD.encode(key.sign(&json).to_bytes());
    fs::write(root.join(MANIFEST), &json).map_err(|e| e.to_string())?;
    fs::write(root.join(SIGNATURE), &sig).map_err(|e| e.to_string())?;
    let total: u64 = entry.files.iter().map(|f| f.size).sum();
    println!(
        "packed {} {}: {} files, {:.1} MB",
        entry.id,
        entry.version,
        entry.files.len(),
        total as f64 / 1e6
    );
    Ok(())
}
