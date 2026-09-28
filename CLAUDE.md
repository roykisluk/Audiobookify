# Audiobookify

Turns folders of audio into iPhone-ready `.m4b` files. Standard library +
`ffmpeg` only — keep it that way, no new dependencies.

## Operating rules

- Originals are never modified. Output goes next to the input; a name clash
  gets a ` (m4b)` suffix rather than overwriting.
- Already-AAC input (`.m4a`/`.m4b`/`.aac`) is **copied, never re-encoded**.
  Anything that "fixes" such a file must therefore be a header-level change,
  not a transcode.
- Every non-trivial function gets an assertion in `selftest()`. Run
  `python3 audiobookify.py --selftest` before calling anything done.

## Lesson log

### 2026-08-23 — iPhone refuses to sync some `.m4b` files

**Symptom.** Finder sync: *“The Biggest Ideas in the Universe” was not copied
to the iPhone because it cannot be played on this iPhone.* The file played
fine in ffmpeg, QuickTime and Music on the Mac.

**Root cause.** The file (a Libro.fm purchase, not built by this tool) carried
HE-AAC signalled *hierarchically*: AudioSpecificConfig `2b8a0800` —
AudioObjectType 5, so ffprobe reports `mime_codec_string=mp4a.40.5`. Apple's
sync compatibility check rejects that codec string outright, even though iOS
decodes the frames without trouble. The audio itself was never the problem.

The tell was a second HE-AAC audiobook in the same library that syncs fine:
its config is `138856e5a0` — the *backward-compatible* signalling, AOT 2 plus
the `0x2B7` SBR sync extension, reported as `mp4a.40.2`. Both configs describe
the identical stream (AAC-LC core at 22050 Hz, SBR to 44100).

**Fix.** `fix_sbr_signalling()` rewrites the config in the `esds` box to the
backward-compatible form. Audio frames are untouched — decoded PCM is
bit-identical (verified by md5 of `-f s16le` output), chapters and cover art
survive. It runs on every finished `.m4b` and no-ops on anything else.

**Gotchas that cost time here, worth remembering:**

- The new config is **one byte longer** (37 bits vs 25). Every ancestor box
  size must grow: `esds`, `stsd`'s `mp4a` entry, `stsd`, `stbl`, `minf`,
  `mdia`, `trak` — **and `moov` itself**. Missing `moov` was the first bug:
  ffmpeg still read the file happily, ffprobe reported everything correctly,
  chapters and PCM all checked out, and the only visible symptom was the last
  box ending one byte short of EOF. Tolerant demuxers hide this; Apple's
  strict one would not have.
- The three MPEG-4 descriptor length fields (`0x03`, `0x04`, `0x05`) must grow
  too — they use the expandable encoding, so a value crossing 0x7F would need
  a wider field. We bail out instead of handling that.
- The function **requires `moov` after `mdat`**. Growing a leading `moov`
  shifts every `mdat` byte, which invalidates all `stco` chunk offsets. The
  ffmpeg mp4 muxer writes `moov` last by default (no `+faststart`), so this
  holds for our output — but the check is there, and it bails rather than
  corrupting.
- `afinfo` (CoreAudio) reports both the broken and fixed files identically,
  so it is **not** a useful oracle for this. `ffprobe -show_entries
  stream=mime_codec_string` is what discriminates.

**Dead end.** First attempt re-encoded to AAC-LC 96k, which does produce a
syncable file but is lossy and 50% larger. Kept only as a fallback; the header
fix supersedes it.

**Open question.** Not yet confirmed by an actual successful sync — the
diagnosis rests on the working/broken pair in the library. If a file with
`mp4a.40.2` still gets refused, the next suspects are the `M4A ` major brand
and the chapter text track.

### 2026-08-24 — `.cue` sidecars: chapters for one-big-file books

**Symptom.** A 12-hour single-`.mp3` book came out with zero chapters, while a
`.cue` listing 20 of them sat unused in the same folder.

**Fix.** `cue_chapters()` turns a sidecar `<book>.cue` into ffmetadata for the
single-file path. Guarded by `has_chapters()` — a source that already carries
its own chapters (a store `.m4b`) keeps them; the cue never overrides.

**Root cause of the follow-up failure.** First version divided the third INDEX
field by 75 (CUE frames). This library's cues write **centiseconds**: values up
to 91 appear, which is impossible for frames (0–74). Dividing by 75 inflated
every mark, and the last one landed *past EOF* — ffmpeg then refused the whole
metadata input with a misleading error:

    Chapter end time 44214430 before start 44214573
    Error opening input: Cannot allocate memory

