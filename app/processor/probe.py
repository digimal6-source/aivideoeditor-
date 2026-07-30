"""Media metadata extraction via ffprobe."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ..errors import ProcessingError
from .ffmpeg import FFmpeg


def _parse_rate(value: str | None) -> float:
    if not value or "/" not in str(value):
        try:
            return float(value) if value else 0.0
        except (TypeError, ValueError):
            return 0.0
    num, _, den = str(value).partition("/")
    try:
        numerator, denominator = float(num), float(den)
    except ValueError:
        return 0.0
    return numerator / denominator if denominator else 0.0


@dataclass(frozen=True)
class MediaInfo:
    duration: float
    width: int
    height: int
    fps: float
    video_codec: str
    audio_codec: str | None
    has_audio: bool
    has_video: bool
    size_bytes: int

    def to_dict(self) -> dict:
        return {
            "duration": round(self.duration, 3),
            "width": self.width,
            "height": self.height,
            "fps": round(self.fps, 3),
            "videoCodec": self.video_codec,
            "audioCodec": self.audio_codec,
            "hasAudio": self.has_audio,
            "hasVideo": self.has_video,
            "sizeBytes": self.size_bytes,
        }


def probe(ffmpeg: FFmpeg, path: Path) -> MediaInfo:
    raw = ffmpeg.run_probe(
        [
            "-v", "error",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            str(path),
        ]
    )
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProcessingError("That video could not be analysed.", detail=raw) from exc

    streams = data.get("streams") or []
    fmt = data.get("format") or {}
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    if video is None:
        raise ProcessingError("That file does not contain a video track.")

    duration = 0.0
    for candidate in (fmt.get("duration"), video.get("duration")):
        try:
            duration = float(candidate)
            if duration > 0:
                break
        except (TypeError, ValueError):
            continue

    fps = _parse_rate(video.get("avg_frame_rate")) or _parse_rate(video.get("r_frame_rate"))

    return MediaInfo(
        duration=duration,
        width=int(video.get("width") or 0),
        height=int(video.get("height") or 0),
        fps=fps,
        video_codec=str(video.get("codec_name") or "unknown"),
        audio_codec=str(audio.get("codec_name")) if audio else None,
        has_audio=audio is not None,
        has_video=True,
        size_bytes=int(float(fmt.get("size") or 0)),
    )
