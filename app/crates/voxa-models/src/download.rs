//! Downloads model files from Voxa's mirror, resuming interrupted downloads and verifying
//! size and sha256 before a file is accepted.

use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::path::Path;

use sha2::{Digest, Sha256};
use thiserror::Error;

use crate::catalogue::ModelFile;

#[derive(Debug, Error)]
pub enum DownloadError {
    #[error("refusing to download from {0}: only https is allowed")]
    InsecureUrl(String),
    #[error("network error: {0}")]
    Network(String),
    #[error("server answered {0}")]
    Status(u16),
    #[error("{path}: expected {expected} bytes, got {got}")]
    WrongSize {
        path: String,
        expected: u64,
        got: u64,
    },
    #[error("{0}: checksum does not match the catalogue; the file was discarded")]
    HashMismatch(String),
    #[error("disk error: {0}")]
    Io(#[from] std::io::Error),
}

pub struct Downloader {
    agent: ureq::Agent,
    /// Tests serve files from 127.0.0.1 over plain http. Never set in the app.
    allow_local_http: bool,
}

impl Default for Downloader {
    fn default() -> Self {
        Self::new()
    }
}

impl Downloader {
    pub fn new() -> Self {
        let agent = ureq::Agent::config_builder()
            .http_status_as_error(false)
            .timeout_connect(Some(std::time::Duration::from_secs(20)))
            .build()
            .into();
        Self {
            agent,
            allow_local_http: false,
        }
    }

    #[cfg(test)]
    pub(crate) fn for_local_tests() -> Self {
        Self {
            allow_local_http: true,
            ..Self::new()
        }
    }

    fn check_url(&self, url: &str) -> Result<(), DownloadError> {
        let local = url.starts_with("http://127.0.0.1:") || url.starts_with("http://localhost:");
        if url.starts_with("https://") || (self.allow_local_http && local) {
            Ok(())
        } else {
            Err(DownloadError::InsecureUrl(url.into()))
        }
    }

    /// Downloads `file` to `dest`, using `part` as the resumable partial file.
    /// `on_bytes(downloaded, total)` reports progress. On success `part` is renamed to
    /// `dest`; on a checksum failure `part` is deleted so the next attempt starts clean.
    pub fn fetch(
        &self,
        file: &ModelFile,
        part: &Path,
        dest: &Path,
        on_bytes: &mut dyn FnMut(u64, u64),
    ) -> Result<(), DownloadError> {
        self.check_url(&file.url)?;
        let mut have = fs::metadata(part).map(|m| m.len()).unwrap_or(0);
        if have > file.size {
            fs::remove_file(part)?;
            have = 0;
        }
        if have < file.size {
            self.download_rest(file, part, have, on_bytes)?;
        }
        verify(file, part)?;
        fs::rename(part, dest)?;
        on_bytes(file.size, file.size);
        Ok(())
    }