"Cannot allocate memory" is what ffmpeg reports for a rejected metadata input;
it has nothing to do with memory. The real message is the line above it.
`per_s` is now sniffed per file (any field > 74 → centiseconds), and marks at
or past the source duration are dropped.

**Gotchas.**

- `-map_metadata 1` on the cue path replaces the source's global tags, so the
  mp3's artist/date are dropped (title/album are re-set explicitly). Same
  behaviour the multi-file path already had.
- `selftest()` had a stray `import tempfile` at the bottom of the function.
  That makes `tempfile` local to the *whole* function, so a use above it raised
  `UnboundLocalError` despite the module-level import. Removed.
- The cue's header `TITLE "<album>"` precedes the first `TRACK`; reset the
  pending title on every `TRACK` line or chapter 1 inherits the book title.

**Not derivable.** `DumbingUsDownUnabridged.mp3` and `Free-Range Kids` have no
cue and no embedded chapters — nothing to build a chapter list from, so they
ship as one chapter.

### 2026-08-24 — standardized output names: `Title - Author (Edition, Unabridged)`

Output filenames now come from `standard_name()` rather than the input's folder
or file name, which arrive in every shape (`Author - Title`, `Title by Author
(Audiobook)`, `DumbingUsDownUnabridged`).

**Where the pieces come from.** The `album` tag is a far better title source
than the `title` tag: `title` is often the *part* (`Free to Learn - Part 01`) or
the *subtitle* (Unconditional Parenting's title tag is "Moving from Rewards and
Punishments to Love and Reason"). Album was right for all 13 books in the test
library. Author is `artist`/`album_artist`, cut at the first `/` (the narrator
is appended there) and stripped of `, PhD`. When there is no artist tag at all,
`itunes_author()` looks it up — the same API the covers already use — which is
what supplies Gatto and Skenazy.

**Edition and abridgement are searched in a blob of every string we have**
(folder name + file name + tags), not just the title: `(Unabridged)` frequently
lives only in the folder name.

**Gotcha — the display name must not reach the tags.** First version set
`title = standard_name(...)`, so `-metadata title=` became "Hold On to Your
Kids - Gordon Neufeld, Gabor Maté (Unabridged)" and Apple Books would show the
author twice. `std_name()` therefore returns *both* the filename and the bare
title, and `build()` takes title and author as separate arguments. Filename
shape and tag shape are different things; keep them in different variables.

**Gotchas that cost time:**

- `.title()` on "50th Anniversary Edition" yields "50**T**h". Capitalize
  word-by-word and only when the word `isalpha() and islower()`.
- `\bunabridged\b` cannot match inside `DumbingUsDownUnabridged` — no word
  boundary between `n` and `U`. The blob gets the camel-case split too, not
  just the title.
- Multi-part books use `-map_metadata 1` (the chapter file), which has no
  global tags, so the author was being dropped entirely. `artist` and
  `album_artist` are now set explicitly from the resolved author.

### 2026-09-28 — multi-disk book, wrong cover, `--out` inside the root

**Symptom 1.** A book shipped as `Book/Disk 1 …/`, `Disk 2 …/` … would come
out as six separate `.m4b`s. **Cause:** each subfolder of the target is its own
book, and a folder is not searched recursively. **Workaround (no code change):**
copy the tracks into one flat staging folder, renumbered `01 … 43` so the order
holds across disks (the chapter title drops the leading number). The per-disk
`album` tag (`… Disk 1 The Theory Of Gravitation`) would become the title, so
retag the *copies* with `ffmpeg -c copy -metadata album=<series>`.

**Symptom 2.** *The Very Best of the Feynman Lectures* got the cover of *The
Pleasure of Finding Things Out*. **Cause:** iTunes has no audiobook by that
name, so the fuzzy search returned a different book by the same author. Open
Library had the right one (`cover_i` 299373). **Fix used:** drop `cover.jpg`
in the book folder (the local override) and rebuild with `--force`. Open
question: `fetch_cover()` does not check that the hit's title matches the
query, so an author-only match is accepted. Always look at the cover.

**Symptom 3.** The second run left an extra `<name> (m4b).m4b`. **Cause:** my
`--out` pointed *inside* the target folder, so the run found `out/` as one
more "book" (its only file was the first run's `.m4b`) and repackaged it.
Keep `--out` outside the target folder.
