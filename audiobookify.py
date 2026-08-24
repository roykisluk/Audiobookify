#!/usr/bin/env python3
"""Turn a folder of audiobooks into iPhone-ready .m4b files.

Run it inside an audiobook's folder (or pass DIR, or a single audio FILE):
  - loose audio files in DIR -> merged into ONE .m4b named after the folder,
    one chapter per file (the common case: a book split into numbered parts)
  - each SUBFOLDER of audio files -> its own merged .m4b
Covers are pulled from free APIs (iTunes -> Google Books -> Open Library, no
key) unless the file already has embedded art -- or you drop a cover.jpg/.png
(or <bookname>.jpg/.png) in the folder, which wins. Books run in parallel.
Output lands next to the input; originals are never touched (a name clash gets
a " (m4b)" suffix rather than overwriting).

Usage:  python3 audiobookify.py [DIR|FILE] [--jobs N] [--dry-run] [--force]
        python3 audiobookify.py --selftest
"""
import json, os, re, shutil, subprocess, sys, tempfile, threading, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

AUDIO_EXTS = {".mp3", ".m4a", ".m4b", ".aac", ".flac", ".ogg", ".opus", ".wav"}
AAC_EXTS = {".m4a", ".m4b", ".aac"}            # already AAC -> copy, never re-encode
CAP_BITRATE = 128_000    # ponytail: transparent for speech; never encode above the source
UA = {"User-Agent": "audiobookify (https://github.com/)"}

# Set by main(): how many ffmpeg encodes may run at once, and which encoder.
_JOBS = os.cpu_count() or 4
_SEM = threading.BoundedSemaphore(_JOBS)
_ENCODER = "aac"


def _hms(t):
    """'HH:MM:SS.micro' -> seconds; None for 'N/A'/garbage."""
    try:
        h, m, s = t.split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)
    except ValueError:
        return None


_PROGRESS_KEYS = ("frame=", "fps=", "stream_", "bitrate=", "total_size=",
                  "out_time", "dup_frames=", "drop_frames=", "speed=", "progress=")


