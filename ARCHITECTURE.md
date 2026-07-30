# Architecture

## The shape of it

```
  app/web/            static UI      (no build step, no framework)
       |
       | HTTP + JSON
       v
  app/api/            http_server -> service -> jobs
       |
       v
  app/processor/      render pipeline -> FFmpeg
       |
       v
  app/storage.py      files on disk (behind an interface)
```

A modular monolith. One process, clear seams, no microservices.

## Layers

| Layer | Files | Responsibility |
|---|---|---|
| Configuration | `app/settings.py`, `app/colors.py` | Environment-driven settings; the single colour palette |
| Domain models | `app/models.py` | Typed, validated settings objects for every part of a render |
| Storage | `app/storage.py` | Source assets, job workspaces, outputs, retention |
| Fonts | `app/fonts.py` | Upload validation, family-name extraction, fallback |
| Presets | `app/presets.py` | Named configurations in a JSON file |
| Captions | `app/captions.py` | Word -> phrase grouping |
| Processing | `app/processor/` | Everything that touches FFmpeg |
| API | `app/api/` | HTTP routing, multipart, job lifecycle |
| UI | `app/web/` | Three static files |

### Why no web framework

The API surface is about fifteen endpoints. FastAPI would add ~40 MB of
dependencies and a wheel-download step to every fresh Codespace, in exchange
for request parsing this app barely needs. `http.server` plus a small router
and a hand-written streaming multipart parser costs ~500 lines and means
`pip install` is two packages. In a constrained or offline environment the app
still starts.

The multipart parser is streaming by design: file parts are exposed as
read-only streams piped straight to disk, so a 4 GB upload never touches memory.

### Why no frontend build

A React/Next app would need `npm install` (hundreds of MB), a dev server, a
build step, and a second process to keep alive. The interface is one page of
forms and a preview. Vanilla JS with CSS custom properties does it in three
files, loads instantly over a phone connection, and the backend serves it at
`/`. It remains fully static, so moving it to Vercel later is a copy.

## The render pipeline

`app/processor/render.py` runs these in order. Each reports progress.

| # | Stage | Module | What happens |
|---|---|---|---|
| 1 | Analyzing | `probe.py` | ffprobe the source |
| 2 | Extracting | `clip.py` | Cut `[start, end)` |
| 3 | Detecting silence | `silence.py` | `silencedetect` on the clip's audio |
| 4 | Removing silence | `silence.py` | Rebuild keep-segments with `concat` |
| 5 | Transcribing | `transcribe/` | faster-whisper (or manual text) |
| 6 | Building captions | `captions.py` | Group into <=3-word phrases |
| 7 | Rendering hook | `hook_render.py` | Pillow -> transparent PNG |
| 8 | Compositing + encoding | `compositor.py` | One FFmpeg filter graph |
| 9 | Finalizing | `render.py` | Probe the output, clean the workspace |

### Ordering decision: transcribe *after* cutting silence

Transcribing first would mean every caption timestamp needs remapping through
the list of removed intervals - a class of off-by-one bug that shows up as
captions drifting out of sync near the end of a clip. Transcribing the already-
cut audio means the timestamps are correct by construction. It costs one extra
pass over the audio and removes an entire failure mode.

## The composition

This is the part the whole app exists for.

```
[0:v] fps=FPS
      scale=W:W:force_original_aspect_ratio=increase   <- fill the square
      crop=W:W
      zoompan=z='<expr>':x='<expr>':y='<expr>':s=WxW   <- MOTION HAPPENS HERE
      scale=S:S                                        <- downsample supersample
      ass=captions.ass                                 <- captions, in square space
                                                          [sq]
color=c=black:s=1080x1920                              [bg]
[bg][sq] overlay=x=VX:y=VY                             <- square lands on canvas
[1:v] format=rgba                                      [hook]
[stage][hook] overlay=x=0:y=0                          <- hook on top
         format=yuv420p                                [vout]
```

Three properties fall out of this graph:

1. **The square cannot move.** `zoompan` has a fixed output size (`s=WxW`).
   Whatever the zoom expression does, the filter emits the same dimensions
   every frame. The `overlay` that places it uses constant coordinates. There
   is no code path that could resize or shift it.
