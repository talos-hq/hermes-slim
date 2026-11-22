//! Streaming: download a seekable bundle frame by frame and unpack it as the frames land.
//!
//! The release publishes `ARCHIVE.frames` beside each bundle (`slim/zstd_frames.py`): the
//! archive's size and sha256, then every frame's compressed size, decompressed size and
//! sha256, then the seek table's. The installers pin that index (its sha256 is in the
//! release manifest) or the archive's sha256 (which the index must name).
//!
//! Frames are claimed in archive order by `connections` workers, each fetching one frame
//! with an HTTP range request. A frame is checked against the index only once all of its
//! bytes are in, and decoded only if it matches, so nothing unverified reaches the
//! decoder or the disk. The tar parser takes decoded frames in order while later ones are
//! still downloading. A dropped or stalled transfer resumes from the last byte received;
//! a frame whose bytes do not match is fetched again from scratch.

use super::{invalid, Frame};
use sha2::{Digest, Sha256};
use std::io::{self, Read};
use std::sync::Mutex;
use std::thread;
use std::time::{Duration, Instant};

pub type Hash = [u8; 32];

/// `ARCHIVE.frames`, parsed.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Index {
    pub size: u64,
    pub sha256: Hash,
    pub frames: Vec<(Frame, Hash)>,
    /// The seek table after the last frame: (offset, length, sha256).
    pub tail: (u64, u64, Hash),
}

pub fn hex(hash: &Hash) -> String {
    hash.iter().map(|b| format!("{b:02x}")).collect()
}

pub fn parse_hash(text: &str) -> Option<Hash> {
    if text.len() != 64 {
        return None;
    }
    let mut out = [0u8; 32];
    for (i, byte) in out.iter_mut().enumerate() {
        *byte = u8::from_str_radix(text.get(i * 2..i * 2 + 2)?, 16).ok()?;
    }
    Some(out)
}

pub fn sha256(bytes: &[u8]) -> Hash {
    Sha256::digest(bytes).into()
}

/// Parse an index; its frames must tile the archive exactly.
pub fn parse_index(text: &str) -> io::Result<Index> {
    let bad = |what: &str| invalid(format!("frame index: {what}"));
    let mut lines = text.lines();
    if lines.next() != Some("hermes-slim-frames 1") {
        return Err(bad("not a hermes-slim-frames 1 file"));
    }
    let (mut size, mut archive, mut tail) = (None, None, None);
    let mut frames = Vec::new();
    let mut offset = 0u64;
    for line in lines {
        let fields: Vec<&str> = line.split_whitespace().collect();
        let number = |i: usize| -> io::Result<u64> {
            fields
                .get(i)
                .and_then(|v| v.parse().ok())
                .ok_or_else(|| bad(line))
        };
        let hash = |i: usize| {
            fields
                .get(i)
                .and_then(|v| parse_hash(v))
                .ok_or_else(|| bad(line))
        };
        match fields.first().copied() {
            Some("archive") => {
                size = Some(number(1)?);
                archive = Some(hash(2)?);
            }
            Some("frame") if tail.is_none() => {
                let compressed = u32::try_from(number(1)?).map_err(|_| bad(line))?;
                let decompressed = u32::try_from(number(2)?).map_err(|_| bad(line))?;
                frames.push((
                    Frame {
                        offset,
                        compressed,
                        decompressed,
                    },
                    hash(3)?,
                ));
                offset += u64::from(compressed);
            }
            Some("tail") => tail = Some((offset, number(1)?, hash(2)?)),
            None => {}
            _ => return Err(bad(line)),
        }
    }
    let (Some(size), Some(sha256), Some(tail)) = (size, archive, tail) else {
        return Err(bad("missing its archive or tail line"));
    };
    if frames.is_empty() || tail.0 + tail.1 != size {
        return Err(bad("frames and tail do not add up to the archive size"));
    }
    Ok(Index {
        size,
        sha256,
        frames,
        tail,
    })
}