def _run_progress(cmd, total_s, label):
    """Like run(), but streams ffmpeg's -progress and prints percent as it
    encodes. total_s is the input duration; without it we can't compute a
    percent so we fall back to a plain (silent) run."""
    if not total_s:
        return run(cmd)
    full = [cmd[0], "-progress", "pipe:1", "-nostats", *cmd[1:]]
    # stderr merged into stdout so a chatty encode can't fill an unread pipe
    proc = subprocess.Popen(full, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    tail, last = [], -10
    for line in proc.stdout:                        # progress lines + warnings
        if line.startswith("out_time="):
            secs = _hms(line.strip().split("=", 1)[1])
            if secs is not None:
                pct = min(99, int(secs / total_s * 100))
                if pct >= last + 10:                # throttle: every ~10%
                    last = pct
                    print(f"   … {label}: {pct}%", flush=True)
        elif not line.startswith(_PROGRESS_KEYS):
            tail.append(line)
            del tail[:-20]                          # keep only the last warnings
    if proc.wait() != 0:
        raise subprocess.CalledProcessError(proc.returncode, cmd,
                                            stderr="".join(tail))


def _heavy(cmd, total_s=None, label=None):
    """Run a CPU-heavy ffmpeg call, capping concurrent encodes to _JOBS so
    parallel parts saturate the cores without oversubscribing them. With a
    label, streams live percent (see _run_progress)."""
    with _SEM:
        return _run_progress(cmd, total_s, label) if label else run(cmd)


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


def local_cover(book):
    """A cover.<ext> or <bookname>.<ext> (jpg/png) next to the audio overrides
    web lookup and embedded art — drop one in the folder when auto-pick is wrong."""
    folder = book[0].parent
    stem = folder.name if len(book) > 1 else book[0].stem
    for name in (stem, "cover"):
        for ext in (".jpg", ".jpeg", ".png"):
            p = folder / f"{name}{ext}"
            if p.exists():
                return p
    return None


def clean_title(name):
    name = re.sub(r"\.[^.]+$", "", name)              # drop extension
    name = re.sub(r"\([^)]*\)", " ", name)            # (Unabridged), (2025)
    name = re.sub(r"\b(unabridged|abridged|audiobook)\b", " ", name, flags=re.I)
    return re.sub(r"\s+", " ", name).strip(" -")


EDITION_RE = re.compile(
    r"\b((?:\d+(?:st|nd|rd|th)|first|second|third|fourth|fifth|sixth|revised|"
    r"expanded|updated|anniversary)[\w\s]*?edition)\b", re.I)
ABRIDGE_RE = re.compile(r"\b(unabridged|abridged)\b", re.I)


def tags_of(path):
    out = run(["ffprobe", "-v", "error", "-show_entries", "format_tags",
               "-of", "json", str(path)]).stdout
    try:
        t = json.loads(out).get("format", {}).get("tags", {})
    except json.JSONDecodeError:
        return {}
    return {k.lower(): v for k, v in t.items()}


def std_name(raw, blob, author):
    """The pure half of standard_name(). Returns (filename, title): the
    filename is '<Title> - <Author> (<Edition>, <Unabridged>)', the title is
    the bare book title — they stay separate because the tags get the title
    and the author in their own fields, not the display name.
    `raw` is the best title string, `blob` everything we know about the book
    (names + tags) — edition and abridgement are hunted there because they
    often sit in the folder name and nowhere else."""
    if " " not in raw:                          # "DumbingUsDownUnabridged"
        raw = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", raw)
    blob = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", blob)   # so \b can see the words
    ed = EDITION_RE.search(blob)
    ab = ABRIDGE_RE.search(blob)
    title = EDITION_RE.sub("", raw)
    title = re.sub(r"\([^)]*\)", " ", title)     # (Unabridged), (Pack #03)
    title = ABRIDGE_RE.sub(" ", title)
    title = re.split(r":|\s+-\s+", title)[0]     # drop the subtitle
    title = re.sub(r"\s+", " ", title).strip(" -,")
    extra = [" ".join(w.capitalize() if w.isalpha() and w.islower() else w
                      for w in x.split())
             for x in (ed.group(1) if ed else "", ab.group(1) if ab else "") if x]
    name = f"{title} - {author}" if author else title
    return name + (f" ({', '.join(extra)})" if extra else ""), title


def standard_name(book):
    """(filename, title, author) from the source's own tags, with the author
    looked up online when the file carries none."""
    tags = tags_of(book[0])
    src = book[0].parent.name if len(book) > 1 else book[0].stem
    raw = tags.get("album") or src
    blob = " ".join((src, raw, tags.get("title", "")))
    author = tags.get("artist") or tags.get("album_artist") or ""
    author = author.split("/")[0]               # narrator appended after a slash
    author = re.sub(r",?\s*\b(ph\.?d|m\.?d|m\.?a)\.?\b", "", author, flags=re.I).strip()
    if not author:
        author = itunes_author(std_name(raw, blob, "")[1]) or ""
    return std_name(raw, blob, author) + (author,)


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


def itunes_author(title):
    """Author name for a title with no artist tag. Same API the covers use."""
    q = urllib.parse.urlencode({"term": title, "media": "audiobook", "limit": 1})
    for res in (_get_json(f"https://itunes.apple.com/search?{q}") or {}).get("results", []):
        return res.get("artistName")
    return None


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


def has_chapters(path):
    out = run(["ffprobe", "-v", "error", "-show_chapters",
               "-of", "csv=p=0", str(path)]).stdout
    return bool(out.strip())


def cue_chapters(cue, total_s):
    """ffmetadata from a .cue sidecar next to a single-file book. INDEX times
    are MIN:SEC:SUB with minutes free to exceed 59. SUB is nominally 1/75 s
    frames, but plenty of audiobook cues write centiseconds — a value above 74
    settles it, and guessing wrong pushes the last mark past EOF, which ffmpeg
    rejects outright. Returns None if the cue carries no usable track index."""
    raw, title = [], None
    for line in cue.read_text(errors="replace").splitlines():
        line = line.strip()
        if line.startswith("TRACK "):
            title = None                       # drop the cue's album TITLE
        elif line.startswith("TITLE "):
            title = line[6:].strip().strip('"')
        elif line.startswith("INDEX 01"):
            mm, ss, sub = (int(x) for x in line.split()[2].split(":"))
            raw.append((mm, ss, sub, title))
    per_s = 100 if any(sub > 74 for _, _, sub, _ in raw) else 75
    marks = [(mm * 60 + ss + sub / per_s, t) for mm, ss, sub, t in raw
             if mm * 60 + ss < total_s]
    if not marks:
        return None
    lines = [";FFMETADATA1"]
    for i, (start, name) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else total_s
        if end <= start:                       # ragged tail mark -> drop it
            continue
        lines += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={int(start * 1000)}",
                  f"END={int(end * 1000)}", f"title={name or f'Chapter {i + 1}'}"]
    return "\n".join(lines) + "\n"


def audio_files(folder):
    return sorted(p for p in folder.iterdir()
                  if p.is_file() and p.suffix.lower() in AUDIO_EXTS)


