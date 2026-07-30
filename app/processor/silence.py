"""Silence removal built on FFmpeg's `silencedetect` filter.

Detection and cutting are deliberately separated so the interval maths can be
unit-tested without touching FFmpeg at all.

Audio/video sync is guaranteed by cutting both streams with the *same* interval
list inside a single `filter_complex` (trim + atrim + concat), producing one
re-encode with no timestamp drift.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ..models import SilenceSettings
from .ffmpeg import FFmpeg, ProgressCallback

_SILENCE_START = re.compile(r"silence_start:\s*(-?\d+(?:\.\d+)?)")
_SILENCE_END = re.compile(r"silence_end:\s*(-?\d+(?:\.\d+)?)")

Interval = tuple[float, float]

#: FFmpeg's `concat` filter is fed one input pair per kept segment, and very
#: long filter graphs become fragile. Beyond this we keep the longest segments.
MAX_SEGMENTS = 120


@dataclass(frozen=True)
class SilenceResult:
    path: Path
    kept: tuple[Interval, ...]
    original_duration: float
    new_duration: float

    @property
    def removed_seconds(self) -> float:
        return max(0.0, self.original_duration - self.new_duration)

    @property
    def changed(self) -> bool:
        return len(self.kept) > 1 or self.removed_seconds > 0.05


def parse_silence_log(log: str) -> list[Interval]:
    """Turn silencedetect stderr output into (start, end) silence intervals."""
    starts = [float(m) for m in _SILENCE_START.findall(log)]
    ends = [float(m) for m in _SILENCE_END.findall(log)]
    intervals: list[Interval] = []
    for index, start in enumerate(starts):
        end = ends[index] if index < len(ends) else None
        if end is None or end <= start:
            continue
        intervals.append((max(0.0, start), end))
    return intervals


def build_keep_intervals(
    silences: list[Interval],
    duration: float,
    settings: SilenceSettings,
) -> list[Interval]:
    """Invert the silence list into the ranges we keep, then pad and merge.

    Padding intentionally leaves a little breathing room around speech so the
    result keeps natural rhythm instead of sounding machine-gunned.
    """
    if duration <= 0:
        return []

    usable = [
        (start, end)
        for start, end in sorted(silences)
        if (end - start) >= settings.min_silence_duration
    ]
    if not usable:
        return [(0.0, duration)]

    keeps: list[Interval] = []
    cursor = 0.0
    for start, end in usable:
        if start > cursor:
            keeps.append((cursor, min(start, duration)))
        cursor = max(cursor, end)
    if cursor < duration:
        keeps.append((cursor, duration))

    padded: list[Interval] = []
    for start, end in keeps:
        padded_start = max(0.0, start - settings.pad_before)
        padded_end = min(duration, end + settings.pad_after)
        if padded_end > padded_start:
            padded.append((padded_start, padded_end))

    merged: list[Interval] = []
    for start, end in padded:
        if merged and start <= merged[-1][1] + 1e-3:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))

    kept = [(s, e) for s, e in merged if (e - s) >= settings.min_segment_duration]
    if not kept:
        # Everything looked like silence (e.g. a music-only clip). Keeping the
        # whole clip is far safer than producing an empty video.
        return [(0.0, duration)]

    if len(kept) > MAX_SEGMENTS:
        longest = sorted(kept, key=lambda iv: iv[1] - iv[0], reverse=True)[:MAX_SEGMENTS]
        kept = sorted(longest)
    return [(round(s, 3), round(e, 3)) for s, e in kept]


def detect_silences(
    ffmpeg: FFmpeg, *, source: Path, settings: SilenceSettings
) -> list[Interval]:
    log = ffmpeg.run(
        [
            "-i", str(source),
            "-af",
            f"silencedetect=noise={settings.threshold_db:g}dB:d={settings.min_silence_duration:g}",
            "-f", "null",
            "-",
        ],
        stage="detecting speech",
    )
    return parse_silence_log(log)


def _concat_graph(kept: list[Interval], with_audio: bool) -> str:
    parts: list[str] = []
    labels: list[str] = []
    for index, (start, end) in enumerate(kept):
        parts.append(
            f"[0:v]trim=start={start:.3f}:end={end:.3f},setpts=PTS-STARTPTS[v{index}]"
        )
        labels.append(f"[v{index}]")
        if with_audio:
            parts.append(
                f"[0:a]atrim=start={start:.3f}:end={end:.3f},asetpts=PTS-STARTPTS[a{index}]"
            )
            labels.append(f"[a{index}]")
    if with_audio:
        ordered = "".join(
            f"[v{i}][a{i}]" for i in range(len(kept))
        )
        parts.append(f"{ordered}concat=n={len(kept)}:v=1:a=1[vout][aout]")
    else:
        ordered = "".join(f"[v{i}]" for i in range(len(kept)))
        parts.append(f"{ordered}concat=n={len(kept)}:v=1:a=0[vout]")
    return ";".join(parts)


def remove_silence(
    ffmpeg: FFmpeg,
    *,
    source: Path,
    destination: Path,
    duration: float,
    has_audio: bool,
    settings: SilenceSettings,
    on_progress: ProgressCallback | None = None,
) -> SilenceResult:
    if not settings.enabled or not has_audio:
        return SilenceResult(source, ((0.0, duration),), duration, duration)

    silences = detect_silences(ffmpeg, source=source, settings=settings)
    kept = build_keep_intervals(silences, duration, settings)
    new_duration = sum(end - start for start, end in kept)

    if len(kept) == 1 and kept[0][0] <= 0.01 and abs(kept[0][1] - duration) <= 0.05:
        return SilenceResult(source, tuple(kept), duration, duration)

    args = [
        "-i", str(source),
        "-filter_complex", _concat_graph(kept, has_audio),
        "-map", "[vout]",
    ]
    if has_audio:
        args += ["-map", "[aout]", "-c:a", "aac", "-b:a", "192k"]
    args += [
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "18",
        "-pix_fmt", "yuv420p",
        str(destination),
    ]
    ffmpeg.run(
        args,
        stage="removing silence",
        expected_duration=new_duration,
        on_progress=on_progress,
    )
    return SilenceResult(destination, tuple(kept), duration, new_duration)
