//! Headless mode (roadmap M11a): the `voxa` command-line program and `voxa serve`.
//!
//! It shares the app's data folder, so voices installed in the desktop app are served as
//! they are. `voxa serve` listens on this machine only (127.0.0.1) by default, and on the
//! local network with `--lan`. Every request except `/v1/health` needs the server's
//! token (`Authorization: Bearer <token>`), kept in `<data>/serve-token`.
//!
//! API:
//! - `GET  /v1/health` → `{"ok": true, "version": "…"}`
//! - `GET  /v1/voices` → the voice list, as in the app (cloned voices included)
//! - `POST /v1/speech` with `{"text": "…", "voice": "harper", "speed": 1.0}` → `audio/wav`.
//!   Audio from cloned voices is watermarked and labelled, as in the app.

use std::io::Read;
use std::net::SocketAddr;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use serde::Deserialize;
use voxa_core::SampleFormat;

use crate::cloning::{self, Cloning};
use crate::licence::Licensing;
use crate::registry::{self, Registry, Voice};

/// The app's identifier: the data folder is `<OS data dir>/<identifier>`, as Tauri's
/// `app_data_dir`, so the desktop app and `voxa` share installed voices.
pub const IDENTIFIER: &str = "app.voxa.desktop";
/// Largest request body (JSON with up to a few thousand characters of text).
const MAX_BODY: usize = 64 * 1024;
pub const DEFAULT_PORT: u16 = 7788;

/// The app's default data folder.
pub fn default_data_dir() -> Option<PathBuf> {
    dirs::data_dir().map(|d| d.join(IDENTIFIER))
}

/// What the app and the server both speak with.
pub struct Services {
    pub reg: Arc<Registry>,
    pub lic: Arc<Licensing>,
    pub clones: Arc<Cloning>,
}

impl Services {
    pub fn open(data_dir: &Path) -> Result<Self, String> {
        Ok(Self {
            reg: Arc::new(Registry::open(data_dir, registry::trusted_keys())?),
            lic: Arc::new(Licensing::from_build(data_dir)),
            clones: Arc::new(Cloning::open(data_dir)?),
        })
    }

    /// Every voice, the installed models' and the user's cloned ones.
    pub fn voices(&self) -> Result<Vec<Voice>, String> {
        let mut v = self.reg.voices()?;
        v.extend(self.clones.voices(&self.reg));
        Ok(v)
    }
}

/// Speaks `text` and returns a 16-bit WAV. Cloned voices are watermarked and labelled.
pub fn speak_wav(
    reg: &Registry,
    clones: &Cloning,
    voice: &str,
    text: &str,
    speed: f32,
) -> Result<Vec<u8>, String> {
    if voice.starts_with(cloning::ID_PREFIX) {
        return clones
            .synthesize(reg, voice, text, speed)
            .map(|a| cloning::labelled_wav(&a));
    }
    reg.synthesize(voice, text, speed)
        .map(|a| a.to_wav(SampleFormat::Pcm16))
}

/// The server's token: created once (random), then read from `<data>/serve-token`.
pub fn token(data_dir: &Path) -> Result<String, String> {
    let path = data_dir.join("serve-token");
    if let Ok(t) = std::fs::read_to_string(&path) {
        let t = t.trim().to_string();
        if t.len() >= 32 {
            return Ok(t);
        }
    }
    let mut b = [0u8; 24];
    getrandom::fill(&mut b).map_err(|e| e.to_string())?;
    let t = hex::encode(b);
    std::fs::create_dir_all(data_dir).map_err(|e| e.to_string())?;
    std::fs::write(&path, &t).map_err(|e| format!("{}: {e}", path.display()))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600));
    }
    Ok(t)
}

/// A response: status, content type, body.
#[derive(Debug, PartialEq)]
pub struct Reply {
    pub status: u16,
    pub content_type: &'static str,
    pub body: Vec<u8>,
}

