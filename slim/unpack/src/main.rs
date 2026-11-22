//! hermes-slim-unpack: unpack a hermes-slim `.tar.zst` bundle using every core.
//!
//!     hermes-slim-unpack ARCHIVE DEST [--strip N] [--decoders N] [--writers N]
//!     hermes-slim-unpack --url URL DEST (--index-sha SHA256 | --sha256 SHA256)
//!                        [--index-url URL] [--connections N] [--strip N] [--writers N]
//!
//! `zstd -d | tar -x` does all its work on two threads: one zstd frame is decoded in order,
//! and tar creates, writes and closes one file at a time. On Windows the close is the
//! expensive part: Defender scans each new file when its handle closes, so a bundle of
//! ~27,000 files spends most of its unpack time waiting on scans in series.
//!
//! This helper splits the work three ways:
//!
//! * **Decoders.** Bundles are written as independent zstd frames followed by a seek table
//!   (zstd's seekable format; `slim/zstd_frames.py`), so frames decode on every core at
//!   once and are handed to the tar parser in order. Any zstd decoder still reads the file
//!   (the seek table is a skippable frame); an archive without one decodes as one stream.
//! * **Parser.** One thread walks the tar stream, creates directories, and hands each
//!   regular file to a writer.
//! * **Writers.** A pool of threads each create, write, timestamp and close their files, so
//!   the per-file scan on close runs in parallel instead of in series.
//!
//! With `--url` the archive is never stored: frames are downloaded in order over several
//! connections, each checked against the release's frame index once it is complete, then
//! decoded and written while later frames are still downloading (see `stream`).
//!
//! Symlinks and hard links are made after every file is written, so no write can follow a
//! link out of DEST; paths that are absolute or climb with `..` are refused. File modes and
//! modification times are kept (Python compares a source file's mtime with its `.pyc`).

