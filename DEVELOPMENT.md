# Development

## Setup

```bash
git clone https://github.com/digimal6-source/aivideoeditor-.git
cd aivideoeditor-
./scripts/setup.sh
./scripts/dev.sh
```

## Layout

```
app/
  settings.py        environment-driven configuration
  colors.py          the ONLY place HEX values live
  models.py          typed, validated settings objects
  errors.py          error taxonomy -> HTTP status + safe message
  timecode.py        timestamp parsing and clip validation
  storage.py         disk storage behind StorageBackend
  fonts.py           font upload, validation, fallback
  presets.py         saved configurations
  captions.py        word -> phrase grouping
  processor/
    ffmpeg.py        subprocess wrapper with progress parsing
    probe.py         ffprobe -> MediaInfo
    clip.py          time-range extraction
    silence.py       silencedetect + segment rebuilding
    effects.py       timeline -> zoompan expressions
    ass_render.py    caption styling -> ASS
    hook_render.py   hook text -> transparent PNG
    compositor.py    the filter graph
    render.py        pipeline orchestration
    transcribe/      speech-to-text backends
  api/
    multipart.py     streaming multipart parser
    jobs.py          job manager and progress weighting
    service.py       application service layer
    http_server.py   routing
  web/               index.html, styles.css, app.js
scripts/             setup.sh, dev.sh, smoke_test.py, make_fixture.py
tests/               unittest suites
fonts/               user-supplied fonts (git-ignored)
data/                runtime data (git-ignored)
```

## Tests

```bash
python3 -m unittest discover -s tests -t .        # all 88
python3 -m unittest tests.test_pipeline -v        # one module
python3 -m unittest tests.test_pipeline.SilenceTests.test_padding_shrinks_the_cut_not_the_speech
```

`tests/test_foundation.py` covers timecode parsing, colour validation, presets,
filename sanitisation and model validation. `tests/test_pipeline.py` covers
caption grouping, silence interval maths, the effects timeline, ASS generation,
viewport geometry, progress weighting and the multipart parser.

Both are pure Python. FFmpeg is exercised by the smoke test instead, which
keeps the unit suite at ~20 ms.

```bash
python3 scripts/smoke_test.py
python3 scripts/smoke_test.py --fps 60
python3 scripts/smoke_test.py --keep
```

## Test fixture

`scripts/make_fixture.py` generates a synthetic video with real silence gaps, so
no media is committed:

```bash
python3 scripts/make_fixture.py data/fixtures/sample.mp4 20
```

## Working on the pipeline

Inspect intermediate files by raising retention and reading the job workspace:

```
data/jobs/<job-id>/
  clip.mp4        extracted range
  cut.mp4         after silence removal
  audio.wav       transcription input
  transcript.json word timings
  captions.ass    generated subtitles
  hook.png        hook overlay
```

To see the exact FFmpeg command, run with `LOG_LEVEL=DEBUG`.

To check a composition claim, extract frames and measure:

```bash
ffmpeg -ss 2 -i data/outputs/<job-id>.mp4 -frames:v 1 /tmp/f.png
ffprobe -v error -show_streams data/outputs/<job-id>.mp4
```

## Conventions

- No HEX literals outside `app/colors.py`
- No magic numbers for layout - use `ViewportSettings`
- FFmpeg gets argument arrays, never shell strings
- User-facing errors are sentences that say what to do; details go to the log
- New settings need a model field, a default, validation, and a test

## Adding things

**A highlight colour:** add to `PALETTE` in `app/colors.py`. The UI picks it up
from `/api/config` automatically.

**An output preset:** add to `OUTPUT_PRESETS` in `app/models.py` and to the
preset buttons in `app/web/index.html`.

**A transcription backend:** implement the `TranscriptionBackend` protocol in
`app/processor/transcribe/__init__.py` and register it in `get_backend()`.

**An effect type:** add to `EFFECT_TYPES`, handle it in `_segment_values()` in
`app/processor/effects.py`. `test_every_declared_effect_type_resolves` will
cover it automatically.
