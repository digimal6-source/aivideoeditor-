"""Timestamp parsing, formatting and clip validation.

Accepted input formats:
    ``HH:MM:SS``, ``HH:MM:SS.mmm``, ``MM:SS``, ``MM:SS.mmm``, ``SS``, ``SS.mmm``
Plain numbers (int/float) are treated as seconds.
"""

from __future__ import annotations

import re
from typing import Union

from .errors import ValidationError

_NUMERIC = re.compile(r"^\d+(\.\d+)?$")
_CLOCK = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{1,2}(?:\.\d+)?)$")

# Guard rails: a short-form clip longer than this is almost certainly a mistake.
MAX_CLIP_SECONDS = 600.0
MIN_CLIP_SECONDS = 0.5


def parse_timecode(value: Union[str, int, float], *, field: str = "timestamp") -> float:
    """Parse a timestamp into seconds (float). Raises ValidationError."""
    if isinstance(value, bool):  # bool is an int subclass; reject explicitly
        raise ValidationError(f"{field} must be a timestamp, not a boolean.")
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds < 0:
            raise ValidationError(f"{field} cannot be negative.")
        return round(seconds, 3)

    if not isinstance(value, str):
        raise ValidationError(f"{field} must be a string or a number.")

    text = value.strip()
    if not text:
        raise ValidationError(f"{field} is required.")

    if _NUMERIC.match(text):
        return round(float(text), 3)

    match = _CLOCK.match(text)
    if not match:
        raise ValidationError(
            f"{field} '{value}' is not a valid timestamp. Use HH:MM:SS, MM:SS or seconds."
        )

    hours = int(match.group(1) or 0)
    minutes = int(match.group(2))
    seconds = float(match.group(3))
    if minutes > 59:
        raise ValidationError(f"{field} '{value}' has more than 59 minutes.")
    if seconds >= 60:
        raise ValidationError(f"{field} '{value}' has more than 59 seconds.")
    return round(hours * 3600 + minutes * 60 + seconds, 3)


def format_timecode(seconds: float, *, millis: bool = False) -> str:
    """Format seconds as HH:MM:SS(.mmm)."""
    if seconds < 0:
        raise ValidationError("Cannot format a negative timestamp.")
    total_ms = int(round(seconds * 1000))
    ms = total_ms % 1000
    total = total_ms // 1000
    hh, rem = divmod(total, 3600)
    mm, ss = divmod(rem, 60)
    base = f"{hh:02d}:{mm:02d}:{ss:02d}"
    return f"{base}.{ms:03d}" if millis else base


def format_ass_time(seconds: float) -> str:
    """Format seconds as an ASS timestamp: H:MM:SS.cc (centiseconds)."""
    seconds = max(0.0, seconds)
    total_cs = int(round(seconds * 100))
    cs = total_cs % 100
    total = total_cs // 100
    hh, rem = divmod(total, 3600)
    mm, ss = divmod(rem, 60)
    return f"{hh:d}:{mm:02d}:{ss:02d}.{cs:02d}"


def validate_clip(start: float, end: float, duration: float | None = None) -> tuple[float, float]:
    """Validate a clip range against the source duration. Returns (start, end)."""
    if start < 0:
        raise ValidationError("Clip start cannot be negative.")
    if end <= start:
        raise ValidationError("Clip end must be greater than clip start.")

    length = end - start
    if length < MIN_CLIP_SECONDS:
        raise ValidationError(
            f"Clip is too short ({length:.2f}s). Minimum is {MIN_CLIP_SECONDS:g}s."
        )
    if length > MAX_CLIP_SECONDS:
        raise ValidationError(
            f"Clip is too long ({length:.1f}s). Maximum is {MAX_CLIP_SECONDS:g}s."
        )

    if duration is not None and duration > 0:
        # Allow a small tolerance: container durations are frequently rounded.
        if start >= duration:
            raise ValidationError(
                f"Clip start ({format_timecode(start)}) is beyond the video duration "
                f"({format_timecode(duration)})."
            )
        if end > duration + 0.25:
            raise ValidationError(
                f"Clip end ({format_timecode(end)}) is beyond the video duration "
                f"({format_timecode(duration)})."
            )
        end = min(end, duration)

    return round(start, 3), round(end, 3)