use std::collections::BTreeMap;
use std::fs::{self, File, OpenOptions};
use std::io::{self, BufReader, Read, Write};
use std::path::{Component, Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::mpsc::{self, Receiver};
use std::sync::{Arc, Condvar, Mutex};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

mod stream;
use sha2::{Digest, Sha256};

/// zstd seekable format: the seek table is a skippable frame with this magic...
const SKIPPABLE_MAGIC: u32 = 0x184D_2A5E;
/// ...and ends with this one.
const SEEKABLE_MAGIC: u32 = 0x8F92_EAB1;
/// Footer: frame count (u32), descriptor (u8), seekable magic (u32).
const FOOTER: u64 = 9;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct Frame {
    offset: u64,
    compressed: u32,
    decompressed: u32,
}

#[derive(Debug, Clone)]
struct Options {
    archive: PathBuf,
    dest: PathBuf,
    strip: usize,
    /// Frame workers: decoders for a local archive, connections (each also decoding the
    /// frame it fetched) for `--url`.
    decoders: usize,
    writers: usize,
    stream: Option<StreamOptions>,
}

#[derive(Debug, Clone)]
struct StreamOptions {
    url: String,
    index_url: String,
    /// The index's own sha256 (the release manifest pins it)...
    index_sha: Option<stream::Hash>,
    /// ...and/or the archive's, which the index must name.
    sha256: Option<stream::Hash>,
    patience: stream::Patience,
}

#[derive(Debug, Default, Clone, Copy, PartialEq, Eq)]
struct Summary {
    files: u64,
    bytes: u64,
    frames: usize,
}

fn invalid(message: String) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

#[cfg(unix)]
fn read_at(file: &File, offset: u64, buf: &mut [u8]) -> io::Result<()> {
    std::os::unix::fs::FileExt::read_exact_at(file, buf, offset)
}

#[cfg(windows)]
fn read_at(file: &File, mut offset: u64, mut buf: &mut [u8]) -> io::Result<()> {
    use std::os::windows::fs::FileExt;
    while !buf.is_empty() {
        let n = file.seek_read(buf, offset)?;
        if n == 0 {
            return Err(io::ErrorKind::UnexpectedEof.into());
        }
        buf = &mut buf[n..];
        offset += n as u64;
    }
    Ok(())
}

fn le32(bytes: &[u8]) -> u32 {
    u32::from_le_bytes([bytes[0], bytes[1], bytes[2], bytes[3]])
}

/// The frames listed in the archive's seek table, or `None` when it has none.
fn seek_table(file: &File, len: u64) -> io::Result<Option<Vec<Frame>>> {
    if len < FOOTER {
        return Ok(None);
    }
    let mut footer = [0u8; FOOTER as usize];
    read_at(file, len - FOOTER, &mut footer)?;
    if le32(&footer[5..9]) != SEEKABLE_MAGIC {
        return Ok(None);
    }
    let count = u64::from(le32(&footer[0..4]));
    let entry: u64 = if footer[4] & 0x80 != 0 { 12 } else { 8 };
    let payload = count * entry + FOOTER;
    let table_start = len
        .checked_sub(payload + 8)
        .ok_or_else(|| invalid("seek table is larger than the archive".into()))?;
    let mut table = vec![0u8; (payload + 8) as usize];
    read_at(file, table_start, &mut table)?;
    if le32(&table[0..4]) != SKIPPABLE_MAGIC || u64::from(le32(&table[4..8])) != payload {
        return Err(invalid("malformed seek table".into()));
    }
    let mut frames = Vec::with_capacity(count as usize);
    let mut offset = 0u64;
    for row in table[8..8 + (count * entry) as usize].chunks_exact(entry as usize) {
        let frame = Frame {
            offset,
            compressed: le32(&row[0..4]),
            decompressed: le32(&row[4..8]),
        };
        offset += u64::from(frame.compressed);
        frames.push(frame);
    }
    if offset != table_start {
        return Err(invalid(format!(
            "seek table covers {offset} bytes, frames end at {table_start}"
        )));
    }
    Ok(Some(frames))
}

/// Where a worker gets frame `i`'s compressed bytes.
enum Source {
    File(File),
    /// Downloaded and verified against the index (`stream::fetch_frame`).
    Net {
        http: Box<stream::Http>,
        index: stream::Index,
    },
}

impl Source {
    fn load(&self, i: usize, frame: Frame) -> io::Result<Vec<u8>> {
        match self {
            Source::File(file) => {
                let mut compressed = vec![0u8; frame.compressed as usize];
                read_at(file, frame.offset, &mut compressed)?;
                Ok(compressed)
            }
            Source::Net { http, index } => stream::fetch_frame(http, index, i),
        }
    }
}

/// A decoded frame, with its compressed bytes when the reader hashes the archive.
type Decoded = (Vec<u8>, Option<Vec<u8>>);

/// Decoded frames handed to the reader in archive order.
struct Shared {
    ready: BTreeMap<usize, io::Result<Decoded>>,
    /// Index of the next frame the reader will take.
    next: usize,
}

/// A `Read` over the decompressed archive, fed by `decoders` threads that each claim the
/// next undecoded frame. At most `window` frames are decoded ahead of the reader, which
/// bounds memory at about `window` x frame size.
struct ParallelFrames {
    shared: Arc<(Mutex<Shared>, Condvar)>,
    /// The compressed archive, hashed in order as the reader takes each frame.
    hasher: Option<Sha256>,
    /// Streaming: (compressed bytes taken, archive size, next 10% step to report).
    progress: Option<(u64, u64, u64)>,
    current: Vec<u8>,
    pos: usize,
    index: usize,
    total: usize,
    claim: Arc<AtomicUsize>,
    workers: Vec<thread::JoinHandle<()>>,
}

impl ParallelFrames {
    fn start(source: Arc<Source>, frames: Vec<Frame>, decoders: usize, hash: bool) -> Self {
        let total = frames.len();
        let window = decoders * 2;
        let shared = Arc::new((
            Mutex::new(Shared {
                ready: BTreeMap::new(),
                next: 0,
            }),
            Condvar::new(),
        ));
        let frames = Arc::new(frames);
        let claim = Arc::new(AtomicUsize::new(0));
        let workers = (0..decoders.max(1))
            .map(|_| {
                let (shared, source, frames, claim) = (
                    shared.clone(),
                    source.clone(),
                    frames.clone(),
                    claim.clone(),
                );
                thread::spawn(move || loop {
                    let i = claim.fetch_add(1, Ordering::SeqCst);
                    if i >= frames.len() {
                        return;
                    }
                    {
                        let (lock, cvar) = &*shared;
                        let mut state = lock.lock().unwrap();
                        while i >= state.next + window {
                            state = cvar.wait(state).unwrap();
                        }
                    }
                    let frame = frames[i];
                    let result = (|| {
                        // Whole and verified before the decoder sees a byte of it.
                        let compressed = source.load(i, frame)?;
                        let data =
                            zstd::bulk::decompress(&compressed, frame.decompressed as usize)?;
                        if data.len() != frame.decompressed as usize {
                            return Err(invalid(format!(
                                "frame {i} decoded to {} bytes, not {}",
                                data.len(),
                                frame.decompressed
                            )));
                        }
                        Ok((data, hash.then_some(compressed)))
                    })();
                    let (lock, cvar) = &*shared;
                    lock.lock().unwrap().ready.insert(i, result);
                    cvar.notify_all();
                })
            })
            .collect();
        ParallelFrames {
            shared,
            hasher: hash.then(Sha256::new),
            progress: None,
            current: Vec::new(),
            pos: 0,
            index: 0,
            total,
            claim,
            workers,
        }
    }
}

impl Read for ParallelFrames {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        while self.pos == self.current.len() {
            if self.index == self.total {
                return Ok(0);
            }
            let (lock, cvar) = &*self.shared;
            let mut state = lock.lock().unwrap();
            let data = loop {
                if let Some(data) = state.ready.remove(&self.index) {
                    break data;
                }
                state = cvar.wait(state).unwrap();
            };
            self.index += 1;
            state.next = self.index;
            cvar.notify_all();
            drop(state);
            let (data, compressed) = data?;
            if let (Some(hasher), Some(compressed)) = (&mut self.hasher, compressed) {
                hasher.update(&compressed);
                if let Some((done, total, step)) = &mut self.progress {
                    *done += compressed.len() as u64;
                    if *done * 10 >= *step * *total {
                        eprintln!(
                            "   {:3}% ({} of {} MiB)",
                            *done * 100 / *total,
                            *done >> 20,
                            *total >> 20
                        );
                        *step = *done * 10 / *total + 1;
                    }
                }
            }
            self.current = data;
            self.pos = 0;
        }
        let n = buf.len().min(self.current.len() - self.pos);
        buf[..n].copy_from_slice(&self.current[self.pos..self.pos + n]);
        self.pos += n;
        Ok(n)
    }
}