/// Timeouts and retry budget for the transfers.
#[derive(Debug, Clone, Copy)]
pub struct Patience {
    /// Connect / response / body time per request; a slow body resumes in a new request.
    pub request: Duration,
    /// Give up after this long without receiving a byte (the installers' window too).
    pub give_up: Duration,
    /// First backoff step; doubles to 30 s.
    pub backoff: Duration,
}

impl Default for Patience {
    fn default() -> Self {
        Patience {
            request: Duration::from_secs(30),
            give_up: Duration::from_secs(120),
            backoff: Duration::from_secs(1),
        }
    }
}

/// Range requests against one URL. GitHub redirects release downloads to a signed CDN URL;
/// that URL is resolved once and reused, and resolved again if it starts refusing.
pub struct Http {
    agent: ureq::Agent,
    url: String,
    resolved: Mutex<Option<String>>,
    patience: Patience,
}

enum Failure {
    /// Retry (with backoff); bytes received so far are kept.
    Transient(String),
    /// The resolved URL stopped working: resolve again, then retry.
    Stale(String),
    /// Retrying cannot help.
    Permanent(String),
}

impl Http {
    pub fn new(url: &str, patience: Patience) -> Self {
        let tls = ureq::tls::TlsConfig::builder()
            .root_certs(ureq::tls::RootCerts::PlatformVerifier)
            .build();
        let agent = ureq::Agent::config_builder()
            .tls_config(tls)
            .proxy(ureq::Proxy::try_from_env())
            .http_status_as_error(false)
            .timeout_connect(Some(patience.request))
            .timeout_recv_response(Some(patience.request))
            .timeout_recv_body(Some(patience.request))
            .user_agent(concat!("hermes-slim-unpack/", env!("CARGO_PKG_VERSION")))
            .build()
            .into();
        Http {
            agent,
            url: url.to_string(),
            resolved: Mutex::new(None),
            patience,
        }
    }

    /// The whole body of a small file (the index), with retries.
    pub fn get_small(&self, url: &str, limit: u64) -> io::Result<Vec<u8>> {
        self.retrying(url, |_| {
            let mut response = self
                .agent
                .get(url)
                .call()
                .map_err(|e| Failure::Transient(e.to_string()))?;
            match response.status().as_u16() {
                200 => {}
                404 | 410 => return Err(Failure::Permanent(format!("{url}: not found"))),
                code => return Err(Failure::Transient(format!("{url}: HTTP {code}"))),
            }
            let mut body = Vec::new();
            response
                .body_mut()
                .as_reader()
                .take(limit + 1)
                .read_to_end(&mut body)
                .map_err(|e| Failure::Transient(e.to_string()))?;
            if body.len() as u64 > limit {
                return Err(Failure::Permanent(format!(
                    "{url}: larger than {limit} bytes"
                )));
            }
            Ok(body)
        })
    }

    fn target(&self) -> String {
        self.resolved
            .lock()
            .unwrap()
            .clone()
            .unwrap_or_else(|| self.url.clone())
    }

    /// One request for `[start + got.len(), start + len)`, appending to `got`.
    fn request(&self, start: u64, len: u64, got: &mut Vec<u8>) -> Result<(), Failure> {
        let from = start + got.len() as u64;
        let last = start + len - 1;
        let target = self.target();
        let mut response = self
            .agent
            .get(&target)
            .header("Range", format!("bytes={from}-{last}"))
            .call()
            .map_err(|e| Failure::Transient(e.to_string()))?;
        let fresh = target == self.url;
        match response.status().as_u16() {
            206 => {}
            // A server that ignores Range would send the whole archive for every frame.
            200 => {
                return Err(Failure::Permanent(
                    "the server ignores range requests".into(),
                ))
            }
            404 | 410 if fresh => {
                return Err(Failure::Permanent(format!("{} not found", self.url)))
            }
            code @ 400..=499 if !fresh && code != 429 => {
                return Err(Failure::Stale(format!("HTTP {code} from the download URL")))
            }
            code => return Err(Failure::Transient(format!("HTTP {code}"))),
        }
        if fresh {
            use ureq::ResponseExt;
            let uri = response.get_uri().to_string();
            if uri != self.url {
                *self.resolved.lock().unwrap() = Some(uri);
            }
        }
        let range = response
            .headers()
            .get("content-range")
            .and_then(|v| v.to_str().ok())
            .unwrap_or("")
            .to_string();
        if !range.starts_with(&format!("bytes {from}-")) {
            return Err(Failure::Permanent(format!(
                "asked for bytes {from}-{last}, got Content-Range {range:?}"
            )));
        }
        let want = (last + 1 - from) as usize;
        let mut reader = response.body_mut().as_reader();
        let mut buf = vec![0u8; 64 << 10];
        let before = got.len();
        while got.len() - before < want {
            let room = (want - (got.len() - before)).min(buf.len());
            match reader.read(&mut buf[..room]) {
                Ok(0) => break,
                Ok(n) => got.extend_from_slice(&buf[..n]),
                Err(e) => return Err(Failure::Transient(e.to_string())),
            }
        }
        if got.len() - before < want {
            return Err(Failure::Transient("connection closed early".into()));
        }
        Ok(())
    }

