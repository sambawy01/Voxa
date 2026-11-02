//! Owner tool for publishing model catalogues.
//!
//! ```text
//! voxa-catalogue keygen <secret-key-file>          new Ed25519 key; prints the public key
//! voxa-catalogue sign <catalogue.json> <secret>    writes catalogue.json.sig
//! voxa-catalogue verify <catalogue.json> <public-key-hex>
//! voxa-catalogue hash <file>                       sha256 and size for a catalogue entry
//! ```
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
  voxa-catalogue hash <file>";

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
