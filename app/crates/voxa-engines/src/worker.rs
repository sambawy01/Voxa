//! Model workers (roadmap M11b): engines that run in a separate process, for larger models
//! that need Python with PyTorch/CUDA or MLX (Qwen3-TTS, RE-USE).
//!
//! A worker pack holds `worker.json`:
//!
//! ```json
//! {"protocol": 1, "command": ["python3", "-I", "worker.py"],
//!  "voices": [{"id": "aria", "name": "Aria", "gender": "female", "language": "en-US"}],
//!  "start_timeout_s": 300, "request_timeout_s": 600}
//! ```
//!
//! `command` runs in the pack's folder. It may only be `python3`/`python` or a program
//! inside the pack. Packs are signed, so this runs code Voxa shipped, but the rule stops a
//! pack from naming any other program on the machine. The protocol is newline-delimited
//! JSON over stdin/stdout (see `app/workers/voxa_worker.py`). The worker is started on
//! first use, every reply has a deadline, and a worker that dies is started again once.

use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError};
use std::sync::Mutex;
use std::time::Duration;

use base64::Engine as _;
use serde::Deserialize;
use voxa_core::{Audio, Engine, EngineError};

pub const PROTOCOL: u32 = 1;
/// Interpreters a worker may be started with; anything else must live inside the pack.
const INTERPRETERS: &[&str] = &["python3", "python"];

#[derive(Debug, Clone, Deserialize, PartialEq)]
pub struct WorkerVoice {
    pub id: String,
    pub name: String,
    #[serde(default)]
    pub gender: String,
    pub language: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct WorkerManifest {
    pub protocol: u32,
    /// The product name shown for its voices, e.g. "Voxa Studio".
    #[serde(default = "default_family")]
    pub family: String,
    pub command: Vec<String>,
    pub voices: Vec<WorkerVoice>,
    #[serde(default = "default_start")]
    pub start_timeout_s: u64,
    #[serde(default = "default_request")]
    pub request_timeout_s: u64,
}

fn default_family() -> String {
    "Voxa".into()
}

fn default_start() -> u64 {
    300
}
fn default_request() -> u64 {
    600
}

impl WorkerManifest {
    pub fn load(dir: &Path) -> Result<Self, EngineError> {
        let path = dir.join("worker.json");
        let text = std::fs::read_to_string(&path)
            .map_err(|e| EngineError::Failed(format!("{}: {e}", path.display())))?;
        let m: Self = serde_json::from_str(&text)
            .map_err(|e| EngineError::Failed(format!("worker.json: {e}")))?;
        if m.protocol != PROTOCOL {
            return Err(EngineError::Failed(format!(
                "this voice needs worker protocol {}, and this version of Voxa speaks {PROTOCOL}; update Voxa",
                m.protocol
            )));
        }
        let program = m
            .command
            .first()
            .ok_or_else(|| EngineError::Failed("worker.json has an empty command".into()))?;
        if !INTERPRETERS.contains(&program.as_str()) {
            let p = Path::new(program);
            let inside = p.is_relative()
                && p.components().all(|c| {
                    matches!(
                        c,
                        std::path::Component::Normal(_) | std::path::Component::CurDir
                    )
                });
            if !inside || !dir.join(p).is_file() {
                return Err(EngineError::Failed(format!(
                    "worker.json may only run python or a program inside the pack, not {program:?}"
                )));
            }
        }
        if m.voices.is_empty() {
            return Err(EngineError::Failed("worker.json lists no voices".into()));
        }
        Ok(m)
    }
}

struct Running {
    child: Child,
    stdin: ChildStdin,
    lines: Receiver<String>,
    next_id: u64,
}

impl Drop for Running {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

pub struct WorkerEngine {
    id: String,
    dir: PathBuf,
    manifest: WorkerManifest,
    /// Learned from the worker's hello; 0 until it has started.
    sample_rate: AtomicU32,
    running: Mutex<Option<Running>>,
}

#[derive(Deserialize)]
struct Reply {
    id: Option<u64>,
    ok: bool,
    #[serde(default)]
    error: Option<String>,
    #[serde(default)]
    protocol: Option<u32>,
    #[serde(default)]
    sample_rate: Option<u32>,
    #[serde(default)]
    voices: Option<Vec<String>>,
    #[serde(default)]
    samples: Option<String>,
}

fn failed(msg: impl Into<String>) -> EngineError {
    EngineError::Failed(msg.into())
}

impl WorkerEngine {
    /// Loads the manifest. The worker process starts on first use.
    pub fn load(id: &str, dir: &Path) -> Result<Self, EngineError> {
        let manifest = WorkerManifest::load(dir)?;
        Ok(Self {
            id: id.into(),
            dir: dir.to_path_buf(),
            manifest,
            sample_rate: AtomicU32::new(0),
            running: Mutex::new(None),
        })
    }