impl ParallelFrames {
    /// The sha256 of every frame read so far followed by `tail`.
    fn digest(&mut self, tail: &[u8]) -> stream::Hash {
        let mut hasher = self.hasher.take().unwrap_or_default();
        hasher.update(tail);
        hasher.finalize().into()
    }
}

impl Drop for ParallelFrames {
    fn drop(&mut self) {
        // Stop handing out frames, release decoders waiting for window space, and join them.
        self.claim.store(usize::MAX / 2, Ordering::SeqCst);
        {
            let (lock, cvar) = &*self.shared;
            lock.lock().unwrap().next = usize::MAX / 2;
            cvar.notify_all();
        }
        for worker in self.workers.drain(..) {
            let _ = worker.join();
        }
    }
}

/// The path inside DEST for an archive member: the first `strip` components dropped, `.`
/// ignored. `None` for a member that strips away entirely (the top-level directory).
fn member_path(path: &Path, strip: usize) -> io::Result<Option<PathBuf>> {
    let mut out = PathBuf::new();
    let mut skipped = 0;
    for component in path.components() {
        match component {
            Component::Normal(_) if skipped < strip => skipped += 1,
            Component::Normal(part) => out.push(part),
            Component::CurDir => {}
            _ => {
                return Err(invalid(format!(
                    "refusing unsafe path in archive: {}",
                    path.display()
                )))
            }
        }
    }
    Ok((!out.as_os_str().is_empty()).then_some(out))
}

struct Job {
    path: PathBuf,
    data: Vec<u8>,
    mode: u32,
    mtime: SystemTime,
}

/// A PAX `mtime` record ("1795309675.6123"), to the nanosecond.
fn parse_pax_time(value: &str) -> Option<SystemTime> {
    let (secs, frac) = value.split_once('.').unwrap_or((value, ""));
    let secs: u64 = secs.parse().ok()?;
    let digits: String = frac.chars().take(9).collect();
    let nanos: u32 = if digits.is_empty() {
        0
    } else {
        format!("{digits:0<9}").parse().ok()?
    };
    Some(UNIX_EPOCH + Duration::new(secs, nanos))
}