def _prep_part(src, work, idx):
    """Encode (or copy, if already AAC) one part to a uniform AAC temp on its
    own core. Returns (temp_path, duration_seconds)."""
    dst = work / f"part{idx:04d}.m4a"
    is_aac = src.suffix.lower() in AAC_EXTS
    codec = ["-c:a", "copy"] if is_aac else \
            ["-c:a", _ENCODER, "-b:a", target_bitrate(src)]
    _heavy(["ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
            "-i", str(src), "-map", "0:a", "-vn", *codec, "-f", "mp4", str(dst)],
           None if is_aac else duration_s(src),      # copies are instant, no bar
           None if is_aac else src.name)
    return dst, duration_s(dst)



# ---- HE-AAC re-signalling -------------------------------------------------
# Some stores (Libro.fm) ship .m4b files whose HE-AAC is signalled
# hierarchically (AudioObjectType 5, advertised as mp4a.40.5). The frames are
# ordinary SBR, but Apple's sync check rejects that signalling outright --
# "<book> was not copied ... because it cannot be played on this iPhone".
# We copy such audio verbatim, so the output inherits the problem. Re-signal
# the identical stream the backward-compatible way (AOT 2 + the 0x2B7 SBR sync
# extension, mp4a.40.2) instead of re-encoding it.

_MP4_CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl"}
_MP4_SKIP = {b"stsd": 8, b"mp4a": 28}          # box-specific bytes before children


def _asc_backward_compatible(asc):
    """AudioSpecificConfig -> the backward-compatible SBR form of the same
    stream, or None if it is not hierarchically-signalled HE-AAC."""
    bits = "".join(f"{b:08b}" for b in asc)
    i = 0

    def take(n):
        nonlocal i
        i += n
        return int(bits[i - n:i], 2)

    if len(bits) < 22 or take(5) != 5:         # not AOT 5
        return None
    freq, chans, ext_freq = take(4), take(4), take(4)
    if take(5) != 2 or 15 in (freq, ext_freq):  # core must be plain AAC-LC
        return None
    out = f"{2:05b}{freq:04b}{chans:04b}000{0x2B7:011b}{5:05b}1{ext_freq:04b}"
    out += "0" * (-len(out) % 8)
    return bytes(int(out[j:j + 8], 2) for j in range(0, len(out), 8))


def _find_esds(buf, start, end, chain):
    """-> (offset of the esds payload, size-field offsets of every ancestor)."""
    off = start
    while off + 8 <= end:
        size = int.from_bytes(buf[off:off + 4], "big")
        typ, body = bytes(buf[off + 4:off + 8]), off + 8
        if size == 1:
            size, body = int.from_bytes(buf[off + 8:off + 16], "big"), off + 16
        if size < 8:
            return None
        if typ == b"esds":
            return body + 4, chain + [off]     # +4 skips version/flags
        if typ in _MP4_CONTAINERS or typ in _MP4_SKIP:
            hit = _find_esds(buf, body + _MP4_SKIP.get(typ, 0), off + size,
                             chain + [off])
            if hit:
                return hit
        off += size
    return None


def _desc_len(buf, off):
    """MPEG-4 expandable length -> (value, offset of its last byte, next offset)."""
    val = 0
    while True:
        b = buf[off]
        off += 1
        val = (val << 7) | (b & 0x7F)
        if not b & 0x80:
            return val, off - 1, off