    pub fn manifest(&self) -> &WorkerManifest {
        &self.manifest
    }

    fn start(&self) -> Result<(Running, u32), EngineError> {
        let cmd = &self.manifest.command;
        let mut program = PathBuf::from(&cmd[0]);
        if !INTERPRETERS.contains(&cmd[0].as_str()) {
            program = self.dir.join(program);
        }
        let mut child = Command::new(&program)
            .args(&cmd[1..])
            .current_dir(&self.dir)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .spawn()
            .map_err(|e| {
                failed(format!(
                    "could not start the voice worker ({}): {e}",
                    cmd[0]
                ))
            })?;
        let stdin = child
            .stdin
            .take()
            .ok_or_else(|| failed("worker has no stdin"))?;
        let stdout = child
            .stdout
            .take()
            .ok_or_else(|| failed("worker has no stdout"))?;
        let (tx, lines) = mpsc::channel();
        std::thread::spawn(move || {
            for line in BufReader::new(stdout).lines() {
                let Ok(line) = line else { break };
                if tx.send(line).is_err() {
                    break;
                }
            }
        });
        let mut r = Running {
            child,
            stdin,
            lines,
            next_id: 1,
        };
        let hello = Self::call(
            &mut r,
            serde_json::json!({"op": "hello", "protocol": PROTOCOL}),
            Duration::from_secs(self.manifest.start_timeout_s),
        )?;
        if hello.protocol != Some(PROTOCOL) {
            return Err(failed("the voice worker speaks a different protocol"));
        }
        let rate = hello
            .sample_rate
            .filter(|r| *r > 0)
            .ok_or_else(|| failed("the voice worker gave no sample rate"))?;
        let have = hello.voices.unwrap_or_default();
        if let Some(v) = self.manifest.voices.iter().find(|v| !have.contains(&v.id)) {
            return Err(failed(format!(
                "the voice worker does not offer {:?}, which its pack lists",
                v.id
            )));
        }
        Ok((r, rate))
    }

    /// Sends one request and waits for its reply.
    fn call(
        r: &mut Running,
        mut req: serde_json::Value,
        timeout: Duration,
    ) -> Result<Reply, EngineError> {
        let id = r.next_id;
        r.next_id += 1;
        req["id"] = id.into();
        let line = format!("{req}\n");
        r.stdin
            .write_all(line.as_bytes())
            .and_then(|_| r.stdin.flush())
            .map_err(|_| failed("the voice worker stopped"))?;
        let deadline = std::time::Instant::now() + timeout;
        loop {
            let left = deadline.saturating_duration_since(std::time::Instant::now());
            match r.lines.recv_timeout(left) {
                Ok(line) => {
                    let reply: Reply = match serde_json::from_str(&line) {
                        Ok(rep) => rep,
                        Err(_) => continue, // not protocol output; workers must log to stderr
                    };
                    if reply.id != Some(id) {
                        continue;
                    }
                    if !reply.ok {
                        return Err(failed(
                            reply
                                .error
                                .unwrap_or_else(|| "the voice worker failed".into()),
                        ));
                    }
                    return Ok(reply);
                }
                Err(RecvTimeoutError::Timeout) => {
                    return Err(failed(format!(
                        "the voice worker did not answer within {} s",
                        timeout.as_secs()
                    )))
                }
                Err(RecvTimeoutError::Disconnected) => {
                    return Err(failed("the voice worker stopped"))
                }
            }
        }
    }

