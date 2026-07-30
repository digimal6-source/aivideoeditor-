# Shortform Clipper

A personal tool that turns a long-form video into a finished 9:16 short.

You decide *what* to clip and *what* the hook says. This app does the boring
part: cutting the clip, removing dead air, transcribing, generating 3-word
captions, and rendering the exact composition you use every time.

There is no AI clip-picking, no LLM, no API key, and nothing paid. It is a
deterministic video renderer with a web interface.

---

## 1. Project overview

The composition it renders, every time:

```
1080 x 1920 (9:16), black background

  +----------------------------------+
  |                                  |
  |      Hiring Is Now The           |   <- hook, ABOVE the square
  |      SLOWEST Way To Ship         |      (highlighted word in your colour)
  |                                  |
  |   +--------------------------+   |
  |   |                          |   |
  |   |                          |   |
  |   |      VIDEO (1:1)         |   |   <- fixed square viewport
  |   |                          |   |      never moves, never resizes
  |   |      important thing     |   |   <- captions, INSIDE the square
  |   |                          |   |
  |   +--------------------------+   |
  |                                  |
  +----------------------------------+
```

The critical detail: zoom and pan are applied to the **video layer**, and the
square then clips it. The square is not cropped-then-zoomed. As the video
breathes, the square stays pinned to the pixel.

## 2. Features

- Browser upload of the full long-form video (streamed to disk, never buffered
  in memory)
- Clip selection by timestamp (`1:23:45`, `12:30`, or `750`)
- Silence removal (FFmpeg `silencedetect`), tuned to cut dead air but keep
  natural pauses
- Local speech-to-text with faster-whisper, or paste a transcript manually
- Captions grouped to a **maximum of 3 words** by default, respecting sentence
  boundaries, punctuation and pauses
- Fixed 1:1 viewport with configurable size and vertical position
- Deterministic, subtle auto zoom/pan; manual keyframes also supported
- Hook text above the square with per-word highlighting
- 10-colour highlight palette (default Electric Violet `#8B5CF6`) plus custom HEX
- `.ttf` / `.otf` font upload from the browser
- Saveable presets (ships with "My Default")
- Live 9:16 preview that reflects your fonts, colours and layout
- Real per-stage progress from the actual render, not a fake timer
- 1080x1920 MP4, H.264 + AAC, 30 or 60 FPS
- Works on an Android phone browser

## 3. Architecture

One Python process serves both the JSON API and the web interface. There is no
build step, no npm install, and no framework.