fn json(status: u16, v: serde_json::Value) -> Reply {
    Reply {
        status,
        content_type: "application/json",
        body: v.to_string().into_bytes(),
    }
}

fn error(status: u16, msg: &str) -> Reply {
    json(status, serde_json::json!({ "error": msg }))
}

/// Compares in constant time, so the token can't be guessed byte by byte from timings.
fn same(a: &str, b: &str) -> bool {
    a.len() == b.len()
        && a.bytes()
            .zip(b.bytes())
            .fold(0u8, |acc, (x, y)| acc | (x ^ y))
            == 0
}

#[derive(Deserialize)]
struct SpeechRequest {
    text: String,
    voice: String,
    #[serde(default = "one")]
    speed: f32,
}

fn one() -> f32 {
    1.0
}

/// Handles one request. `auth` is the `Authorization` header, if any.
pub fn handle(
    s: &Services,
    token: &str,
    method: &str,
    path: &str,
    auth: Option<&str>,
    body: &[u8],
) -> Reply {
    let path = path.split('?').next().unwrap_or(path);
    if (method, path) == ("GET", "/v1/health") {
        return json(
            200,
            serde_json::json!({ "ok": true, "version": env!("CARGO_PKG_VERSION") }),
        );
    }
    let authorised = auth
        .and_then(|a| a.strip_prefix("Bearer "))
        .is_some_and(|t| same(t.trim(), token));
    if !authorised {
        return error(
            401,
            "missing or wrong token (Authorization: Bearer <token>)",
        );
    }
    match (method, path) {
        ("GET", "/v1/voices") => match s.voices() {
            Ok(v) => json(200, serde_json::to_value(v).unwrap_or_default()),
            Err(e) => error(500, &e),
        },
        ("POST", "/v1/speech") => {
            if body.len() > MAX_BODY {
                return error(413, "request too large");
            }
            let req: SpeechRequest = match serde_json::from_slice(body) {
                Ok(r) => r,
                Err(e) => return error(400, &format!("bad request: {e}")),
            };
            if let Err(e) = s.lic.require() {
                return error(403, &e);
            }
            match speak_wav(&s.reg, &s.clones, &req.voice, &req.text, req.speed) {
                Ok(wav) => Reply {
                    status: 200,
                    content_type: "audio/wav",
                    body: wav,
                },
                Err(e) if e.contains("unknown voice") => error(404, &e),
                Err(e) => error(400, &e),
            }
        }
        (_, "/v1/voices" | "/v1/speech" | "/v1/health") => error(405, "method not allowed"),
        _ => error(404, "not found"),
    }
}

/// Serves until the process ends. Requests are handled one at a time, since each one
/// already uses the machine's cores.
pub fn serve(s: Services, token: String, addr: SocketAddr) -> Result<(), String> {
    let server = tiny_http::Server::http(addr).map_err(|e| format!("{addr}: {e}"))?;
    serve_on(server, s, token);
    Ok(())
}

fn serve_on(server: tiny_http::Server, s: Services, token: String) {
    for mut req in server.incoming_requests() {
        let auth = req
            .headers()
            .iter()
            .find(|h| h.field.equiv("Authorization"))
            .map(|h| h.value.as_str().to_string());
        let mut body = Vec::new();
        let too_big = req.body_length().is_some_and(|n| n > MAX_BODY);
        let reply = if too_big {
            error(413, "request too large")
        } else {
            let _ = req
                .as_reader()
                .take(MAX_BODY as u64 + 1)
                .read_to_end(&mut body);
            handle(
                &s,
                &token,
                req.method().as_str(),
                req.url(),
                auth.as_deref(),
                &body,
            )
        };
        let header = tiny_http::Header::from_bytes(&b"Content-Type"[..], reply.content_type)
            .expect("valid header");
        let _ = req.respond(
            tiny_http::Response::from_data(reply.body)
                .with_status_code(reply.status)
                .with_header(header),
        );
    }
}