    fn download_rest(
        &self,
        file: &ModelFile,
        part: &Path,
        have: u64,
        on_bytes: &mut dyn FnMut(u64, u64),
    ) -> Result<(), DownloadError> {
        let mut req = self.agent.get(&file.url);
        if have > 0 {
            req = req.header("Range", format!("bytes={have}-"));
        }
        let resp = req
            .call()
            .map_err(|e| DownloadError::Network(e.to_string()))?;
        let status = resp.status().as_u16();
        let (mut out, mut written) = match status {
            // Server resumed from where we stopped.
            206 if have > 0 => (OpenOptions::new().append(true).open(part)?, have),
            // Full body: start over (server ignored Range, or this is the first attempt).
            200 => (File::create(part)?, 0),
            other => return Err(DownloadError::Status(other)),
        };
        let mut reader = resp.into_body().into_reader();
        let mut buf = vec![0u8; 64 * 1024];
        loop {
            let n = reader
                .read(&mut buf)
                .map_err(|e| DownloadError::Network(e.to_string()))?;
            if n == 0 {
                break;
            }
            // Never write more than the catalogue says: a hostile or broken server cannot
            // fill the disk.
            if written + n as u64 > file.size {
                drop(out);
                fs::remove_file(part)?;
                return Err(DownloadError::WrongSize {
                    path: file.path.clone(),
                    expected: file.size,
                    got: written + n as u64,
                });
            }
            out.write_all(&buf[..n])?;
            written += n as u64;
            on_bytes(written, file.size);
        }
        out.sync_all()?;
        Ok(())
    }
}

/// Checks size and sha256 of a downloaded file; deletes it on mismatch.
fn verify(file: &ModelFile, path: &Path) -> Result<(), DownloadError> {
    let got = fs::metadata(path)?.len();
    if got != file.size {
        // Short file: keep it, a later attempt resumes from here.
        return Err(DownloadError::WrongSize {
            path: file.path.clone(),
            expected: file.size,
            got,
        });
    }
    if sha256_file(path)? != file.sha256.to_ascii_lowercase() {
        fs::remove_file(path)?;
        return Err(DownloadError::HashMismatch(file.path.clone()));
    }
    Ok(())
}

pub fn sha256_file(path: &Path) -> std::io::Result<String> {
    let mut f = File::open(path)?;
    let mut h = Sha256::new();
    let mut buf = vec![0u8; 64 * 1024];
    loop {
        let n = f.read(&mut buf)?;
        if n == 0 {
            break;
        }
        h.update(&buf[..n]);
    }
    Ok(hex::encode(h.finalize()))
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    use std::io::{BufRead, BufReader};
    use std::net::TcpListener;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::Arc;

    /// A tiny HTTP server for tests: serves `body`, honours `Range: bytes=N-` when
    /// `ranges` is true, and cuts the connection after `cut_after` bytes on the first
    /// request (to simulate a dropped download).
    pub struct TestServer {
        pub url: String,
        pub requests: Arc<AtomicUsize>,
    }

    impl TestServer {
        pub fn start(body: Vec<u8>, ranges: bool, cut_after: Option<usize>) -> Self {
            let listener = TcpListener::bind("127.0.0.1:0").unwrap();
            let url = format!("http://{}/file.bin", listener.local_addr().unwrap());
            let requests = Arc::new(AtomicUsize::new(0));
            let count = Arc::clone(&requests);
            std::thread::spawn(move || {
                for stream in listener.incoming() {
                    let Ok(mut stream) = stream else { continue };
                    let n = count.fetch_add(1, Ordering::SeqCst);
                    let mut reader = BufReader::new(stream.try_clone().unwrap());
                    let mut start = 0usize;
                    loop {
                        let mut line = String::new();
                        if reader.read_line(&mut line).unwrap_or(0) == 0 || line == "\r\n" {
                            break;
                        }
                        if let Some(v) = line.to_ascii_lowercase().strip_prefix("range: bytes=") {
                            start = v.trim().trim_end_matches('-').parse().unwrap_or(0);
                        }
                    }
                    let (status, slice) = if ranges && start > 0 {
                        ("206 Partial Content", &body[start.min(body.len())..])
                    } else {
                        ("200 OK", &body[..])
                    };
                    let head = format!(
                        "HTTP/1.1 {status}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                        slice.len()
                    );
                    let _ = stream.write_all(head.as_bytes());
                    match cut_after {
                        Some(c) if n == 0 => {
                            let _ = stream.write_all(&slice[..c.min(slice.len())]);
                        }
                        _ => {
                            let _ = stream.write_all(slice);
                        }
                    }
                    let _ = stream.flush();
                }
            });
            TestServer { url, requests }
        }
    }

    pub fn blob(n: usize) -> Vec<u8> {
        (0..n).map(|i| (i * 31 % 251) as u8).collect()
    }

    pub fn file_for(body: &[u8], url: &str) -> ModelFile {
        ModelFile {
            path: "model.onnx".into(),
            url: url.into(),
            sha256: hex::encode(Sha256::digest(body)),
            size: body.len() as u64,
        }
    }

    #[test]
    fn downloads_and_verifies() {
        let body = blob(200_000);
        let srv = TestServer::start(body.clone(), true, None);
        let dir = tempfile::tempdir().unwrap();
        let (part, dest) = (dir.path().join("f.part"), dir.path().join("f"));
        let mut last = (0, 0);
        Downloader::for_local_tests()
            .fetch(&file_for(&body, &srv.url), &part, &dest, &mut |a, b| {
                last = (a, b)
            })
            .unwrap();
        assert_eq!(fs::read(&dest).unwrap(), body);
        assert!(!part.exists());
        assert_eq!(last, (200_000, 200_000));
    }

    #[test]
    fn resumes_after_a_dropped_connection() {
        let body = blob(300_000);
        let srv = TestServer::start(body.clone(), true, Some(100_000));
        let dir = tempfile::tempdir().unwrap();
        let (part, dest) = (dir.path().join("f.part"), dir.path().join("f"));
        let file = file_for(&body, &srv.url);
        let d = Downloader::for_local_tests();
        // First attempt is cut short; the partial file is kept.
        assert!(d.fetch(&file, &part, &dest, &mut |_, _| {}).is_err());
        let partial = fs::metadata(&part).unwrap().len();
        assert!(partial > 0 && partial < 300_000);
        // Second attempt resumes with a Range request and completes.
        d.fetch(&file, &part, &dest, &mut |_, _| {}).unwrap();
        assert_eq!(fs::read(&dest).unwrap(), body);
        assert_eq!(srv.requests.load(Ordering::SeqCst), 2);
    }

    #[test]
    fn restarts_when_server_ignores_range() {
        let body = blob(50_000);
        let srv = TestServer::start(body.clone(), false, None);
        let dir = tempfile::tempdir().unwrap();
        let (part, dest) = (dir.path().join("f.part"), dir.path().join("f"));
        fs::write(&part, &body[..1000]).unwrap(); // stale partial
        Downloader::for_local_tests()
            .fetch(&file_for(&body, &srv.url), &part, &dest, &mut |_, _| {})
            .unwrap();
        assert_eq!(fs::read(&dest).unwrap(), body);
    }

    #[test]
    fn rejects_and_discards_corrupt_files() {
        let body = blob(10_000);
        let srv = TestServer::start(body.clone(), true, None);
        let dir = tempfile::tempdir().unwrap();
        let (part, dest) = (dir.path().join("f.part"), dir.path().join("f"));
        let mut file = file_for(&body, &srv.url);
        file.sha256 = "ab".repeat(32);
        let err = Downloader::for_local_tests()
            .fetch(&file, &part, &dest, &mut |_, _| {})
            .unwrap_err();
        assert!(matches!(err, DownloadError::HashMismatch(_)));
        assert!(!part.exists() && !dest.exists());
    }

    #[test]
    fn refuses_bodies_larger_than_the_catalogue_says() {
        let body = blob(10_000);
        let srv = TestServer::start(body.clone(), true, None);
        let dir = tempfile::tempdir().unwrap();
        let (part, dest) = (dir.path().join("f.part"), dir.path().join("f"));
        let mut file = file_for(&body, &srv.url);
        file.size = 5_000;
        let err = Downloader::for_local_tests()
            .fetch(&file, &part, &dest, &mut |_, _| {})
            .unwrap_err();
        assert!(matches!(err, DownloadError::WrongSize { .. }));
        assert!(!part.exists());
    }

    #[test]
    fn refuses_plain_http() {
        let dir = tempfile::tempdir().unwrap();
        let file = file_for(b"x", "http://example.com/x");
        let err = Downloader::new()
            .fetch(
                &file,
                &dir.path().join("p"),
                &dir.path().join("d"),
                &mut |_, _| {},
            )
            .unwrap_err();
        assert!(matches!(err, DownloadError::InsecureUrl(_)));
        // Local http is only for tests, never in the real downloader.
        let local = file_for(b"x", "http://127.0.0.1:1/x");
        assert!(matches!(
            Downloader::new().fetch(
                &local,
                &dir.path().join("p"),
                &dir.path().join("d"),
                &mut |_, _| {}
            ),
            Err(DownloadError::InsecureUrl(_))
        ));
    }
}
