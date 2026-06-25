# Audiobookify

Turn a folder of audio files into iPhone-ready `.m4b` audiobooks — chapters,
cover art, the works. Originals are never touched; output lands in `./m4b/`.

For each top-level item in the folder:

| Input | Output |
|-------|--------|
| Folder of audio files | One `.m4b`, **one chapter per file** |
| Loose `.mp3` (or `.flac`, `.wav`, …) | Re-encoded to `.m4b` |
| Loose `.m4a` / `.m4b` | Repackaged to `.m4b` (audio copied, no re-encode) |

- **Parallel by default** — every book is processed at once (one worker per CPU).
- **Robust cover art** — tries iTunes, then Google Books, then Open Library,
  across a few title variants. Skipped if the file already has embedded art.
- **No API keys, no accounts.** Just `ffmpeg`.

## Requirements

- Python 3.8+ (standard library only)
- [`ffmpeg`](https://ffmpeg.org/) and `ffprobe` on your `PATH`
  (`brew install ffmpeg` on macOS)

## Usage

```bash
audiobookify              # process the current folder
audiobookify ~/Books      # process a specific folder
audiobookify --dry-run    # print the ffmpeg commands, change nothing
audiobookify --force      # rebuild even if the .m4b already exists
audiobookify --jobs 2     # cap parallelism (default: one per CPU)
```

Output `.m4b` files go to `./m4b/` inside the target folder.

## The `audiobookify` command

`install.sh` adds a shell alias so you can type `audiobookify` in any folder
and it processes that folder:

```bash
./install.sh        # adds the alias to ~/.zshrc, then: source ~/.zshrc
```

To remove it, delete the `audiobookify` alias line from `~/.zshrc`.

## How covers are found

1. **iTunes Search API** — audiobook, then ebook, then any media.
2. **Google Books API** — `volumeInfo.imageLinks`.
3. **Open Library** — `covers.openlibrary.org`.

Each provider is tried with the cleaned title, and — for `Author - Title`
names — with the title and author parts separately. First hit wins.

## Speed

A multi-part book is encoded **one part per core, in parallel**, then the
parts are concat-copied (a fast remux) into the final `.m4b` — so a 10-part
book on a 10-core machine encodes ~10× faster than one serial pass. Parts that
are already AAC (`.m4a`/`.m4b`) are copied, never re-encoded.

The encoder is auto-picked: `aac_at` (Apple AudioToolbox, fastest on macOS) →
`libfdk_aac` → ffmpeg's built-in `aac`. Use `--jobs N` to cap core usage.

> Note: AAC encoding is CPU-only — GPU and extra RAM don't speed it up. The
> win is using all your CPU cores instead of one.

## Quality / bitrate

Bitrate is chosen automatically per source — **the source's own rate, capped
at 128 kbps** (transparent for speech, and never upscales):

| Source | Output |
|--------|--------|
| 320k MP3 | 128k AAC |
| 96k MP3 | 96k AAC |
| Already AAC (`.m4a`/`.m4b`) | copied as-is — zero re-encode |

MP3 is already lossy, so a true "lossless" `.m4b` isn't possible; AAC matches
or beats MP3 at the same bitrate, so there's no audible loss. Change the cap
via `CAP_BITRATE` in `audiobookify.py`.

## Test

```bash
python3 audiobookify.py --selftest
```