```
browser (vanilla JS)  ->  stdlib HTTP server  ->  job manager (thread pool)
                                              ->  render pipeline  ->  FFmpeg
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for the layer breakdown, the FFmpeg
filter graph, and the reasoning behind the choices.

## 4. Requirements

- Python 3.10+
- FFmpeg 5.0+ (must include `libass`, `libx264`, `zoompan`)
- ~2 GB free disk for a typical job

In GitHub Codespaces both are installed for you.

## 5. GitHub Codespaces setup

This is the whole thing. On your phone, in the GitHub app or browser:

1. Open the repository.
2. Tap **Code** -> **Codespaces** -> **Create codespace on main**.
3. Wait for it to finish building (first time: a few minutes).
4. In the terminal at the bottom, run:

```bash
./scripts/setup.sh
```

5. Then start it:

```bash
./scripts/dev.sh
```

6. Open the **PORTS** tab, find port **8000**, and tap the globe icon to open
   the app in your browser.

That is it. Two commands.

> If the page will not load on your phone, set port 8000's visibility to
> **Public** in the PORTS tab (long-press the row -> Port Visibility -> Public).

## 6. FFmpeg installation

Codespaces: `./scripts/setup.sh` installs it. Nothing to do.

Elsewhere:

```bash
sudo apt-get update && sudo apt-get install -y ffmpeg   # Debian / Ubuntu
brew install ffmpeg                                     # macOS
```

Verify:

```bash
ffmpeg -version
ffmpeg -hide_banner -filters | grep -E ' (ass|zoompan|silencedetect) '
```

All three filters must appear.

## 7. Python setup

```bash
python3 -m pip install -r requirements.txt
```

That is only Pillow and fonttools. The server itself uses the standard library.

## 8. Node setup

None. There is no frontend build. The interface is three static files served by
the Python process.

## 9. Whisper setup

Optional but recommended:

```bash
python3 -m pip install -r requirements-optional.txt
```

The model downloads **once** on first use and is cached in
`data/models/` (`WHISPER_DOWNLOAD_ROOT`). It is not re-downloaded per job.
Everything runs on CPU by default.

| `WHISPER_MODEL_SIZE` | Size | Notes |
|---|---|---|
| `tiny` | ~75 MB | Fastest. Struggles with names and jargon. |
| `base` | ~145 MB | **Default.** Good balance on a 2-core Codespace. |
| `small` | ~480 MB | Clearly better. Roughly 2-3x slower than base. |
| `medium` | ~1.5 GB | Best CPU-practical accuracy. Slow on 2 cores. |

**If you skip this step the app still works.** Under *Caption Settings*, set
**Caption source** to **Paste transcript manually**, paste your text, and the
app distributes it across the clip's speech and applies the same 3-word
grouping and styling.

## 10. Font setup

The app defaults to **Indivisible** for captions and **Rubik Bold** for the
hook. Those fonts are not redistributable, so they are not in this repository.

Two ways to add them:

- **From your phone:** in the app, *Caption Settings* -> **Upload font**, and
  pick a `.ttf` or `.otf`. It is stored in `fonts/` and appears in both font
  dropdowns immediately.
- **From the terminal:** drop the files into `fonts/` and restart.

Until then the font dropdowns show *"Indivisible (not uploaded)"* and rendering
falls back to a system font. The app tells you it did this rather than
silently substituting: you will see a warning on the finished job.

## 11. Environment variables

Copy the template (`./scripts/setup.sh` does this for you):

```bash
cp .env.example .env
```

Everything has a working default. The ones you might actually change:

| Variable | Default | Purpose |
|---|---|---|
| `API_PORT` | `8000` | Port to serve on |
| `DATA_DIR` | `./data` | Root for uploads, outputs, temp jobs |
| `FONTS_DIR` | `./fonts` | Where uploaded fonts live |
| `MAX_UPLOAD_BYTES` | `4294967296` | 4 GiB upload ceiling |
| `JOB_RETENTION_HOURS` | `12` | Finished jobs/outputs deleted after this |
| `MAX_CONCURRENT_JOBS` | `1` | Rendering is CPU-bound; 1 suits 2 cores |
| `TRANSCRIPTION_BACKEND` | `faster_whisper` | or `manual`, or `none` |
| `WHISPER_MODEL_SIZE` | `base` | See the table above |
| `API_BASE_URL` | *(empty)* | Set only if the UI is hosted elsewhere |

There are no secrets, no keys, and nothing to pay for. `.env` is git-ignored.

## 12. Running the backend

```bash
./scripts/dev.sh
```

or directly:

```bash
python3 -m app.main
```

## 13. Running the frontend

Nothing to run. The backend serves it at `/`.

## 14. Running the full app

```bash
./scripts/dev.sh
```

One process. Open port 8000.

## 15. Uploading a video

Tap the upload area at the top and pick a file (MP4, MOV, MKV, WebM, M4V, AVI).
A real progress bar tracks the transfer. When it finishes, the metadata panel
shows filename, size, duration, resolution, FPS, and whether audio was found.

The file streams straight to disk, so a multi-gigabyte source is fine.

## 16. Generating a clip

1. **Clip Selection** - enter start and end. `1:23:45`, `12:30` and `750` all
   work. Validation is immediate and explains what is wrong.
2. **Hook** - type the hook text. Every word becomes a chip; **tap the words you
   want highlighted**. Pick a colour from the swatches or enter a HEX value.
3. **Caption Settings** - font, size, max words per phrase (3), outline, shadow,
   position.
4. **Video Effects** - `auto` (subtle, deterministic), `manual` keyframes, or
   `none`. Silence removal settings are here too.
5. **Output Settings** - 1080p30 or 1080p60, and the square's size/position.
6. Tap **Generate**.

The preview panel updates as you type, so you can see the composition before
rendering.

## 17. Downloading the output

When the job reaches *Done*, the Result card shows the finished video inline
(seekable) with a **Download MP4** button. On Android that saves to your
Downloads folder.

On disk it is at `data/outputs/<job-id>.mp4`. It is deleted after
`JOB_RETENTION_HOURS`, so download it before then.

## 18. Running tests

```bash
python3 -m unittest discover -s tests -t .
```

88 tests, about 0.02 seconds. No FFmpeg needed.

## 19. Running the smoke test

This one renders a real video and inspects it:

```bash
python3 scripts/smoke_test.py
```

It generates its own test fixture, runs the whole pipeline, then extracts two
frames from different timestamps *while the auto zoom is running* and asserts
the square's bounding box is identical in both. That is the property this whole
application exists to guarantee.

```bash
python3 scripts/smoke_test.py --fps 60    # check the 60 FPS path
python3 scripts/smoke_test.py --keep      # keep the rendered MP4
```

## 20. Troubleshooting

See [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

## 21. Future deployment architecture

Nothing in the code assumes it all runs in one place:

- **Frontend to Vercel** - the UI is static. Set `API_BASE_URL` and it will talk
  to a remote backend; CORS is already handled via `CORS_ALLOW_ORIGIN`.
- **Backend as its own service** - it is a plain HTTP server with no local-path
  assumptions leaking into the API.
- **Rendering on a separate worker** - `RenderPipeline` is invoked through
  `JobManager`, which is the seam. Swap the thread pool for a queue consumer
  and the pipeline moves untouched.
- **Object storage** - all file access goes through the `StorageBackend`
  protocol in `app/storage.py`. Implement S3 against that interface; nothing
  else changes.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the specific seams.

---

## Licence and fonts

No font files are distributed here. Indivisible and Rubik are yours to supply
under their own licences.