/// A member's modification time: its PAX record when present (Python's tarfile writes the
/// exact time there and a rounded one in the header, which would put a source file a second
/// after its .pyc and make Python recompile it), else the header's whole seconds.
fn member_mtime<R: Read>(entry: &mut tar::Entry<R>) -> SystemTime {
    if let Ok(Some(extensions)) = entry.pax_extensions() {
        for ext in extensions.flatten() {
            if ext.key() == Ok("mtime") {
                if let Some(t) = ext.value().ok().and_then(parse_pax_time) {
                    return t;
                }
            }
        }
    }
    UNIX_EPOCH + Duration::from_secs(entry.header().mtime().unwrap_or(0))
}

fn create(path: &Path, mode: u32) -> io::Result<File> {
    let mut options = OpenOptions::new();
    options.write(true).create(true).truncate(true);
    #[cfg(unix)]
    std::os::unix::fs::OpenOptionsExt::mode(&mut options, mode & 0o7777);
    #[cfg(not(unix))]
    let _ = mode;
    match options.open(path) {
        Err(e) if e.kind() == io::ErrorKind::NotFound => {
            // Archives list a directory before its files, but nothing requires it.
            if let Some(parent) = path.parent() {
                fs::create_dir_all(parent)?;
            }
            options.open(path)
        }
        other => other,
    }
}

/// Create, write, timestamp and close one file. The close is where Windows Defender scans
/// it, which is why this runs on many threads at once.
fn write_file(job: &Job) -> io::Result<()> {
    let mut file = create(&job.path, job.mode)?;
    file.write_all(&job.data)?;
    file.set_modified(job.mtime)?;
    Ok(())
}

fn writer_pool(
    count: usize,
    jobs: Receiver<Job>,
) -> (Vec<thread::JoinHandle<()>>, Arc<Mutex<Option<io::Error>>>) {
    let jobs = Arc::new(Mutex::new(jobs));
    let error = Arc::new(Mutex::new(None));
    let threads = (0..count.max(1))
        .map(|_| {
            let (jobs, error) = (jobs.clone(), error.clone());
            thread::spawn(move || loop {
                let job = jobs.lock().unwrap().recv();
                let Ok(job) = job else { return };
                if let Err(e) = write_file(&job) {
                    let mut first = error.lock().unwrap();
                    if first.is_none() {
                        *first = Some(io::Error::new(
                            e.kind(),
                            format!("{}: {e}", job.path.display()),
                        ));
                    }
                }
            })
        })
        .collect();
    (threads, error)
}

enum Link {
    Symbolic { path: PathBuf, target: PathBuf },
    Hard { path: PathBuf, target: PathBuf },
}

fn make_link(link: &Link) -> io::Result<()> {
    match link {
        Link::Hard { path, target } => {
            fs::hard_link(target, path).or_else(|_| fs::copy(target, path).map(|_| ()))
        }
        #[cfg(unix)]
        Link::Symbolic { path, target } => std::os::unix::fs::symlink(target, path),
        #[cfg(windows)]
        Link::Symbolic { path, target } => {
            // Creating a symlink needs Developer Mode or elevation on Windows; a copy of
            // what it points at serves the same purpose for an installed tree.
            let resolved = path
                .parent()
                .map_or_else(|| target.clone(), |p| p.join(target));
            std::os::windows::fs::symlink_file(target, path)
                .or_else(|_| fs::copy(resolved, path).map(|_| ()))
        }
    }
}