def fix_sbr_signalling(path):
    """Rewrite path's HE-AAC config in place so Apple will sync it. Audio bytes
    are untouched -- only the esds config grows by one byte. Returns True if it
    changed anything; a no-op (and safe) on every other kind of file."""
    total = path.stat().st_size
    with path.open("rb") as f:
        off, moov = 0, None
        while off + 8 <= total:                # find the top-level moov
            f.seek(off)
            size, typ = int.from_bytes(f.read(4), "big"), f.read(4)
            if size == 1:
                size = int.from_bytes(f.read(8), "big")
            if size < 8:
                return False
            if typ == b"moov":
                moov = (off, size)
            off += size
        if not moov or moov[0] + moov[1] != total:
            return False        # moov isn't last: growing it would shift mdat
        f.seek(moov[0])
        buf = bytearray(f.read(moov[1]))

    hit = _find_esds(buf, 8, len(buf), [])
    if not hit:
        return False
    desc, chain = hit

    lens = []                                  # length fields that must grow too
    if buf[desc] != 0x03:                      # ES_Descriptor
        return False
    _, lf, p = _desc_len(buf, desc + 1); lens.append(lf)
    flags, p = buf[p + 2], p + 3
    if flags & 0x80: p += 2                    # streamDependenceFlag
    if flags & 0x40: p += 1 + buf[p]           # URL_Flag
    if flags & 0x20: p += 2                    # OCRstreamFlag
    if buf[p] != 0x04:                         # DecoderConfigDescriptor
        return False
    _, lf, p = _desc_len(buf, p + 1); lens.append(lf)
    p += 13                                    # objectType..avgBitrate
    if buf[p] != 0x05:                         # DecoderSpecificInfo
        return False
    n, lf, asc = _desc_len(buf, p + 1); lens.append(lf)

    new = _asc_backward_compatible(bytes(buf[asc:asc + n]))
    if new is None:
        return False
    grow = len(new) - n
    if any(buf[o] + grow > 0x7F for o in lens):
        return False                           # would need a wider length field

    out = buf[:asc] + new + buf[asc + n:]
    for o in lens:
        out[o] += grow
    for o in [0] + chain:                      # moov itself, then every ancestor
        out[o:o + 4] = (int.from_bytes(out[o:o + 4], "big") + grow).to_bytes(4, "big")

    tmp = path.with_suffix(path.suffix + ".tmp")
    with path.open("rb") as f, tmp.open("wb") as g:
        left = moov[0]
        while left:
            chunk = f.read(min(1 << 20, left))
            g.write(chunk); left -= len(chunk)
        g.write(out)
    tmp.replace(path)
    return True


def build(book, out_path, work, dry, title=None, author=None):
    """book = list of audio file Paths (1 = single, >1 = merge).
    Multi-part books encode every part in parallel, then concat-copy — uses all
    cores instead of one serial encode through the concat demuxer."""
    multi = len(book) > 1
    title = title or clean_title(book[0].parent.name if multi else book[0].name)

    if dry:
        how = (f"encode {len(book)} parts in parallel ({_ENCODER}, source rate "
               f"capped at {_cap_kbps(CAP_BITRATE)}), then concat-copy"
               if multi else
               "copy" if book[0].suffix.lower() in AAC_EXTS else f"encode ({_ENCODER})")
        return f"would {how} -> {out_path.name}"

    cover = local_cover(book)                  # a jpg/png in the folder wins
    src_has_cover = False
    if cover is None:
        cover = work / "cover.jpg"
        query = f"{title} - {author}" if author else title
        if multi:
            fetch_cover(query, cover)
        else:
            src_has_cover = has_cover(book[0])
            if not src_has_cover:
                fetch_cover(query, cover)
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
        cue = book[0].with_suffix(".cue")       # sidecar chapters for one big file
        if cue.exists() and not has_chapters(book[0]):
            meta = cue_chapters(cue, duration_s(book[0]))
            if meta:
                metafile = work / "meta.txt"
                metafile.write_text(meta)
                cmd += ["-i", str(metafile)]
                n_in, meta_idx = 2, 1
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
    if author:                                 # a multi-part book maps metadata
        cmd += ["-metadata", f"artist={author}",       # from the chapter file,
                "-metadata", f"album_artist={author}"]  # which carries no artist


    cmd += ["-f", "mp4", str(out_path)]
    # live percent only for a real single-file encode; concat-copy is instant
    enc = (not multi) and audio_codec[1] == _ENCODER
    _heavy(cmd, duration_s(book[0]) if enc else None, title if enc else None)
    fix_sbr_signalling(out_path)               # Apple won't sync mp4a.40.5

    if multi:                                  # drop temp parts, keep the .m4b
        for t in temps:
            t.unlink(missing_ok=True)
    return None


