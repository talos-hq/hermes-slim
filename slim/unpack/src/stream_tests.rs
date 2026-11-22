//! `--url`: a seekable archive and its frame index served by a small HTTP/1.1 server that can
//! break a transfer the ways a bad connection does (dropped, stalled, corrupted, refused,
//! range-ignoring), and can hold the last frame back until the first file is on disk.

use super::tests::{assert_tree, bundle_tar, noise, seekable, Scratch};
use super::*;
use std::collections::HashMap;
use std::io::{BufRead, BufReader as Lines};
use std::net::{TcpListener, TcpStream};

#[derive(Default)]
struct Faults {
    /// Serve bytes up to this archive offset, then close (the first time it is crossed).
    drop_at: Option<u64>,
    /// Serve bytes up to this offset, then go quiet past the client's timeout (once).
    stall_at: Option<u64>,
    /// Flip this byte the first time it is served.
    corrupt_once_at: Option<u64>,
    /// Flip this byte every time.
    corrupt_always_at: Option<u64>,
    ignore_ranges: bool,
    /// Answer this many archive requests with 503 first.
    unavailable: usize,
    /// Hold a request for bytes at or past `gate_from` until this file exists.
    gate: Option<(PathBuf, u64)>,
    gate_opened: Option<bool>,
}

struct Server {
    base: String,
    log: Arc<Mutex<Vec<String>>>,
    faults: Arc<Mutex<Faults>>,
}

const ARCHIVE: &str = "/b.tar.zst";

impl Server {
    fn start(files: HashMap<String, Vec<u8>>, faults: Faults) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let base = format!("http://{}", listener.local_addr().unwrap());
        let (log, faults) = (
            Arc::new(Mutex::new(Vec::new())),
            Arc::new(Mutex::new(faults)),
        );
        let files = Arc::new(files);
        let (l, f) = (log.clone(), faults.clone());
        thread::spawn(move || {
            for conn in listener.incoming().flatten() {
                let (files, log, faults) = (files.clone(), l.clone(), f.clone());
                thread::spawn(move || serve(conn, &files, &log, &faults));
            }
        });
        Server { base, log, faults }
    }

    fn url(&self, path: &str) -> String {
        format!("{}{path}", self.base)
    }

    /// Range starts requested for the archive.
    fn starts(&self) -> Vec<u64> {
        self.log
            .lock()
            .unwrap()
            .iter()
            .filter_map(|l| l.strip_prefix(&format!("{ARCHIVE} bytes=")))
            .filter_map(|r| r.split('-').next()?.parse().ok())
            .collect()
    }
}