    /// Bytes `[start, start + len)`, resuming across dropped or stalled transfers until no
    /// byte has arrived for `give_up`.
    pub fn range(&self, start: u64, len: u64) -> io::Result<Vec<u8>> {
        let mut got = Vec::with_capacity(len as usize);
        let mut backoff = self.patience.backoff;
        let mut progress = Instant::now();
        loop {
            let before = got.len();
            match self.request(start, len, &mut got) {
                Ok(()) => return Ok(got),
                Err(Failure::Permanent(why)) => return Err(io::Error::other(why)),
                Err(Failure::Stale(_)) => *self.resolved.lock().unwrap() = None,
                Err(Failure::Transient(why)) => {
                    if got.len() > before {
                        progress = Instant::now();
                        backoff = self.patience.backoff;
                    } else if progress.elapsed() >= self.patience.give_up {
                        return Err(io::Error::new(
                            io::ErrorKind::TimedOut,
                            format!("no progress for {:?}: {why}", self.patience.give_up),
                        ));
                    }
                    thread::sleep(backoff);
                    backoff = (backoff * 2).min(Duration::from_secs(30));
                }
            }
        }
    }

    fn retrying<T>(
        &self,
        url: &str,
        mut attempt: impl FnMut(usize) -> Result<T, Failure>,
    ) -> io::Result<T> {
        let started = Instant::now();
        let mut backoff = self.patience.backoff;
        for n in 0.. {
            match attempt(n) {
                Ok(v) => return Ok(v),
                Err(Failure::Permanent(why)) => return Err(io::Error::other(why)),
                Err(Failure::Transient(why) | Failure::Stale(why)) => {
                    if started.elapsed() >= self.patience.give_up {
                        return Err(io::Error::new(
                            io::ErrorKind::TimedOut,
                            format!("{url}: {why}"),
                        ));
                    }
                    thread::sleep(backoff);
                    backoff = (backoff * 2).min(Duration::from_secs(30));
                }
            }
        }
        unreachable!()
    }
}

/// How many times a frame whose bytes do not match the index is fetched again.
const REFETCHES: usize = 3;

/// Frame `i` from the network, verified against the index.
pub fn fetch_frame(http: &Http, index: &Index, i: usize) -> io::Result<Vec<u8>> {
    let (frame, want) = &index.frames[i];
    for _ in 0..=REFETCHES {
        let bytes = http.range(frame.offset, u64::from(frame.compressed))?;
        if sha256(&bytes) == *want {
            return Ok(bytes);
        }
    }
    Err(invalid(format!(
        "frame {i} (bytes {}+{}) does not match its sha256 after {} downloads",
        frame.offset,
        frame.compressed,
        REFETCHES + 1
    )))
}

/// The seek table after the frames, verified.
pub fn fetch_tail(http: &Http, index: &Index) -> io::Result<Vec<u8>> {
    let (offset, len, want) = index.tail;
    for _ in 0..=REFETCHES {
        let bytes = http.range(offset, len)?;
        if sha256(&bytes) == want {
            return Ok(bytes);
        }
    }
    Err(invalid("the seek table does not match its sha256".into()))
}