const USAGE: &str = "\
voxa: Voxa from the command line (headless mode).

USAGE
  voxa serve [--port 7788] [--lan]     serve voices over HTTP (see `voxa help serve`)
  voxa voices                          list installed voices
  voxa import <voice-pack-folder>      install a signed voice pack
  voxa say <text> --voice <id> [--speed 1.0] -o <out.wav>
  voxa status                          licence status
  voxa activate <licence-key>          activate this machine
  voxa deactivate                      free this machine's activation

OPTIONS
  --data <folder>   use this data folder instead of the app's
";

const SERVE_HELP: &str = "\
voxa serve [--port 7788] [--lan]

Serves the installed voices over HTTP. Without --lan it listens on this machine only
(127.0.0.1); with --lan, on every network interface. Every request except /v1/health
needs the token printed at start (also in <data>/serve-token):

  curl -H \"Authorization: Bearer $TOKEN\" http://localhost:7788/v1/voices
  curl -H \"Authorization: Bearer $TOKEN\" -d '{\"text\":\"Hello.\",\"voice\":\"harper\"}' \\
       http://localhost:7788/v1/speech -o hello.wav
";

/// Takes `--name value` out of `args`.
fn take_opt(args: &mut Vec<String>, names: &[&str]) -> Result<Option<String>, String> {
    if let Some(i) = args.iter().position(|a| names.contains(&a.as_str())) {
        if i + 1 >= args.len() {
            return Err(format!("{} needs a value", args[i]));
        }
        let v = args.remove(i + 1);
        args.remove(i);
        return Ok(Some(v));
    }
    Ok(None)
}

fn take_flag(args: &mut Vec<String>, name: &str) -> bool {
    if let Some(i) = args.iter().position(|a| a == name) {
        args.remove(i);
        true
    } else {
        false
    }
}

