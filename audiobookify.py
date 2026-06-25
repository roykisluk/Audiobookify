#!/usr/bin/env python3
"""Turn a folder of audiobooks into iPhone-ready .m4b files.

Run it inside an audiobook's folder (or pass DIR):
  - loose audio files in DIR -> merged into ONE .m4b named after the folder,
    one chapter per file (the common case: a book split into numbered parts)
  - each SUBFOLDER of audio files -> its own merged .m4b
Covers are pulled from free APIs (iTunes -> Google Books -> Open Library, no
key) unless the file already has embedded art. Books are processed in parallel.
Output goes to ./m4b/ ; originals are never touched.

Usage:  python3 audiobookify.py [DIR] [--jobs N] [--dry-run] [--force]
        python3 audiobookify.py --selftest
"""
import json, os, re, subprocess, sys, threading, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

AUDIO_EXTS = {".mp3", ".m4a", ".m4b", ".aac", ".flac", ".ogg", ".opus", ".wav"}
AAC_EXTS = {".m4a", ".m4b", ".aac"}            # already AAC -> copy, never re-encode
CAP_BITRATE = 128_000    # ponytail: transparent for speech; never encode above the source
OUT_DIR = "m4b"
UA = {"User-Agent": "audiobookify (https://github.com/)"}

# Set by main(): how many ffmpeg encodes may run at once, and which encoder.
_JOBS = os.cpu_count() or 4
_SEM = threading.BoundedSemaphore(_JOBS)
_ENCODER = "aac"


def _heavy(cmd):
    """Run a CPU-heavy ffmpeg call, capping concurrent encodes to _JOBS so
    parallel parts saturate the cores without oversubscribing them."""
    with _SEM:
        return run(cmd)


def _has_encoder(name):
    return subprocess.run(["ffmpeg", "-hide_banner", "-h", f"encoder={name}"],
                          capture_output=True, text=True).returncode == 0


def pick_encoder():
    for enc in ("aac_at", "libfdk_aac"):       # faster/better than native aac
        if _has_encoder(enc):
            return enc
    return "aac"


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw)


def duration_s(path):
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "default=nw=1:nk=1", str(path)]).stdout.strip()
    return float(out)


def _cap_kbps(br):
    return f"{min(br, CAP_BITRATE) // 1000}k"


def target_bitrate(path):
    """AAC bitrate to use: the source's own rate, capped at CAP_BITRATE. Never
    upscales (no point) and never exceeds the cap (waste for speech)."""
    for entries in ("stream=bit_rate", "format=bit_rate"):
        sel = ["-select_streams", "a:0"] if entries[0] == "s" else []
        out = run(["ffprobe", "-v", "error", *sel, "-show_entries", entries,
                   "-of", "default=nw=1:nk=1", str(path)]).stdout.strip()
        try:
            br = int(out.splitlines()[0])
        except (ValueError, IndexError):
            continue
        if br > 0:
            return _cap_kbps(br)
    return _cap_kbps(CAP_BITRATE)              # unknown -> the cap


def has_cover(path):
    out = run(["ffprobe", "-v", "error", "-select_streams", "v",
               "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(path)]).stdout
    return "video" in out


def clean_title(name):
    name = re.sub(r"\.[^.]+$", "", name)              # drop extension
    name = re.sub(r"\([^)]*\)", " ", name)            # (Unabridged), (2025)
    name = re.sub(r"\b(unabridged|abridged|audiobook)\b", " ", name, flags=re.I)
    return re.sub(r"\s+", " ", name).strip(" -")


def _get_json(url):
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r)
    except Exception:
        return None


def _itunes(term):
    for media in ("audiobook", "ebook", "all"):
        q = urllib.parse.urlencode({"term": term, "media": media, "limit": 1})
        data = _get_json(f"https://itunes.apple.com/search?{q}")
        for res in (data or {}).get("results", []):
            url = res.get("artworkUrl100", "")
            if url:
                yield re.sub(r"/\d+x\d+bb\.(jpg|png)", r"/600x600bb.\1", url)


def _google_books(term):
    q = urllib.parse.urlencode({"q": term, "maxResults": 1})
    data = _get_json(f"https://www.googleapis.com/books/v1/volumes?{q}")
    for item in (data or {}).get("items", []):
        links = item.get("volumeInfo", {}).get("imageLinks", {})
        url = links.get("thumbnail") or links.get("smallThumbnail")
        if url:
            yield url.replace("http://", "https://").replace("&edge=curl", "")


def _openlibrary(term):
    q = urllib.parse.urlencode({"q": term, "limit": 1})
    data = _get_json(f"https://openlibrary.org/search.json?{q}")
    for doc in (data or {}).get("docs", []):
        cid = doc.get("cover_i")
        if cid:
            yield f"https://covers.openlibrary.org/b/id/{cid}-L.jpg"


def _download(url, dest):
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=15) as r:
            data = r.read()
    except Exception:
        return False
    if len(data) < 2048:          # 1x1 placeholders / errors are tiny
        return False
    dest.write_bytes(data)
    return True


