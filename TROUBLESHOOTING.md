# Troubleshooting

## The app will not start

### `FFmpeg was not found on PATH`

```bash
sudo apt-get update && sudo apt-get install -y ffmpeg
ffmpeg -version
```

Then `./scripts/dev.sh` again.

### `Address already in use`

Something is already on port 8000.

```bash
API_PORT=8080 ./scripts/dev.sh
```

Then open port 8080 in the PORTS tab instead.

### `ModuleNotFoundError: No module named 'PIL'`

```bash
python3 -m pip install -r requirements.txt
```

## Codespaces

### The forwarded URL will not load on my phone

Open the **PORTS** tab, long-press port 8000, choose **Port Visibility** ->
**Public**. Private ports need a browser session GitHub can authenticate, which
is unreliable on mobile.

### The Codespace stopped mid-render

Codespaces suspend after 30 minutes of inactivity, and a long render with no
browser interaction can look idle. Keep the tab open while rendering. Restart
the Codespace, run `./scripts/dev.sh`, and re-submit; uploaded sources survive
a restart, so you do not need to upload again.

### It ran out of disk

```bash
df -h /workspaces
rm -rf data/jobs/*
rm -rf data/uploads/*
```

Lower `JOB_RETENTION_HOURS` in `.env` to clean up sooner.

## Uploads

### The upload fails immediately

Check the extension is in `ALLOWED_VIDEO_EXTENSIONS` (`.mp4 .mov .mkv .webm
.m4v .avi` by default) and the file is under `MAX_UPLOAD_BYTES` (4 GiB).

### `That video's duration is unknown`

ffprobe could not read the file - it is usually truncated or a partial
download. Verify locally:

```bash
ffprobe -v error -show_format your-video.mp4
```

If that errors, the source file is the problem. Re-export or remux it:

```bash
ffmpeg -i broken.mp4 -c copy fixed.mp4
```

### The upload progress bar stalls near the end

That is the server writing the last chunks and probing the file. On a large
source it can take a few seconds. It has not hung.

## Clip selection

### `End time must be after start time`

Both fields accept `HH:MM:SS`, `MM:SS` or plain seconds - but be consistent.
`130` means 130 seconds, not 1:30.

### `Clip is longer than the maximum`

The cap is 600 seconds. Shorts do not need more, and it protects you from
accidentally rendering an hour.

## Captions

### No captions in the output

Check, in order:

1. Is **Captions** enabled?
2. Is the caption source `none`?
3. Does the clip actually contain speech? Silence produces no captions.
4. If the source is `faster_whisper`, is it installed?

```bash
python3 -c "import faster_whisper; print('installed')"
```

If that fails, either install it (`pip install -r requirements-optional.txt`)
or switch the caption source to **Paste transcript manually**.

### faster-whisper will not install

It needs to download ~200 MB of wheels. On a restricted network it will fail.
The app is designed for this: use **Paste transcript manually**. You get real,
timed captions with identical styling and the same 3-word grouping - you just
supply the words.

### The first render with Whisper takes forever

The model downloads once (~145 MB for `base`) into `data/models/`. Later jobs
reuse it. If it is still slow, drop to `WHISPER_MODEL_SIZE=tiny`.

### Captions have more than 3 words

They cannot - the limit is enforced before every other grouping rule and is
covered by tests. If you are seeing long lines, check **Max words per phrase**
in Caption Settings; a saved preset may have raised it.

### Captions are cut off at the edge of the square

Increase **Horizontal margin** or reduce **Font size**. Captions are
deliberately clipped to the square - that is the intended composition - so a
very large font with a small margin will collide with the boundary.

## Fonts

### `Indivisible font not installed`

Expected until you provide it. Upload a `.ttf`/`.otf` from *Caption Settings* ->
**Upload font**, or drop it in `fonts/`. The font is not redistributable, so it
cannot ship with the repository.

### The uploaded font is rejected

Only `.ttf` and `.otf` are accepted, up to 20 MB, and the file must actually
parse as a font. A renamed `.woff2` will be rejected.

### The output used the wrong font

Check the warnings on the finished job. The app reports every fallback rather
than substituting silently.

## Rendering

### The job failed with an FFmpeg error

The UI shows a readable summary; the terminal running `dev.sh` has the full
FFmpeg output. The most common causes are a corrupt source or a clip range
beyond the real duration (some containers report a duration longer than the
actual stream).

### The output has no audio

Check the metadata panel after upload - if **Audio** says *none*, the source has
no audio track, so neither will the output, and there will be no captions.

### Rendering is very slow

CPU encoding on 2 cores is the bottleneck. Options:

- Use 30 FPS instead of 60 (roughly halves the work)
- Lower `x264_preset` toward `veryfast` in the output settings
- Use a shorter clip
- Use a larger Codespace machine type

Roughly: a 30-second clip at 1080p30 takes about a minute on a 2-core
Codespace, plus transcription time.

### The video is soft or blurry

The source is lower resolution than the square. Zooming a 720p source into a
1000px viewport is an upscale. Supersampling reduces it but cannot invent
detail. Use a higher-resolution source, or reduce the auto zoom amount.

### The square appears to move

It cannot, structurally - but if you believe it is, prove it:

```bash
python3 scripts/smoke_test.py
```

The last section extracts frames at different timestamps during an active zoom
and compares the square's bounding box. If that passes, what you are seeing is
the video content moving *inside* a fixed square, which is the intended effect.

## Silence removal

### Too much was cut

Raise **Min silence duration** (0.45s default) or lower **Threshold** toward
`-40 dB`.

### Nothing was cut

The clip may have background noise above the threshold. Raise **Threshold**
toward `-25 dB`.

### The whole clip is silent

The app keeps everything rather than producing an empty video. This is
deliberate, and tested.

## Still stuck

Run the diagnostic:

```bash
python3 scripts/smoke_test.py
```

It checks FFmpeg, the required filters, every import, and then renders and
inspects a real video. The first failing line tells you which part is broken.