    fn synthesize_once(
        &self,
        sentence: &str,
        voice: &str,
        speed: f32,
    ) -> Result<Audio, EngineError> {
        let mut guard = self.running.lock().unwrap();
        if guard.is_none() {
            let (r, rate) = self.start()?;
            self.sample_rate.store(rate, Ordering::Relaxed);
            *guard = Some(r);
        }
        let r = guard.as_mut().expect("started");
        let reply = Self::call(
            r,
            serde_json::json!({"op": "synthesize", "text": sentence, "voice": voice, "speed": speed}),
            Duration::from_secs(self.manifest.request_timeout_s),
        );
        let reply = match reply {
            Ok(rep) => rep,
            Err(e) => {
                // A dead or stuck worker is dropped (killed). Don't ask the OS whether it has
                // exited: its output closes before it is reaped. An error reply leaves it running.
                let msg = e.to_string();
                if msg.contains("stopped") || msg.contains("did not answer") {
                    *guard = None;
                }
                return Err(e);
            }
        };
        let rate = reply
            .sample_rate
            .ok_or_else(|| failed("no sample rate in the reply"))?;
        let bytes = base64::engine::general_purpose::STANDARD
            .decode(reply.samples.unwrap_or_default())
            .map_err(|_| failed("the voice worker sent malformed audio"))?;
        if bytes.len() % 4 != 0 {
            return Err(failed("the voice worker sent malformed audio"));
        }
        let samples = bytes
            .as_chunks::<4>()
            .0
            .iter()
            .map(|c| f32::from_le_bytes(*c))
            .collect();
        Ok(Audio::new(rate, samples))
    }
}

impl Engine for WorkerEngine {
    fn id(&self) -> &str {
        &self.id
    }

    /// The worker's rate, known once it has started; 0 before.
    fn sample_rate(&self) -> u32 {
        self.sample_rate.load(Ordering::Relaxed)
    }

    fn voices(&self) -> Vec<String> {
        self.manifest.voices.iter().map(|v| v.id.clone()).collect()
    }

    fn synthesize_sentence(
        &self,
        sentence: &str,
        voice: &str,
        speed: f32,
    ) -> Result<Audio, EngineError> {
        match self.synthesize_once(sentence, voice, speed) {
            Err(e) if e.to_string().contains("stopped") => {
                // The worker died (crash, out of memory): start it again, once.
                self.synthesize_once(sentence, voice, speed)
            }
            r => r,
        }
    }

    fn synthesize(
        &self,
        req: &voxa_core::SynthesisRequest,
        on_progress: &mut dyn FnMut(usize, usize),
    ) -> Result<Audio, EngineError> {
        // The default joins sentences at `self.sample_rate()`, which a worker only learns
        // when it starts; join at the rate of the first sentence instead.
        let text = voxa_core::validate_text(&req.text)?;
        if !(0.5..=2.0).contains(&req.speed) {
            return Err(EngineError::BadSpeed(req.speed));
        }
        if !self.voices().iter().any(|v| v == &req.voice) {
            return Err(EngineError::UnknownVoice(req.voice.clone()));
        }
        let sentences = voxa_core::split_sentences(text);
        let total = sentences.len();
        on_progress(0, total);
        let mut out: Option<Audio> = None;
        for (i, s) in sentences.iter().enumerate() {
            let a = self.synthesize_sentence(s, &req.voice, req.speed)?;
            match out.as_mut() {
                None => out = Some(a),
                Some(o) if o.sample_rate == a.sample_rate => {
                    o.append(&a, voxa_core::engine::SENTENCE_GAP_S)
                }
                Some(o) => {
                    return Err(failed(format!(
                        "the voice worker changed sample rate ({} to {} Hz)",
                        o.sample_rate, a.sample_rate
                    )))
                }
            }
            on_progress(i + 1, total);
        }
        out.ok_or_else(|| failed("no text"))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use voxa_core::SynthesisRequest;

    fn python() -> String {
        for p in ["python3", "python"] {
            if Command::new(p)
                .arg("--version")
                .output()
                .is_ok_and(|o| o.status.success())
            {
                return p.into();
            }
        }
        panic!("these tests need Python 3 (python3 or python on PATH)");
    }

    /// A pack folder running the reference worker with extra arguments.
    fn pack(extra: &[&str], voices: &str) -> tempfile::TempDir {
        let d = tempfile::tempdir().unwrap();
        let workers = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../workers");
        for f in ["voxa_worker.py", "reference_worker.py"] {
            std::fs::copy(workers.join(f), d.path().join(f)).unwrap();
        }
        let mut cmd = vec![python(), "-I".into(), "reference_worker.py".into()];
        cmd.extend(extra.iter().map(|s| s.to_string()));
        let manifest = serde_json::json!({
            "protocol": 1, "command": cmd, "start_timeout_s": 20, "request_timeout_s": 20,
            "voices": serde_json::from_str::<serde_json::Value>(voices).unwrap(),
        });
        std::fs::write(d.path().join("worker.json"), manifest.to_string()).unwrap();
        d
    }

    const VOICES: &str = r#"[{"id":"low","name":"Low","language":"en-US"},
                             {"id":"high","name":"High","language":"en-US"}]"#;