/// Runs the `voxa` program; returns the text to print or an error.
pub fn cli(mut args: Vec<String>) -> Result<String, String> {
    let data = match take_opt(&mut args, &["--data"])? {
        Some(d) => PathBuf::from(d),
        None => default_data_dir().ok_or("no data folder on this system; pass --data")?,
    };
    let cmd = if args.is_empty() {
        "help".to_string()
    } else {
        args.remove(0)
    };
    match cmd.as_str() {
        "help" | "--help" | "-h" => Ok(match args.first().map(String::as_str) {
            Some("serve") => SERVE_HELP.into(),
            _ => USAGE.into(),
        }),
        "voices" => {
            let s = Services::open(&data)?;
            let v = s.voices()?;
            if v.is_empty() {
                return Ok(
                    "No voices installed. Install some in the app or with `voxa import`.".into(),
                );
            }
            Ok(v.iter()
                .map(|v| {
                    format!(
                        "{:<28} {:<18} {:<14} {}",
                        v.id, v.name, v.language, v.family
                    )
                })
                .collect::<Vec<_>>()
                .join("\n"))
        }
        "import" => {
            let dir = args
                .first()
                .ok_or("usage: voxa import <voice-pack-folder>")?;
            let s = Services::open(&data)?;
            let m = s.reg.import(Path::new(dir))?;
            Ok(format!("Installed {} {} ({})", m.id, m.version, m.family))
        }
        "say" => {
            let voice = take_opt(&mut args, &["--voice", "-v"])?.ok_or("say needs --voice")?;
            let out = take_opt(&mut args, &["-o", "--out"])?.ok_or("say needs -o <out.wav>")?;
            let speed = match take_opt(&mut args, &["--speed"])? {
                Some(s) => s.parse::<f32>().map_err(|_| "--speed must be a number")?,
                None => 1.0,
            };
            let text = args.join(" ");
            if text.trim().is_empty() {
                return Err("say needs some text".into());
            }
            let s = Services::open(&data)?;
            s.lic.require()?;
            let wav = speak_wav(&s.reg, &s.clones, &voice, &text, speed)?;
            std::fs::write(&out, &wav).map_err(|e| format!("{out}: {e}"))?;
            Ok(format!("Wrote {out}"))
        }
        "status" => {
            let s = Services::open(&data)?;
            Ok(serde_json::to_string_pretty(&s.lic.status()).unwrap_or_default())
        }
        "activate" => {
            let key = args.first().ok_or("usage: voxa activate <licence-key>")?;
            let s = Services::open(&data)?;
            let name = std::env::var("HOSTNAME").unwrap_or_else(|_| "voxa serve".into());
            let st = s.lic.activate(key, &name)?;
            Ok(serde_json::to_string_pretty(&st).unwrap_or_default())
        }
        "deactivate" => {
            let s = Services::open(&data)?;
            let st = s.lic.deactivate()?;
            Ok(serde_json::to_string_pretty(&st).unwrap_or_default())
        }
        "serve" => {
            let port = match take_opt(&mut args, &["--port", "-p"])? {
                Some(p) => p.parse::<u16>().map_err(|_| "--port must be a number")?,
                None => DEFAULT_PORT,
            };
            let host = if take_flag(&mut args, "--lan") {
                "0.0.0.0"
            } else {
                "127.0.0.1"
            };
            let addr: SocketAddr = format!("{host}:{port}")
                .parse()
                .map_err(|e| format!("{e}"))?;
            let s = Services::open(&data)?;
            let token = token(&data)?;
            eprintln!(
                "Voxa is serving {} voices on http://{addr} (data: {})",
                s.voices().map(|v| v.len()).unwrap_or(0),
                data.display()
            );
            eprintln!("Token: {token}");
            serve(s, token, addr)?;
            Ok(String::new())
        }
        other => Err(format!("unknown command {other:?}\n\n{USAGE}")),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn services() -> (tempfile::TempDir, Services) {
        let d = tempfile::tempdir().unwrap();
        let s = Services::open(d.path()).unwrap();
        (d, s)
    }

    #[test]
    fn token_is_created_once_and_private() {
        let d = tempfile::tempdir().unwrap();
        let t = token(d.path()).unwrap();
        assert_eq!(t.len(), 48);
        assert_eq!(token(d.path()).unwrap(), t);
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mode = std::fs::metadata(d.path().join("serve-token"))
                .unwrap()
                .permissions()
                .mode();
            assert_eq!(mode & 0o077, 0, "only the owner can read the token");
        }
    }

    #[test]
    fn health_is_open_and_everything_else_needs_the_token() {
        let (_d, s) = services();
        let t = "a".repeat(48);
        let r = handle(&s, &t, "GET", "/v1/health", None, b"");
        assert_eq!(r.status, 200);
        for auth in [
            None,
            Some("Bearer wrong"),
            Some("Basic aaaa"),
            Some(t.as_str()),
        ] {
            let r = handle(&s, &t, "GET", "/v1/voices", auth, b"");
            assert_eq!(r.status, 401, "{auth:?}");
        }
        let ok = format!("Bearer {t}");
        let r = handle(&s, &t, "GET", "/v1/voices?x=1", Some(&ok), b"");
        assert_eq!((r.status, r.body.as_slice()), (200, &b"[]"[..]));
    }

    #[test]
    fn speech_errors_are_clear() {
        let (_d, s) = services();
        let t = "b".repeat(48);
        let auth = format!("Bearer {t}");
        let call = |m: &str, p: &str, body: &[u8]| handle(&s, &t, m, p, Some(&auth), body);
        assert_eq!(call("POST", "/v1/speech", b"not json").status, 400);
        let r = call("POST", "/v1/speech", br#"{"text":"Hi.","voice":"harper"}"#);
        assert_eq!(r.status, 404, "{}", String::from_utf8_lossy(&r.body));
        assert_eq!(
            call("POST", "/v1/speech", &vec![b' '; MAX_BODY + 1]).status,
            413
        );
        assert_eq!(call("GET", "/v1/speech", b"").status, 405);
        assert_eq!(call("GET", "/v1/nothing", b"").status, 404);
    }

    #[test]
    fn serves_over_http() {
        let (_d, s) = services();
        let t = "c".repeat(48);
        let server = tiny_http::Server::http("127.0.0.1:0").unwrap();
        let addr = server.server_addr().to_ip().unwrap();
        let t2 = t.clone();
        std::thread::spawn(move || serve_on(server, s, t2));
        let base = format!("http://{addr}");
        let health: serde_json::Value = ureq::get(&format!("{base}/v1/health"))
            .call()
            .unwrap()
            .body_mut()
            .read_json()
            .unwrap();
        assert_eq!(health["ok"], true);
        let err = ureq::get(&format!("{base}/v1/voices")).call().unwrap_err();
        assert!(matches!(err, ureq::Error::StatusCode(401)), "{err}");
        let mut r = ureq::get(&format!("{base}/v1/voices"))
            .header("Authorization", &format!("Bearer {t}"))
            .call()
            .unwrap();
        assert_eq!(r.body_mut().read_to_string().unwrap(), "[]");
    }

    #[test]
    fn cli_parses_and_explains() {
        let d = tempfile::tempdir().unwrap();
        let data = d.path().to_string_lossy().to_string();
        let run = |a: &[&str]| {
            let mut v = vec!["--data".to_string(), data.clone()];
            v.extend(a.iter().map(|s| s.to_string()));
            cli(v)
        };
        assert!(run(&[]).unwrap().contains("voxa serve"));
        assert!(run(&["help", "serve"]).unwrap().contains("Bearer"));
        assert!(run(&["voices"]).unwrap().contains("No voices installed"));
        assert!(run(&["say", "Hello"]).unwrap_err().contains("--voice"));
        assert!(run(&["say", "Hi", "--voice", "harper"])
            .unwrap_err()
            .contains("-o"));
        assert!(run(&["say", "Hi", "--voice", "harper", "-o", "x.wav"])
            .unwrap_err()
            .contains("unknown voice"));
        assert!(run(&["serve", "--port"])
            .unwrap_err()
            .contains("needs a value"));
        assert!(run(&["frobnicate"])
            .unwrap_err()
            .contains("unknown command"));
        assert!(run(&["import"]).unwrap_err().contains("usage"));
    }

    /// `VOXA_KOKORO_PACK=<signed dev pack> cargo test -p voxa-app -- --ignored headless_e2e`
    #[test]
    #[ignore = "needs a signed Kokoro pack (VOXA_KOKORO_PACK)"]
    fn headless_e2e_import_and_serve_speech() {
        let d = tempfile::tempdir().unwrap();
        let data = d.path().to_string_lossy().to_string();
        let pack = std::env::var("VOXA_KOKORO_PACK").unwrap();
        let out = cli(vec!["--data".into(), data.clone(), "import".into(), pack]).unwrap();
        assert!(out.contains("Installed"), "{out}");
        let s = Services::open(d.path()).unwrap();
        let t = token(d.path()).unwrap();
        let server = tiny_http::Server::http("127.0.0.1:0").unwrap();
        let addr = server.server_addr().to_ip().unwrap();
        let t2 = t.clone();
        std::thread::spawn(move || serve_on(server, s, t2));
        let mut r = ureq::post(&format!("http://{addr}/v1/speech"))
            .header("Authorization", &format!("Bearer {t}"))
            .send_json(
                serde_json::json!({"text": "Hello from a headless Voxa.", "voice": "harper"}),
            )
            .unwrap();
        assert_eq!(r.headers().get("content-type").unwrap(), "audio/wav");
        let wav = voxa_core::WavFile::parse(&r.body_mut().read_to_vec().unwrap()).unwrap();
        assert!(wav.duration() > 1.0, "{}", wav.duration());
    }
}