2. **Captions cannot zoom.** `ass` is applied *after* `zoompan`, onto the
   already-square frame. libass renders against `PlayResX/Y` = the viewport
   size, so captions are authored in square-space and clipped by the square's
   edges for free.
3. **The hook cannot be affected by any of it.** It is overlaid onto the full
   canvas after the square is already placed.

### Why `zoompan` and not `crop`/`scale` with `eval=frame`

`crop` with per-frame expressions changes the *output dimensions* frame to
frame. Downstream filters and the encoder require a constant frame size, so
that approach either fails outright or forces a rescale that reintroduces the
exact wobble the fixed viewport is meant to prevent. `zoompan` is the only
standard filter that varies apparent scale while guaranteeing constant output
size. That guarantee is what makes the square provably fixed.

One wrinkle: `zoompan` has no `t` variable, so time is derived from the output
frame index as `on/FPS`. `build_expressions()` in `effects.py` compiles the
timeline into piecewise expressions on that basis.

### Supersampling

The square is rendered at `size x supersample` (default 2x) and downsampled
before encoding. Zooming into a 1000px square from a 720p source otherwise
shows softness; rendering at 2000px and scaling down hides it.

## Effects model

```python
VideoEffect(start=3.0, end=7.0, type="zoom_in", scale=1.0, scale_to=1.1)
```

`resolve_timeline()` turns settings into a list of these, whether the mode is
`auto`, `manual` or `none`. `auto` is a pure function of clip duration - no
randomness, so the same input always renders the same output. Default motion
is 6% over an 8-second cycle: enough to feel alive, not enough to look like
auto-generated content.

A manual keyframe timeline needs no pipeline changes; it already accepts the
same list.

## Caption grouping

`group_words()` accumulates words and flushes on the first of:

1. the word limit (default 3) - a hard cap, never exceeded
2. the character budget - stops two long words overflowing the square
3. sentence-ending punctuation
4. clause punctuation, if at least two words are buffered
5. a pause longer than 340 ms

The word limit is checked before everything else, so the guarantee holds
regardless of the text.

## Colour system

Every colour lives in `app/colors.py`. Ten named presets, Electric Violet
`#8B5CF6` first. Conversion helpers produce ASS (`&HAABBGGRR`) and FFmpeg forms.
No HEX literal appears anywhere else in the Python code; the UI reads the
palette from `GET /api/config`, so adding a colour is a one-line change.

## Job lifecycle

`JobManager` owns a `ThreadPoolExecutor` (default 1 worker - rendering is
CPU-bound). Progress is real: FFmpeg's `-progress` output is parsed against the
expected duration, and each stage contributes a weight (`STAGE_WEIGHTS`)
reflecting how long it actually takes, with encoding at 38%. Nothing is
simulated. Stages with unknown duration report indeterminate rather than
inventing a number.

Failures clean their workspace and keep no output. Successes keep the MP4 until
retention expires.

## Security

- FFmpeg is always invoked with an argument array, never a shell string
- Uploaded filenames are sanitised to `[A-Za-z0-9._ -]` and path components
  stripped
- IDs are validated against `^[a-z0-9][a-z0-9_-]{5,63}$` before touching disk
- Static file serving resolves the path and rejects anything outside the web
  directory (verified against `curl --path-as-is` traversal attempts)
- Font uploads are checked by parsing the file with fontTools, not by trusting
  the extension
- Upload size is capped by configuration
- Errors return a clean message; the traceback goes to the log only

## Extension seams

| Want to... | Change |
|---|---|
| Use S3 | Implement `StorageBackend`; nothing else moves |
| Render on a worker | Replace `JobManager`'s executor with a queue consumer |
| Host the UI on Vercel | Serve `app/web/` statically, set `API_BASE_URL` |
| Add a caption animation | Emit different ASS in `ass_render.py` |
| Add manual keyframes | Feed `VideoEffect` list from the UI (already supported) |
| Add an output preset | One entry in `OUTPUT_PRESETS` |
| Add a highlight colour | One entry in `app/colors.py` |
