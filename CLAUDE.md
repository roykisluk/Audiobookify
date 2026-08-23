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