fn serve(
    mut conn: TcpStream,
    files: &HashMap<String, Vec<u8>>,
    log: &Mutex<Vec<String>>,
    faults: &Mutex<Faults>,
) {
    let mut reader = Lines::new(conn.try_clone().unwrap());
    let mut first = String::new();
    if reader.read_line(&mut first).is_err() {
        return;
    }
    let path = first.split_whitespace().nth(1).unwrap_or("/").to_string();
    let mut range = None;
    loop {
        let mut line = String::new();
        if reader.read_line(&mut line).unwrap_or(0) == 0 || line == "\r\n" {
            break;
        }
        if let Some(v) = line.to_ascii_lowercase().strip_prefix("range: bytes=") {
            let (a, b) = v.trim().split_once('-').unwrap();
            range = Some((a.parse::<u64>().unwrap(), b.parse::<u64>().unwrap()));
        }
    }
    log.lock().unwrap().push(match range {
        Some((a, b)) => format!("{path} bytes={a}-{b}"),
        None => path.clone(),
    });
    if let Some(target) = path.strip_prefix("/r") {
        let _ = write!(
            conn,
            "HTTP/1.1 302 Found\r\nLocation: {target}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
        );
        return;
    }
    let Some(body) = files.get(&path) else {
        let _ = write!(
            conn,
            "HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
        );
        return;
    };
    let is_archive = path == ARCHIVE;
    let mut f = faults.lock().unwrap();
    if is_archive && f.unavailable > 0 {
        f.unavailable -= 1;
        let _ = write!(
            conn,
            "HTTP/1.1 503 Busy\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
        );
        return;
    }
    let (start, end) = match range {
        Some((a, b)) if is_archive && !f.ignore_ranges => (a, b.min(body.len() as u64 - 1)),
        _ => (0, body.len() as u64 - 1),
    };
    let mut bytes = body[start as usize..=end as usize].to_vec();
    let within = |at: Option<u64>| at.filter(|&o| is_archive && o >= start && o <= end);
    for (at, once) in [(f.corrupt_once_at, true), (f.corrupt_always_at, false)] {
        if let Some(o) = within(at) {
            bytes[(o - start) as usize] ^= 0xff;
            if once {
                f.corrupt_once_at = None;
            }
        }
    }
    let cut = within(f.drop_at)
        .map(|o| (o, false))
        .or(within(f.stall_at).map(|o| (o, true)));
    if cut.is_some() {
        f.drop_at = None;
        f.stall_at = None;
    }
    let gate = f
        .gate
        .clone()
        .filter(|(_, from)| is_archive && start >= *from);
    drop(f);
    if let Some((path, _)) = gate {
        let opened = (0..200).any(|_| {
            thread::sleep(Duration::from_millis(50));
            path.exists()
        });
        let mut f = faults.lock().unwrap();
        f.gate_opened.get_or_insert(opened);
    }
    let head = if range.is_some() && is_archive && !faults.lock().unwrap().ignore_ranges {
        format!(
            "HTTP/1.1 206 Partial Content\r\nContent-Range: bytes {start}-{end}/{}\r\n",
            body.len()
        )
    } else {
        "HTTP/1.1 200 OK\r\n".to_string()
    };
    let _ = write!(
        conn,
        "{head}Content-Length: {}\r\nConnection: close\r\n\r\n",
        bytes.len()
    );
    match cut {
        Some((at, stall)) => {
            let _ = conn.write_all(&bytes[..(at - start) as usize]);
            let _ = conn.flush();
            if stall {
                thread::sleep(Duration::from_secs(3));
            }
        }
        None => {
            let _ = conn.write_all(&bytes);
        }
    }
}

/// `ARCHIVE.frames` for an archive, as `slim/zstd_frames.py` writes it.
fn index_text(archive: &[u8], dir: &Path) -> String {
    let path = dir.join("index-src.tar.zst");
    fs::write(&path, archive).unwrap();
    let file = File::open(&path).unwrap();
    let frames = seek_table(&file, archive.len() as u64).unwrap().unwrap();
    let mut out = format!(
        "hermes-slim-frames 1\narchive {} {}\n",
        archive.len(),
        stream::hex(&stream::sha256(archive))
    );
    let mut end = 0;
    for f in &frames {
        let bytes = &archive[f.offset as usize..(f.offset + u64::from(f.compressed)) as usize];
        out += &format!(
            "frame {} {} {}\n",
            f.compressed,
            f.decompressed,
            stream::hex(&stream::sha256(bytes))
        );
        end = f.offset + u64::from(f.compressed);
    }
    let tail = &archive[end as usize..];
    out += &format!(
        "tail {} {}\n",
        tail.len(),
        stream::hex(&stream::sha256(tail))
    );
    out
}

struct Fixture {
    scratch: Scratch,
    archive: Vec<u8>,
    index: String,
    frames: Vec<Frame>,
}

/// The test bundle in 64 KiB frames: ~90 of them, so several are always in flight.
fn fixture() -> Fixture {
    let scratch = Scratch::new();
    let archive = seekable(&bundle_tar(), 64 << 10);
    let index = index_text(&archive, &scratch.0);
    let frames = stream::parse_index(&index)
        .unwrap()
        .frames
        .into_iter()
        .map(|(f, _)| f)
        .collect();
    Fixture {
        scratch,
        archive,
        index,
        frames,
    }
}

impl Fixture {
    fn serve(&self, index: &str, faults: Faults) -> Server {
        let mut files = HashMap::new();
        files.insert(ARCHIVE.to_string(), self.archive.clone());
        files.insert(format!("{ARCHIVE}.frames"), index.as_bytes().to_vec());
        Server::start(files, faults)
    }

