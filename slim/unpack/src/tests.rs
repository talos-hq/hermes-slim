//! Round trips through real archives: tar members built with `tar::Builder`, compressed
//! the two ways a bundle can be (independent frames plus a seek table, as
//! `slim/zstd_frames.py` writes them; or one plain zstd stream, as every release before
//! the seekable bundles did), then unpacked into a fresh directory.

use super::*;
use std::sync::atomic::AtomicU64;

static NEXT: AtomicU64 = AtomicU64::new(0);

/// A fresh directory under the system temp dir, removed when dropped.
struct Scratch(PathBuf);

impl Scratch {
    fn new() -> Self {
        let n = NEXT.fetch_add(1, Ordering::SeqCst);
        let dir = std::env::temp_dir().join(format!("hs-unpack-{}-{n}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        Scratch(dir)
    }
}

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

const MTIME: u64 = 1_700_000_000;

fn member(
    builder: &mut tar::Builder<Vec<u8>>,
    path: &str,
    kind: tar::EntryType,
    mode: u32,
    data: &[u8],
    link: Option<&str>,
) {
    let mut header = tar::Header::new_gnu();
    header.set_entry_type(kind);
    header.set_mode(mode);
    header.set_mtime(MTIME);
    header.set_size(data.len() as u64);
    if let Some(link) = link {
        builder.append_link(&mut header, path, link).unwrap();
    } else {
        builder.append_data(&mut header, path, data).unwrap();
    }
}

/// Pseudo-random bytes (compressible enough to be realistic, varied enough to catch a
/// frame handed to the parser out of order).
fn noise(len: usize, seed: u64) -> Vec<u8> {
    let mut x = seed.wrapping_mul(6_364_136_223_846_793_005).wrapping_add(1);
    (0..len)
        .map(|i| {
            x ^= x << 13;
            x ^= x >> 7;
            x ^= x << 17;
            if i % 7 == 0 {
                b'a'
            } else {
                (x & 0xff) as u8
            }
        })
        .collect()
}

fn long_name() -> String {
    format!("hermes-slim/lib/{}/module.py", "deep".repeat(30))
}

fn bundle_tar() -> Vec<u8> {
    let mut b = tar::Builder::new(Vec::new());
    member(
        &mut b,
        "hermes-slim/",
        tar::EntryType::Directory,
        0o755,
        b"",
        None,
    );
    member(
        &mut b,
        "hermes-slim/bin/",
        tar::EntryType::Directory,
        0o755,
        b"",
        None,
    );
    member(
        &mut b,
        "hermes-slim/bin/hermes",
        tar::EntryType::Regular,
        0o755,
        b"#!/bin/sh\necho hermes\n",
        None,
    );
    for i in 0..40 {
        member(
            &mut b,
            &format!("hermes-slim/lib/f{i}.bin"),
            tar::EntryType::Regular,
            0o644,
            &noise(37_000 + i * 911, i as u64),
            None,
        );
    }
    member(
        &mut b,
        "hermes-slim/lib/big.bin",
        tar::EntryType::Regular,
        0o644,
        &noise(3 << 20, 99),
        None,
    );
    member(
        &mut b,
        &long_name(),
        tar::EntryType::Regular,
        0o644,
        b"print('long path')\n",
        None,
    );
    // As Python's tarfile writes it: the exact time in PAX, a rounded one in the header.
    b.append_pax_extensions([("mtime", &b"1700000000.75"[..])])
        .unwrap();
    let mut header = tar::Header::new_ustar();
    header.set_entry_type(tar::EntryType::Regular);
    header.set_mode(0o644);
    header.set_mtime(1_700_000_001);
    header.set_size(3);
    b.append_data(&mut header, "hermes-slim/lib/source.py", &b"x=1"[..])
        .unwrap();
    member(
        &mut b,
        "hermes-slim/bin/hermes-link",
        tar::EntryType::Symlink,
        0o777,
        b"",
        Some("hermes"),
    );
    member(
        &mut b,
        "hermes-slim/bin/hermes-hard",
        tar::EntryType::Link,
        0o755,
        b"",
        Some("hermes-slim/bin/hermes"),
    );
    b.into_inner().unwrap()
}

/// Independent frames of `frame` bytes plus a seek table, byte for byte what
/// `slim/zstd_frames.py` writes.
fn seekable(data: &[u8], frame: usize) -> Vec<u8> {
    let mut out = Vec::new();
    let mut table = Vec::new();
    let mut count = 0u32;
    let mut compressor = zstd::bulk::Compressor::new(3).unwrap();
    compressor.include_checksum(true).unwrap();
    for chunk in data.chunks(frame) {
        let compressed = compressor.compress(chunk).unwrap();
        table.extend_from_slice(&(compressed.len() as u32).to_le_bytes());
        table.extend_from_slice(&(chunk.len() as u32).to_le_bytes());
        out.extend_from_slice(&compressed);
        count += 1;
    }
    out.extend_from_slice(&SKIPPABLE_MAGIC.to_le_bytes());
    out.extend_from_slice(&((table.len() + 9) as u32).to_le_bytes());
    out.extend_from_slice(&table);
    out.extend_from_slice(&count.to_le_bytes());
    out.push(0);
    out.extend_from_slice(&SEEKABLE_MAGIC.to_le_bytes());
    out
}

fn options(archive: &Path, dest: &Path) -> Options {
    Options {
        archive: archive.into(),
        dest: dest.into(),
        strip: 1,
        decoders: 4,
        writers: 8,
    }
}

fn assert_tree(dest: &Path) {
    assert_eq!(
        fs::read(dest.join("bin/hermes")).unwrap(),
        b"#!/bin/sh\necho hermes\n"
    );
    for i in 0..40 {
        assert_eq!(
            fs::read(dest.join(format!("lib/f{i}.bin"))).unwrap(),
            noise(37_000 + i * 911, i as u64),
            "lib/f{i}.bin"
        );
    }
    assert_eq!(
        fs::read(dest.join("lib/big.bin")).unwrap(),
        noise(3 << 20, 99)
    );
    let long = long_name();
    assert_eq!(
        fs::read(dest.join(long.strip_prefix("hermes-slim/").unwrap())).unwrap(),
        b"print('long path')\n"
    );
    assert_eq!(
        fs::read(dest.join("bin/hermes-link")).unwrap(),
        b"#!/bin/sh\necho hermes\n"
    );
    assert_eq!(
        fs::read(dest.join("bin/hermes-hard")).unwrap(),
        b"#!/bin/sh\necho hermes\n"
    );
    let modified = fs::metadata(dest.join("lib/f3.bin"))
        .unwrap()
        .modified()
        .unwrap();
    assert_eq!(
        modified,
        UNIX_EPOCH + Duration::from_secs(MTIME),
        "mtime kept (a .pyc is checked against it)"
    );
    let precise = fs::metadata(dest.join("lib/source.py"))
        .unwrap()
        .modified()
        .unwrap();
    assert_eq!(
        precise,
        UNIX_EPOCH + Duration::from_millis(1_700_000_000_750),
        "the PAX mtime wins over the rounded header"
    );
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        assert_eq!(
            fs::metadata(dest.join("bin/hermes"))
                .unwrap()
                .permissions()
                .mode()
                & 0o111,
            0o111,
            "executable bit kept"
        );
        assert!(fs::symlink_metadata(dest.join("bin/hermes-link"))
            .unwrap()
            .file_type()
            .is_symlink());
    }
}

#[test]
fn seekable_archive_unpacks_with_parallel_decoders() {
    let tmp = Scratch::new();
    let archive = tmp.0.join("bundle.tar.zst");
    // 256 KiB frames: the 3 MiB member alone spans a dozen of them.
    fs::write(&archive, seekable(&bundle_tar(), 256 << 10)).unwrap();
    let summary = run(&options(&archive, &tmp.0.join("out"))).unwrap();
    assert!(summary.frames > 10, "{summary:?}");
    assert_eq!(summary.files, 44);
    assert_tree(&tmp.0.join("out"));
}

#[test]
fn single_stream_archive_unpacks_too() {
    // Every release before seekable bundles: one zstd frame, no seek table.
    let tmp = Scratch::new();
    let archive = tmp.0.join("bundle.tar.zst");
    fs::write(&archive, zstd::bulk::compress(&bundle_tar(), 3).unwrap()).unwrap();
    let summary = run(&options(&archive, &tmp.0.join("out"))).unwrap();
    assert_eq!(summary.frames, 0);
    assert_tree(&tmp.0.join("out"));
}

#[test]
fn seekable_archive_is_still_plain_zstd() {
    // Any zstd decoder skips the seek table: old installers and system zstd keep working.
    let tar = bundle_tar();
    let plain = zstd::stream::decode_all(&seekable(&tar, 64 << 10)[..]).unwrap();
    assert_eq!(plain, tar);
}

#[test]
fn one_writer_and_one_decoder_give_the_same_tree() {
    let tmp = Scratch::new();
    let archive = tmp.0.join("bundle.tar.zst");
    fs::write(&archive, seekable(&bundle_tar(), 100 << 10)).unwrap();
    let opts = Options {
        decoders: 1,
        writers: 1,
        ..options(&archive, &tmp.0.join("out"))
    };
    run(&opts).unwrap();
    assert_tree(&tmp.0.join("out"));
}

#[test]
fn paths_that_climb_out_of_dest_are_refused() {
    let tmp = Scratch::new();
    let mut b = tar::Builder::new(Vec::new());
    member(
        &mut b,
        "hermes-slim/bin/hermes",
        tar::EntryType::Regular,
        0o755,
        b"ok",
        None,
    );
    // tar::Builder refuses to write "..", so set the raw name and reseal the header.
    let mut header = tar::Header::new_old();
    let name = b"hermes-slim/../../escaped";
    header.as_old_mut().name[..name.len()].copy_from_slice(name);
    header.set_entry_type(tar::EntryType::Regular);
    header.set_mode(0o644);
    header.set_size(4);
    header.set_cksum();
    b.append(&header, &b"evil"[..]).unwrap();
    let archive = tmp.0.join("bundle.tar.zst");
    fs::write(&archive, seekable(&b.into_inner().unwrap(), 256 << 10)).unwrap();
    let err = run(&options(&archive, &tmp.0.join("a").join("out"))).unwrap_err();
    assert!(err.to_string().contains("unsafe path"), "{err}");
    assert!(!tmp.0.join("escaped").exists());
}

#[test]
fn a_corrupt_frame_fails_instead_of_writing_garbage() {
    // Without per-frame checksums, damaged literals can decode "successfully" to wrong
    // bytes; zstd_frames.py writes a content checksum into every frame for this.
    let tmp = Scratch::new();
    let mut data = seekable(&bundle_tar(), 256 << 10);
    let middle = data.len() / 2;
    for b in &mut data[middle..middle + 64] {
        *b ^= 0x5a;
    }
    let archive = tmp.0.join("bundle.tar.zst");
    fs::write(&archive, data).unwrap();
    assert!(run(&options(&archive, &tmp.0.join("out"))).is_err());
}

#[test]
fn a_seek_table_that_disagrees_with_the_frames_is_rejected() {
    let tmp = Scratch::new();
    let mut data = seekable(&bundle_tar(), 256 << 10);
    // First entry's compressed size, just after the skippable frame header.
    let count =
        u32::from_le_bytes(data[data.len() - 9..data.len() - 5].try_into().unwrap()) as usize;
    let first = data.len() - 9 - count * 8;
    data[first] = data[first].wrapping_add(1);
    let archive = tmp.0.join("bundle.tar.zst");
    fs::write(&archive, data).unwrap();
    let err = run(&options(&archive, &tmp.0.join("out"))).unwrap_err();
    assert!(err.to_string().contains("seek table"), "{err}");
}

#[test]
fn member_paths_strip_and_refuse() {
    assert_eq!(
        member_path(Path::new("hermes-slim/bin/x"), 1).unwrap(),
        Some(PathBuf::from("bin/x"))
    );
    assert_eq!(member_path(Path::new("hermes-slim/"), 1).unwrap(), None);
    assert_eq!(
        member_path(Path::new("./hermes-slim/./a"), 1).unwrap(),
        Some(PathBuf::from("a"))
    );
    assert!(member_path(Path::new("hermes-slim/../etc/passwd"), 1).is_err());
    assert!(member_path(Path::new("/etc/passwd"), 0).is_err());
}

#[test]
fn pax_times_parse_to_the_nanosecond() {
    assert_eq!(
        parse_pax_time("12.5"),
        Some(UNIX_EPOCH + Duration::from_millis(12_500))
    );
    assert_eq!(
        parse_pax_time("12"),
        Some(UNIX_EPOCH + Duration::from_secs(12))
    );
    assert_eq!(
        parse_pax_time("12.1234567891"),
        Some(UNIX_EPOCH + Duration::new(12, 123_456_789))
    );
    assert_eq!(parse_pax_time("-3.5"), None);
}

#[test]
fn arguments() {
    let args: Vec<String> = ["a.tar.zst", "out", "--strip", "1", "--writers", "3"]
        .map(String::from)
        .to_vec();
    let opts = parse(&args).unwrap();
    assert_eq!((opts.strip, opts.writers), (1, 3));
    assert!(parse(&["only-one".to_string()]).is_err());
    assert!(parse(&["a".to_string(), "b".to_string(), "--bogus".to_string()]).is_err());
}