fn unpack<R: Read>(stream: R, opts: &Options) -> io::Result<Summary> {
    fs::create_dir_all(&opts.dest)?;
    let (send, receive) = mpsc::sync_channel::<Job>(opts.writers * 4);
    let (writers, write_error) = writer_pool(opts.writers, receive);
    let mut summary = Summary::default();
    let mut links = Vec::new();
    let mut dirs = Vec::new();
    let parsed = (|| -> io::Result<()> {
        let mut archive = tar::Archive::new(stream);
        for entry in archive.entries()? {
            let mut entry = entry?;
            let Some(rel) = member_path(&entry.path()?, opts.strip)? else {
                continue;
            };
            let path = opts.dest.join(&rel);
            let mtime = member_mtime(&mut entry);
            let header = entry.header();
            let mode = header.mode().unwrap_or(0o644);
            match header.entry_type() {
                tar::EntryType::Directory => {
                    fs::create_dir_all(&path)?;
                    dirs.push((path, mode));
                }
                tar::EntryType::Regular | tar::EntryType::Continuous => {
                    let mut data = Vec::with_capacity(entry.size() as usize);
                    entry.read_to_end(&mut data)?;
                    summary.files += 1;
                    summary.bytes += data.len() as u64;
                    if send
                        .send(Job {
                            path,
                            data,
                            mode,
                            mtime,
                        })
                        .is_err()
                    {
                        return Err(io::Error::other("file writers stopped"));
                    }
                }
                tar::EntryType::Symlink => {
                    let target = entry.link_name()?.ok_or_else(|| {
                        invalid(format!("symlink {} has no target", rel.display()))
                    })?;
                    links.push(Link::Symbolic {
                        path,
                        target: target.into_owned(),
                    });
                }
                tar::EntryType::Link => {
                    let target = entry.link_name()?.ok_or_else(|| {
                        invalid(format!("hard link {} has no target", rel.display()))
                    })?;
                    let Some(target) = member_path(&target, opts.strip)? else {
                        return Err(invalid(format!(
                            "hard link {} points outside the bundle",
                            rel.display()
                        )));
                    };
                    links.push(Link::Hard {
                        path,
                        target: opts.dest.join(target),
                    });
                }
                // PAX and GNU long-name records are folded into the next entry by `tar`.
                _ => {}
            }
        }
        Ok(())
    })();
    drop(send);
    for writer in writers {
        let _ = writer.join();
    }
    parsed?;
    if let Some(e) = write_error.lock().unwrap().take() {
        return Err(e);
    }
    for link in &links {
        make_link(link)?;
    }
    #[cfg(unix)]
    for (path, mode) in dirs.iter().rev() {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(path, fs::Permissions::from_mode(mode & 0o7777))?;
    }
    #[cfg(not(unix))]
    let _ = dirs;
    Ok(summary)
}

/// Download, verify and unpack in one pass (`--url`).
fn run_stream(opts: &Options, s: &StreamOptions) -> io::Result<Summary> {
    let http = stream::Http::new(&s.url, s.patience);
    let text = http.get_small(&s.index_url, 4 << 20)?;
    if let Some(pin) = s.index_sha {
        if stream::sha256(&text) != pin {
            return Err(invalid(format!("{} sha256 mismatch", s.index_url)));
        }
    }
    let index = stream::parse_index(
        std::str::from_utf8(&text).map_err(|_| invalid("frame index is not text".into()))?,
    )?;
    if let Some(sha) = s.sha256 {
        if index.sha256 != sha {
            return Err(invalid(format!(
                "the frame index describes archive {}, not {}",
                stream::hex(&index.sha256),
                stream::hex(&sha)
            )));
        }
    }
    let frames: Vec<Frame> = index.frames.iter().map(|(f, _)| *f).collect();
    let (count, want, want_size) = (frames.len(), index.sha256, index.size);
    let source = Arc::new(Source::Net {
        http: Box::new(http),
        index,
    });
    let mut parallel = ParallelFrames::start(source.clone(), frames, opts.decoders, true);
    parallel.progress = Some((0, want_size, 1));
    let summary = unpack(&mut parallel, opts)?;
    // tar stops at its end-of-archive marker; the frames after it still count.
    io::copy(&mut parallel, &mut io::sink())?;
    // The seek table comes last in the archive and is fetched last: frames start at once.
    let Source::Net { http, index } = &*source else {
        unreachable!()
    };
    let tail = stream::fetch_tail(http, index)?;
    if parallel.digest(&tail) != want {
        return Err(invalid(
            "the downloaded archive does not match its sha256".into(),
        ));
    }
    Ok(Summary {
        frames: count,
        ..summary
    })
}

fn run(opts: &Options) -> io::Result<Summary> {
    if let Some(s) = &opts.stream {
        return run_stream(opts, s);
    }
    let file = File::open(&opts.archive)?;
    let len = file.metadata()?.len();
    match seek_table(&file, len)? {
        Some(frames) => {
            let count = frames.len();
            let source = Arc::new(Source::File(file));
            let summary = unpack(
                ParallelFrames::start(source, frames, opts.decoders, false),
                opts,
            )?;
            Ok(Summary {
                frames: count,
                ..summary
            })
        }
        // One zstd stream: decoded in order on this thread, files still written in parallel.
        None => unpack(
            zstd::stream::read::Decoder::new(BufReader::new(file))?,
            opts,
        ),
    }
}