    fn options(&self, server: &Server, path: &str, connections: usize) -> Options {
        Options {
            archive: PathBuf::from(server.url(path)),
            dest: self.scratch.0.join("dest"),
            strip: 1,
            decoders: connections,
            writers: 8,
            stream: Some(StreamOptions {
                url: server.url(path),
                index_url: server.url(&format!("{ARCHIVE}.frames")),
                index_sha: Some(stream::sha256(self.index.as_bytes())),
                sha256: Some(stream::sha256(&self.archive)),
                patience: stream::Patience {
                    request: Duration::from_millis(800),
                    give_up: Duration::from_secs(5),
                    backoff: Duration::from_millis(20),
                },
            }),
        }
    }

    /// An offset in the middle of frame `i`.
    fn inside(&self, i: usize) -> u64 {
        self.frames[i].offset + u64::from(self.frames[i].compressed) / 2
    }
}

#[test]
fn streams_through_a_redirect_to_the_same_tree_as_a_local_unpack() {
    let fx = fixture();
    let server = fx.serve(&fx.index, Faults::default());
    let opts = fx.options(&server, &format!("/r{ARCHIVE}"), 4);
    let summary = run(&opts).unwrap();
    assert_eq!(summary.frames, fx.frames.len());
    assert_tree(&opts.dest);
    // One range request per frame plus the seek table, all at the redirect's target.
    let starts = server.starts();
    assert_eq!(starts.len(), fx.frames.len() + 1, "{starts:?}");
    for f in &fx.frames {
        assert!(
            starts.contains(&f.offset),
            "frame at {} never requested",
            f.offset
        );
    }
}

#[test]
fn files_are_written_while_later_frames_are_still_downloading() {
    let fx = fixture();
    let opts_dest = fx.scratch.0.join("dest");
    let last = fx.frames[fx.frames.len() - 1].offset;
    // The last frame is held until the first file is on disk: a downloader that unpacked
    // only after the whole archive arrived could never open this gate.
    let faults = Faults {
        gate: Some((opts_dest.join("bin/hermes"), last)),
        ..Faults::default()
    };
    let server = fx.serve(&fx.index, faults);
    run(&fx.options(&server, ARCHIVE, 4)).unwrap();
    assert_eq!(server.faults.lock().unwrap().gate_opened, Some(true));
    assert_tree(&opts_dest);
}

#[test]
fn a_dropped_connection_resumes_mid_frame() {
    let fx = fixture();
    let at = fx.inside(20);
    let server = fx.serve(
        &fx.index,
        Faults {
            drop_at: Some(at),
            ..Faults::default()
        },
    );
    let opts = fx.options(&server, ARCHIVE, 4);
    run(&opts).unwrap();
    assert_tree(&opts.dest);
    assert!(
        server.starts().contains(&at),
        "no request resumed at byte {at}"
    );
}

#[test]
fn a_stalled_transfer_times_out_and_resumes_mid_frame() {
    let fx = fixture();
    let at = fx.inside(7);
    let server = fx.serve(
        &fx.index,
        Faults {
            stall_at: Some(at),
            ..Faults::default()
        },
    );
    let opts = fx.options(&server, ARCHIVE, 4);
    run(&opts).unwrap();
    assert_tree(&opts.dest);
    assert!(
        server.starts().contains(&at),
        "no request resumed at byte {at}"
    );
}

#[test]
fn a_frame_that_arrives_corrupted_is_downloaded_again() {
    let fx = fixture();
    let i = 33;
    let faults = Faults {
        corrupt_once_at: Some(fx.inside(i)),
        ..Faults::default()
    };
    let server = fx.serve(&fx.index, faults);
    let opts = fx.options(&server, ARCHIVE, 4);
    run(&opts).unwrap();
    assert_tree(&opts.dest);
    let again = server
        .starts()
        .iter()
        .filter(|&&s| s == fx.frames[i].offset)
        .count();
    assert_eq!(again, 2, "frame {i} fetched {again} times");
}

#[test]
fn a_frame_that_never_matches_fails_without_writing_its_bytes() {
    let fx = fixture();
    let i = fx.frames.len() / 2;
    let faults = Faults {
        corrupt_always_at: Some(fx.inside(i)),
        ..Faults::default()
    };
    let server = fx.serve(&fx.index, faults);
    let opts = fx.options(&server, ARCHIVE, 2);
    let err = run(&opts).unwrap_err().to_string();
    assert!(err.contains("does not match its sha256"), "{err}");
    // Whatever was written before the failure came from verified frames: intact.
    for n in 0..40 {
        if let Ok(data) = fs::read(opts.dest.join(format!("lib/f{n}.bin"))) {
            assert_eq!(data, noise(37_000 + n * 911, n as u64), "lib/f{n}.bin");
        }
    }
}

