"""Stage 1 of the pipeline: extract the user-selected time range.

The clip is re-encoded rather than stream-copied. Stream copying would snap the
start to the nearest keyframe, which makes the extracted range differ from the
timestamps the user typed. Correctness beats speed here.
"""

from __future__ import annotations

from pathlib import Path

from ..models import ClipSelection
from .ffmpeg import FFmpeg, ProgressCallback


def extract_clip(
    ffmpeg: FFmpeg,
    *,
    source: Path,
    destination: Path,
    clip: ClipSelection,
    has_audio: bool,
    on_progress: ProgressCallback | None = None,
) -> Path:
    args = [
        # Fast seek to just before the cut, then an accurate slow seek.
        "-ss", f"{max(0.0, clip.start):.3f}",
        "-i", str(source),
        "-t", f"{clip.duration:.3f}",
        "-map", "0:v:0",
    ]
    if has_audio:
        args += ["-map", "0:a:0?"]
    args += [
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "18",
        "-pix_fmt", "yuv420p",
    ]
    if has_audio:
        args += ["-c:a", "pcm_s16le" if destination.suffix == ".mkv" else "aac", "-b:a", "192k"]
    else:
        args += ["-an"]
    args += ["-movflags", "+faststart", str(destination)]

    ffmpeg.run(
        args,
        stage="extracting the clip",
        expected_duration=clip.duration,
        on_progress=on_progress,
    )
    return destination


def extract_audio(ffmpeg: FFmpeg, *, source: Path, destination: Path) -> Path:
    """Mono 16 kHz PCM - the format Whisper-family models expect."""
    ffmpeg.run(
        [
            "-i", str(source),
            "-vn",
            "-ac", "1",
            "-ar", "16000",
            "-c:a", "pcm_s16le",
            str(destination),
        ],
        stage="extracting audio",
    )
    return destination