const USAGE: &str =
    "usage: hermes-slim-unpack ARCHIVE DEST [--strip N] [--decoders N] [--writers N]
       hermes-slim-unpack --url URL DEST (--index-sha SHA256 | --sha256 SHA256)
                          [--index-url URL] [--connections N] [--strip N] [--writers N]";

fn parse(args: &[String]) -> Result<Options, String> {
    let cores = thread::available_parallelism().map_or(4, |n| n.get());
    let mut positional = Vec::new();
    // Writers mostly wait on the disk and on Defender, not the CPU: more than one per core.
    let (mut strip, mut decoders, mut writers) = (0usize, None, (cores * 4).clamp(8, 64));
    let (mut url, mut index_url, mut index_sha, mut sha256) = (None, None, None, None);
    let mut it = args.iter();
    while let Some(arg) = it.next() {
        let mut value = |name: &str| -> Result<String, String> {
            it.next()
                .cloned()
                .ok_or_else(|| format!("{name} needs a value\n{USAGE}"))
        };
        let number = |name: &str, v: String| -> Result<usize, String> {
            v.parse()
                .map_err(|_| format!("{name} needs a number\n{USAGE}"))
        };
        let hash = |name: &str, v: String| -> Result<stream::Hash, String> {
            stream::parse_hash(&v.to_ascii_lowercase())
                .ok_or_else(|| format!("{name} needs a sha256 (64 hex digits)"))
        };
        match arg.as_str() {
            "--strip" => strip = number(arg, value(arg)?)?,
            "--decoders" | "--connections" => decoders = Some(number(arg, value(arg)?)?.max(1)),
            "--writers" => writers = number(arg, value(arg)?)?.max(1),
            "--url" => url = Some(value(arg)?),
            "--index-url" => index_url = Some(value(arg)?),
            "--index-sha" => index_sha = Some(hash(arg, value(arg)?)?),
            "--sha256" => sha256 = Some(hash(arg, value(arg)?)?),
            "-h" | "--help" => return Err(USAGE.to_string()),
            flag if flag.starts_with("--") => {
                return Err(format!("unknown option {flag}\n{USAGE}"))
            }
            _ => positional.push(PathBuf::from(arg)),
        }
    }
    let Some(url) = url else {
        let [archive, dest] =
            <[PathBuf; 2]>::try_from(positional).map_err(|_| USAGE.to_string())?;
        return Ok(Options {
            archive,
            dest,
            strip,
            decoders: decoders.unwrap_or(cores),
            writers,
            stream: None,
        });
    };
    let [dest] = <[PathBuf; 1]>::try_from(positional).map_err(|_| USAGE.to_string())?;
    if index_sha.is_none() && sha256.is_none() {
        // Frames are written as they arrive: something must pin what they should be.
        return Err(format!("--url needs --index-sha or --sha256\n{USAGE}"));
    }
    Ok(Options {
        archive: PathBuf::from(&url),
        dest,
        strip,
        // Each connection also decodes the frame it fetched; more than ~8 rarely helps a
        // single server and costs a frame of memory each.
        decoders: decoders.unwrap_or(cores.clamp(4, 8)),
        writers,
        stream: Some(StreamOptions {
            index_url: index_url.unwrap_or_else(|| format!("{url}.frames")),
            url,
            index_sha,
            sha256,
            patience: stream::Patience::default(),
        }),
    })
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.first().map(String::as_str) == Some("--version") {
        println!("hermes-slim-unpack {}", env!("CARGO_PKG_VERSION"));
        return;
    }
    let opts = match parse(&args) {
        Ok(opts) => opts,
        Err(message) => {
            eprintln!("{message}");
            std::process::exit(2);
        }
    };
    let started = Instant::now();
    match run(&opts) {
        Ok(s) => eprintln!(
            "unpacked {} files ({} MiB) in {:.1}s: {} frames, {} {}, {} writers",
            s.files,
            s.bytes >> 20,
            started.elapsed().as_secs_f64(),
            s.frames.max(1),
            if s.frames > 0 { opts.decoders } else { 1 },
            if opts.stream.is_some() {
                "connections (streamed)"
            } else {
                "decoders"
            },
            opts.writers
        ),
        Err(e) => {
            eprintln!("hermes-slim-unpack: {}: {e}", opts.archive.display());
            std::process::exit(1);
        }
    }
}

#[cfg(test)]
mod stream_tests;
#[cfg(test)]
mod tests;