#[test]
fn an_index_that_is_not_the_pinned_one_fetches_no_frame() {
    let fx = fixture();
    let server = fx.serve(&fx.index.replace("frame ", "frame  "), Faults::default());
    let err = run(&fx.options(&server, ARCHIVE, 4))
        .unwrap_err()
        .to_string();
    assert!(err.contains("sha256 mismatch"), "{err}");
    assert!(
        server.starts().is_empty(),
        "frames fetched: {:?}",
        server.starts()
    );
    assert!(!fx.scratch.0.join("dest").exists());
}

#[test]
fn an_index_for_another_archive_is_refused() {
    let fx = fixture();
    let server = fx.serve(&fx.index, Faults::default());
    let mut opts = fx.options(&server, ARCHIVE, 4);
    let s = opts.stream.as_mut().unwrap();
    s.index_sha = None;
    s.sha256 = Some([7u8; 32]);
    let err = run(&opts).unwrap_err().to_string();
    assert!(err.contains("describes archive"), "{err}");
    assert!(server.starts().is_empty());
}

#[test]
fn frames_that_match_an_index_with_the_wrong_archive_hash_still_fail() {
    // A self-consistent index whose archive line lies: every frame verifies, the whole does not.
    let fx = fixture();
    let real = stream::hex(&stream::sha256(&fx.archive));
    let forged = fx.index.replace(&real, &"0".repeat(64));
    let server = fx.serve(&forged, Faults::default());
    let mut opts = fx.options(&server, ARCHIVE, 4);
    let s = opts.stream.as_mut().unwrap();
    s.index_sha = Some(stream::sha256(forged.as_bytes()));
    s.sha256 = None;
    let err = run(&opts).unwrap_err().to_string();
    assert!(err.contains("does not match its sha256"), "{err}");
}

#[test]
fn a_server_that_ignores_ranges_fails_fast_so_the_installer_falls_back() {
    let fx = fixture();
    let server = fx.serve(
        &fx.index,
        Faults {
            ignore_ranges: true,
            ..Faults::default()
        },
    );
    let started = Instant::now();
    let err = run(&fx.options(&server, ARCHIVE, 4))
        .unwrap_err()
        .to_string();
    assert!(err.contains("ignores range requests"), "{err}");
    assert!(started.elapsed() < Duration::from_secs(3));
}

#[test]
fn a_busy_server_is_retried() {
    let fx = fixture();
    let server = fx.serve(
        &fx.index,
        Faults {
            unavailable: 5,
            ..Faults::default()
        },
    );
    let opts = fx.options(&server, ARCHIVE, 4);
    run(&opts).unwrap();
    assert_tree(&opts.dest);
}

#[test]
fn an_index_whose_frames_do_not_add_up_is_rejected() {
    let fx = fixture();
    let short = fx.index.replacen("frame ", "frame 1", 1);
    assert!(stream::parse_index(&short).is_err());
    assert!(stream::parse_index("hermes-slim-frames 2\n").is_err());
    assert!(stream::parse_index(&fx.index).is_ok());
}

#[test]
fn url_arguments() {
    let args = |s: &str| s.split(' ').map(String::from).collect::<Vec<_>>();
    let sha = "ab".repeat(32);
    let opts = parse(&args(&format!(
        "--url https://h/b.tar.zst out --sha256 {sha} --strip 1"
    )))
    .unwrap();
    let s = opts.stream.unwrap();
    assert_eq!(s.index_url, "https://h/b.tar.zst.frames");
    assert_eq!(s.sha256, stream::parse_hash(&sha));
    assert_eq!(opts.dest, PathBuf::from("out"));
    let err = parse(&args("--url https://h/b.tar.zst out")).unwrap_err();
    assert!(err.contains("--index-sha or --sha256"), "{err}");
    assert!(parse(&args("--url https://h/b out --sha256 xyz")).is_err());
}