    fn req(text: &str, voice: &str) -> SynthesisRequest {
        SynthesisRequest {
            text: text.into(),
            voice: voice.into(),
            speed: 1.0,
        }
    }

    #[test]
    fn speaks_through_the_reference_worker() {
        let d = pack(&[], VOICES);
        let e = WorkerEngine::load("ref", d.path()).unwrap();
        assert_eq!(e.voices(), ["low", "high"]);
        let mut seen = Vec::new();
        let a = e
            .synthesize(&req("One two three. Four five.", "high"), &mut |x, n| {
                seen.push((x, n))
            })
            .unwrap();
        assert_eq!(a.sample_rate, 16_000);
        assert_eq!(seen, vec![(0, 2), (1, 2), (2, 2)]);
        // Five words of 0.16 s each, plus the gap between the two sentences.
        let words = 5.0 * (0.12 + 0.04);
        assert!((a.duration() - words).abs() < 0.3, "{}", a.duration());
        assert!(matches!(
            e.synthesize(&req("Hi.", "nobody"), &mut |_, _| {}),
            Err(EngineError::UnknownVoice(_))
        ));
    }

    #[test]
    fn worker_errors_come_back_in_plain_words() {
        let d = pack(&["--fail-after", "1"], VOICES);
        let e = WorkerEngine::load("ref", d.path()).unwrap();
        let err = e
            .synthesize(&req("Hello.", "low"), &mut |_, _| {})
            .unwrap_err();
        assert!(err.to_string().contains("out of GPU memory"), "{err}");
    }

    #[test]
    fn a_crashed_worker_is_restarted_once() {
        // Crashes during its 2nd synthesis; the restarted worker counts from 1 again.
        let d = pack(&["--crash-after", "2"], VOICES);
        let e = WorkerEngine::load("ref", d.path()).unwrap();
        e.synthesize(&req("First.", "low"), &mut |_, _| {}).unwrap();
        e.synthesize(&req("Second.", "low"), &mut |_, _| {})
            .unwrap();
        e.synthesize(&req("Third.", "low"), &mut |_, _| {}).unwrap();
    }