def process_book(name, files, out_root, work_root, dry, force):
    """Returns a one-line status string (printed by the caller)."""
    name, title, author = standard_name(files)
    safe = re.sub(r'[/:]', '-', name)
    out_path = out_root / (safe + ".m4b")
    if any(out_path.resolve() == f.resolve() for f in files):   # would clobber source
        out_path = out_root / (safe + " (m4b).m4b")
    n = len(files)
    label = f"{name}  ({n} file{'s' if n > 1 else ''})"
    if out_path.exists() and not force:
        return f"✓ skip (exists): {out_path.name}"
    work = work_root / safe
    work.mkdir(exist_ok=True)
    try:
        msg = build(files, out_path, work, dry, title, author)
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
    out_arg = None
    if "--out" in args:
        i = args.index("--out")
        out_arg = args[i + 1]
        del args[i:i + 2]
    pos = [a for a in args if not a.startswith("-")]
    root = Path(pos[0]) if pos else Path.cwd()

    global _JOBS, _SEM, _ENCODER
    _JOBS = jobs
    _SEM = threading.BoundedSemaphore(jobs)
    _ENCODER = pick_encoder()

    single = root.is_file()               # one file arg -> just that book
    out_root = root.parent if single else root   # output sits next to the input
    if out_arg:
        out_root = Path(out_arg)
        out_root.mkdir(parents=True, exist_ok=True)
    work_root = Path(tempfile.mkdtemp(prefix="audiobookify-"))  # scratch, off-tree

    books = []  # (name, [files])
    if single:
        books.append((root.stem, [root]))
    else:
        for item in sorted(root.iterdir()):
            if item.is_dir():
                files = audio_files(item)
                if files:
                    books.append((item.name, files))
        # a prior run's outputs live here now (no m4b/ subfolder) -> don't re-eat them
        generated = {f"{re.sub(r'[/:]', '-', n)}.m4b" for n, _ in books}
        generated.add(f"{re.sub(r'[/:]', '-', root.name)}.m4b")
        loose = [item for item in sorted(root.iterdir())
                 if item.is_file() and item.suffix.lower() in AUDIO_EXTS
                 and item.name not in generated]
        if loose:
            books.append((root.name, sorted(loose)))

    try:
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
    finally:
        shutil.rmtree(work_root, ignore_errors=True)


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
    cue = Path(tempfile.mkdtemp()) / "x.cue"
    cue.write_text('TITLE "Album"\nTRACK 1 AUDIO\n  TITLE "One"\n  INDEX 01 0:0:00\n'
                   'TRACK 2 AUDIO\n  TITLE "Two"\n  INDEX 01 1:30:38\n')
    c = cue_chapters(cue, 200.0)
    assert "title=One" in c and "title=Two" in c and "title=Album" not in c
    assert "START=0" in c and "END=90506" in c and "START=90506" in c and "END=200000" in c
    # a sub-second field above 74 means centiseconds, not 1/75 frames
    cue.write_text('TRACK 1 AUDIO\n  INDEX 01 0:0:00\nTRACK 2 AUDIO\n  INDEX 01 1:30:91\n')
    assert "START=90910" in cue_chapters(cue, 200.0)
    # a mark at/past EOF would make end <= start; ffmpeg refuses that, so drop it
    cue.write_text('TRACK 1 AUDIO\n  INDEX 01 0:0:00\nTRACK 2 AUDIO\n  INDEX 01 5:0:00\n')
    assert cue_chapters(cue, 200.0).count("[CHAPTER]") == 1
    assert std_name("How Children Learn, 50th Anniversary Edition",
                    "How Children Learn, 50th Anniversary Edition (Unabridged)", "John Holt") == \
        ("How Children Learn - John Holt (50th Anniversary Edition, Unabridged)",
         "How Children Learn")
    assert std_name("Grow Wild: The Whole-Child Guide (Pack #03)", "", "Katy Bowman") == \
        ("Grow Wild - Katy Bowman", "Grow Wild")
    assert std_name("DumbingUsDownUnabridged", "DumbingUsDownUnabridged",
                    "John Taylor Gatto")[0] == "Dumbing Us Down - John Taylor Gatto (Unabridged)"
    assert std_name("Better with Age - The Psychology of Aging", "", "") == \
        ("Better with Age", "Better with Age")
    # bitrate: cap high sources at 128k, keep lower sources as-is
    assert _cap_kbps(320000) == "128k" and _cap_kbps(96000) == "96k" \
        and _cap_kbps(64000) == "64k"
    # HE-AAC signalled hierarchically (mp4a.40.5) -> backward compatible
    assert _asc_backward_compatible(bytes.fromhex("2b8a0800")).hex() == "138856e5a0"
    assert _asc_backward_compatible(bytes.fromhex("138856e5a0")) is None   # plain LC
    # progress clock parsing: real timestamp -> seconds, "N/A" -> None
    assert abs(_hms("01:02:03.5") - 3723.5) < 1e-6 and _hms("N/A") is None
    # local cover override: named/cover, jpg/png, single/multi; none -> None
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        (d / "Book.mp3").touch()
        assert local_cover([d / "Book.mp3"]) is None
        (d / "cover.png").touch()
        assert local_cover([d / "Book.mp3"]) == d / "cover.png"
        (d / "Book.jpg").touch()                  # same-name beats generic cover
        assert local_cover([d / "Book.mp3"]) == d / "Book.jpg"
        sub = d / "My Series"; sub.mkdir()
        (sub / "01.mp3").touch(); (sub / "My Series.png").touch()
        assert local_cover([sub / "01.mp3", sub / "02.mp3"]) == sub / "My Series.png"
    print("selftest ok")


if __name__ == "__main__":
    main()