def fetch_cover(title, dest):
    """Best-effort cover download -> dest jpg. Tries 3 providers and a few
    title variants. Returns True on success."""
    terms = [title]
    if " - " in title:                                # "Author - Title" splits
        before, after = (s.strip() for s in title.split(" - ", 1))
        terms += [after, before]
    seen = set()
    for term in terms:
        if not term or term in seen:
            continue
        seen.add(term)
        for provider in (_itunes, _google_books, _openlibrary):
            for url in provider(term):
                if _download(url, dest):
                    return True
    return False


def _chapter_title(name):
    # ponytail: strip a leading track number ("01 ", "1.", "03-"); a literal
    # year like "1984 Foo" would lose it too, rare enough to ignore.
    return re.sub(r"^\s*\d+[\s._\-]*", "", clean_title(name)).strip(" -")


def chapter_metadata(files, durations):
    """ffmetadata text: one chapter per file. Titles come from filenames; if
    they carry no real names (all identical, e.g. '01 Book', '02 Book') we fall
    back to 'Chapter N' so the player's chapter list is navigable."""
    titles = [_chapter_title(f.name) for f in files]
    if len(set(titles)) <= 1:
        titles = [f"Chapter {i + 1}" for i in range(len(files))]
    lines = [";FFMETADATA1"]
    start = 0  # ms
    for title, d in zip(titles, durations):
        end = start + int(d * 1000)
        lines += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={start}",
                  f"END={end}", f"title={title}"]
        start = end
    return "\n".join(lines) + "\n"


def audio_files(folder):
    return sorted(p for p in folder.iterdir()
                  if p.is_file() and p.suffix.lower() in AUDIO_EXTS)


def _prep_part(src, work, idx):
    """Encode (or copy, if already AAC) one part to a uniform AAC temp on its
    own core. Returns (temp_path, duration_seconds)."""
    dst = work / f"part{idx:04d}.m4a"
    codec = ["-c:a", "copy"] if src.suffix.lower() in AAC_EXTS else \
            ["-c:a", _ENCODER, "-b:a", target_bitrate(src)]
    _heavy(["ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
            "-i", str(src), "-map", "0:a", "-vn", *codec, "-f", "mp4", str(dst)])
    return dst, duration_s(dst)


def build(book, out_path, work, dry):
    """book = list of audio file Paths (1 = single, >1 = merge).
    Multi-part books encode every part in parallel, then concat-copy — uses all
    cores instead of one serial encode through the concat demuxer."""
    multi = len(book) > 1
    title = clean_title(book[0].parent.name if multi else book[0].name)

    if dry:
        how = (f"encode {len(book)} parts in parallel ({_ENCODER}, source rate "
               f"capped at {_cap_kbps(CAP_BITRATE)}), then concat-copy"
               if multi else
               "copy" if book[0].suffix.lower() in AAC_EXTS else f"encode ({_ENCODER})")
        return f"would {how} -> {out_path.name}"

    cover = work / "cover.jpg"
    src_has_cover = False
    if multi:
        fetch_cover(title, cover)
    else:
        src_has_cover = has_cover(book[0])
        if not src_has_cover:
            fetch_cover(title, cover)
    cover_ok = cover.exists()

    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "warning"]

    if multi:
        with ThreadPoolExecutor(max_workers=min(_JOBS, len(book))) as pex:
            parts = list(pex.map(
                lambda iv: _prep_part(iv[1], work, iv[0]), enumerate(book)))
        temps, durs = [p for p, _ in parts], [d for _, d in parts]
        listfile = work / "list.txt"
        listfile.write_text("".join(
            f"file '{t.resolve().as_posix()}'\n" for t in temps))
        metafile = work / "meta.txt"
        metafile.write_text(chapter_metadata(book, durs))
        cmd += ["-f", "concat", "-safe", "0", "-i", str(listfile),
                "-i", str(metafile)]
        n_in, meta_idx = 2, 1
        audio_codec = ["-c:a", "copy"]         # temps are already AAC
    else:
        cmd += ["-i", str(book[0])]
        n_in, meta_idx = 1, None
        s = book[0].suffix.lower()
        audio_codec = ["-c:a", "copy"] if s in AAC_EXTS else \
                      ["-c:a", _ENCODER, "-b:a", target_bitrate(book[0])]

    cover_in = None
    if cover_ok:
        cmd += ["-i", str(cover)]
        cover_in = n_in

    cmd += ["-map", "0:a"]
    cover_codec = []
    if cover_in is not None:                   # fetched jpg
        cmd += ["-map", f"{cover_in}:v"]
        cover_codec = ["-c:v", "mjpeg"]
    elif not multi and src_has_cover:          # keep the file's own embedded art
        cmd += ["-map", "0:v"]
        cover_codec = ["-c:v", "copy"]
    cmd += ["-map_metadata", str(meta_idx) if meta_idx is not None else "0"]
    cmd += audio_codec + cover_codec
    if cover_codec:
        cmd += ["-disposition:v", "attached_pic"]
    if title:
        cmd += ["-metadata", f"title={title}", "-metadata", f"album={title}"]
    cmd += ["-f", "mp4", str(out_path)]
    _heavy(cmd)

    if multi:                                  # drop temp parts, keep the .m4b
        for t in temps:
            t.unlink(missing_ok=True)
    return None