    #[test]
    fn a_pack_must_list_voices_the_worker_has() {
        let d = pack(&[], r#"[{"id":"ghost","name":"Ghost","language":"en-US"}]"#);
        let e = WorkerEngine::load("ref", d.path()).unwrap();
        let err = e
            .synthesize(&req("Hi.", "ghost"), &mut |_, _| {})
            .unwrap_err();
        assert!(err.to_string().contains("does not offer"), "{err}");
    }

    #[test]
    fn manifests_may_only_run_python_or_the_pack_s_own_programs() {
        let d = tempfile::tempdir().unwrap();
        let write = |cmd: serde_json::Value, protocol: u32| {
            std::fs::write(
                d.path().join("worker.json"),
                serde_json::json!({"protocol": protocol, "command": cmd,
                    "voices": [{"id": "a", "name": "A", "language": "en"}]})
                .to_string(),
            )
            .unwrap();
            WorkerManifest::load(d.path())
        };
        for bad in [
            serde_json::json!(["/bin/sh", "-c", "echo"]),
            serde_json::json!(["curl"]),
            serde_json::json!(["../outside"]),
            serde_json::json!([]),
        ] {
            assert!(write(bad.clone(), 1).is_err(), "{bad}");
        }
        assert!(write(serde_json::json!(["python3", "-I", "w.py"]), 1).is_ok());
        // A program of the pack's own must exist inside it ("curl" means <pack>/curl).
        assert!(write(serde_json::json!(["./bin/worker"]), 1).is_err());
        std::fs::create_dir_all(d.path().join("bin")).unwrap();
        std::fs::write(d.path().join("bin/worker"), "").unwrap();
        assert!(write(serde_json::json!(["./bin/worker"]), 1).is_ok());
        let err = write(serde_json::json!(["python3"]), 2).unwrap_err();
        assert!(err.to_string().contains("update Voxa"), "{err}");
    }

    #[test]
    fn a_worker_that_never_answers_times_out() {
        let d = tempfile::tempdir().unwrap();
        std::fs::write(d.path().join("silent.py"), "import time\ntime.sleep(60)\n").unwrap();
        std::fs::write(
            d.path().join("worker.json"),
            serde_json::json!({"protocol": 1, "command": [python(), "-I", "silent.py"],
                "start_timeout_s": 1,
                "voices": [{"id": "a", "name": "A", "language": "en"}]})
            .to_string(),
        )
        .unwrap();
        let e = WorkerEngine::load("silent", d.path()).unwrap();
        let t = std::time::Instant::now();
        let err = e.synthesize(&req("Hi.", "a"), &mut |_, _| {}).unwrap_err();
        assert!(
            err.to_string().contains("did not answer within 1 s"),
            "{err}"
        );
        assert!(t.elapsed() < Duration::from_secs(10));
    }

    /// Voxa Studio (Qwen3-TTS) through its real worker, checked with speech recognition:
    /// `VOXA_STUDIO_DIR=<built studio package> VOXA_CONSENT_DIR=<consent models> \
    ///  cargo test --release -p voxa-engines -- --ignored studio_e2e --nocapture`
    #[test]
    #[ignore = "needs a Voxa Studio package and the consent models"]
    fn studio_e2e_speaks_english_intelligibly() {
        let dir = PathBuf::from(std::env::var("VOXA_STUDIO_DIR").unwrap());
        let asr = crate::consent::ConsentChecker::load(
            Path::new(&std::env::var("VOXA_CONSENT_DIR").unwrap()),
            2,
        )
        .unwrap();
        let e = WorkerEngine::load("studio", &dir).unwrap();
        assert!(e.voices().contains(&"studio-ryan".to_string()));
        let words = |s: &str| -> Vec<String> {
            s.to_lowercase()
                .split(|c: char| !c.is_alphanumeric())
                .filter(|w| !w.is_empty())
                .map(str::to_string)
                .collect()
        };
        let (mut errors, mut total, mut secs, mut took) = (0usize, 0usize, 0f64, 0f64);
        for (voice, text) in [
            (
                "studio-ryan",
                "Thank you for calling. Please leave a message after the tone.",
            ),
            (
                "studio-aiden",
                "The meeting has moved to Thursday afternoon at noon.",
            ),
        ] {
            let t = std::time::Instant::now();
            let a = e.synthesize(&req(text, voice), &mut |_, _| {}).unwrap();
            took += t.elapsed().as_secs_f64();
            secs += a.duration();
            assert_eq!(a.sample_rate, 24_000);
            let heard = asr.transcribe(&a.samples, a.sample_rate);
            let (want, got) = (words(text), words(&heard));
            // Word-level edit distance.
            let mut prev: Vec<usize> = (0..=got.len()).collect();
            for (i, x) in want.iter().enumerate() {
                let mut cur = vec![i + 1];
                for (j, y) in got.iter().enumerate() {
                    cur.push(
                        (prev[j] + usize::from(x != y))
                            .min(prev[j + 1] + 1)
                            .min(cur[j] + 1),
                    );
                }
                prev = cur;
            }
            errors += prev[got.len()];
            total += want.len();
            eprintln!("{voice}: {text:?} -> {heard:?} ({:.2} s)", a.duration());
        }
        let wer = errors as f64 / total as f64;
        eprintln!(
            "studio: WER {:.1}%, {:.1} s of speech in {:.1} s ({:.2}x real time, first call includes loading)",
            100.0 * wer,
            secs,
            took,
            secs / took
        );
        assert!(wer <= 0.10, "word error rate {:.1}%", 100.0 * wer);
    }
}