def process_book(name, files, out_root, work_root, dry, force):
    """Returns a one-line status string (printed by the caller)."""
    safe = re.sub(r'[/:]', '-', name)
    out_path = out_root / (safe + ".m4b")
    n = len(files)
    label = f"{name}  ({n} file{'s' if n > 1 else ''})"
    if out_path.exists() and not force:
        return f"✓ skip (exists): {out_path.name}"
    work = work_root / safe
    work.mkdir(exist_ok=True)
    try:
        msg = build(files, out_path, work, dry)
        if dry:
            return f"→ {label}\n   {msg}"
        return f"✓ done: {label} -> {out_path.name}"
    except subprocess.CalledProcessError as e:
        return f"✗ FAILED: {label}\n{e.stderr[-800:]}"


def main():
    args = sys.argv[1:]
    if "--selftest" in args:
        return selftest()
    dry = "--dry-run" in args
    force = "--force" in args
    jobs = os.cpu_count() or 4
    if "--jobs" in args:
        i = args.index("--jobs")
        jobs = max(1, int(args[i + 1]))
        del args[i:i + 2]
    pos = [a for a in args if not a.startswith("-")]
    root = Path(pos[0]) if pos else Path.cwd()

    global _JOBS, _SEM, _ENCODER
    _JOBS = jobs
    _SEM = threading.BoundedSemaphore(jobs)
    _ENCODER = pick_encoder()

    out_root = root / OUT_DIR
    out_root.mkdir(exist_ok=True)
    work_root = out_root / ".work"
    work_root.mkdir(exist_ok=True)

    books = []  # (name, [files])
    loose = []  # loose audio files in DIR = parts of one book (this folder)
    for item in sorted(root.iterdir()):
        if item.resolve() == out_root.resolve():
            continue
        if item.is_dir():
            files = audio_files(item)
            if files:
                books.append((item.name, files))
        elif item.is_file() and item.suffix.lower() in AUDIO_EXTS:
            loose.append(item)
    if loose:
        books.append((root.name, sorted(loose)))

    if not books:
        print(f"No audiobooks found in {root}")
        return

    workers = 1 if dry else min(jobs, len(books))
    print(f"Audiobookify: {len(books)} book(s), {jobs} core(s), encoder={_ENCODER}\n")

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(process_book, name, files, out_root, work_root,
                          dry, force): name for name, files in books}
        for fut in as_completed(futs):
            print(fut.result(), flush=True)


def selftest():
    assert clean_title("Buddha's Brain (Unabridged).mp3") == "Buddha's Brain"
    assert clean_title("Alex Hutchinson - The Explorers Gene (2025).m4b") == \
        "Alex Hutchinson - The Explorers Gene"
    m = chapter_metadata([Path("01 Intro.mp3"), Path("02 Next.mp3")], [1.5, 2.0])
    assert "START=0" in m and "END=1500" in m and "START=1500" in m and "END=3500" in m
    assert "title=Intro" in m and "title=Next" in m       # leading number stripped
    # identical part names -> numbered chapters, not 10x the same title
    d = chapter_metadata([Path("01 The Book.mp3"), Path("02 The Book.mp3")], [1, 1])
    assert "title=Chapter 1" in d and "title=Chapter 2" in d
    # bitrate: cap high sources at 128k, keep lower sources as-is
    assert _cap_kbps(320000) == "128k" and _cap_kbps(96000) == "96k" \
        and _cap_kbps(64000) == "64k"
    print("selftest ok")


if __name__ == "__main__":
    main()
