"""Typed configuration and job models.

These dataclasses are the contract between the HTTP layer, the preset store and
the render pipeline. Nothing downstream passes around loose dictionaries: every
request body is parsed and validated here exactly once.

Only the standard library is used so the models can be imported by the API
process, the worker process and the test-suite without any install step.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Sequence

from . import colors
from .errors import ValidationError
from .timecode import parse_timecode, validate_clip

# ---------------------------------------------------------------------------
# coercion helpers
# ---------------------------------------------------------------------------


def _dict(data: Any, name: str) -> dict:
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValidationError(f"'{name}' must be an object.")
    return data


def _num(data: dict, key: str, default: float, *, lo: float, hi: float, name: str) -> float:
    raw = data.get(key, default)
    if raw is None or raw == "":
        return float(default)
    if isinstance(raw, bool):
        raise ValidationError(f"{name} must be a number.")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValidationError(f"{name} must be a number.") from None
    if value != value:  # NaN
        raise ValidationError(f"{name} must be a number.")
    if not (lo <= value <= hi):
        raise ValidationError(f"{name} must be between {lo:g} and {hi:g} (got {value:g}).")
    return value


def _int(data: dict, key: str, default: int, *, lo: int, hi: int, name: str) -> int:
    return int(round(_num(data, key, default, lo=lo, hi=hi, name=name)))


def _bool(data: dict, key: str, default: bool) -> bool:
    raw = data.get(key, default)
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(raw, (int, float)):
        return bool(raw)
    return default


def _str(data: dict, key: str, default: str, *, name: str, max_len: int = 500) -> str:
    raw = data.get(key, default)
    if raw is None:
        return default
    if not isinstance(raw, str):
        raise ValidationError(f"{name} must be text.")
    text = raw.strip()
    if len(text) > max_len:
        raise ValidationError(f"{name} is too long (max {max_len} characters).")
    return text


def _choice(data: dict, key: str, default: str, allowed: Sequence[str], *, name: str) -> str:
    value = _str(data, key, default, name=name, max_len=60).lower().replace("-", "_")
    if value not in allowed:
        raise ValidationError(f"{name} must be one of: {', '.join(allowed)}.")
    return value


def _color(data: dict, key: str, default: str, *, name: str) -> str:
    raw = data.get(key)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return colors.normalize_hex(default, field=name)
    return colors.normalize_hex(raw, field=name)


# ---------------------------------------------------------------------------
# viewport
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ViewportSettings:
    """The fixed 1:1 square window the video is clipped to.

    ``x``/``y`` are the top-left corner of the square inside the 1080x1920
    canvas. ``x=None`` means horizontally centred. Nothing else in the codebase
    is allowed to hardcode these numbers.
    """

    size: int = 1000
    x: int | None = None
    y: int = 620
    background_color: str = colors.DEFAULT_BACKGROUND_COLOR
    corner_radius: int = 0
    supersample: int = 2

    @classmethod
    def from_dict(cls, data: Any) -> "ViewportSettings":
        d = _dict(data, "viewport")
        size = _int(d, "size", 1000, lo=200, hi=2160, name="Viewport size")
        raw_x = d.get("x", None)
        x = None if raw_x in (None, "", "center") else _int(d, "x", 0, lo=-2160, hi=2160, name="Viewport x")
        return cls(
            size=size,
            x=x,
            y=_int(d, "y", 620, lo=0, hi=3840, name="Viewport y"),
            background_color=_color(d, "backgroundColor", colors.DEFAULT_BACKGROUND_COLOR, name="Background colour"),
            corner_radius=_int(d, "cornerRadius", 0, lo=0, hi=400, name="Corner radius"),
            supersample=_int(d, "supersample", 2, lo=1, hi=3, name="Supersample"),
        )

    def resolved_x(self, canvas_width: int) -> int:
        if self.x is None:
            return (canvas_width - self.size) // 2
        return self.x

    def validate_against_canvas(self, canvas_width: int, canvas_height: int) -> None:
        if self.size > canvas_width:
            raise ValidationError(
                f"Viewport size ({self.size}px) is wider than the canvas ({canvas_width}px)."
            )
        if self.y + self.size > canvas_height:
            raise ValidationError(
                f"Viewport (y={self.y}, size={self.size}) does not fit inside the "
                f"{canvas_width}x{canvas_height} canvas."
            )
        x = self.resolved_x(canvas_width)
        if x < 0 or x + self.size > canvas_width:
            raise ValidationError("Viewport is horizontally outside the canvas.")

    def to_dict(self) -> dict:
        return {
            "size": self.size,
            "x": self.x,
            "y": self.y,
            "backgroundColor": self.background_color,
            "cornerRadius": self.corner_radius,
            "supersample": self.supersample,
        }


# ---------------------------------------------------------------------------
# hook
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HookSettings:
    enabled: bool = True
    text: str = ""
    font_id: str = ""
    font_size: int = 74
    color: str = colors.DEFAULT_TEXT_COLOR
    highlight_color: str = colors.DEFAULT_HIGHLIGHT_COLOR
    highlight_indices: tuple[int, ...] = ()
    align: str = "center"
    vertical_anchor: str = "above_viewport"
    y: int | None = None
    margin_x: int = 70
    gap_above_viewport: int = 60
    line_spacing: float = 1.15
    uppercase: bool = False
    outline_width: float = 0.0
    outline_color: str = colors.DEFAULT_OUTLINE_COLOR
    shadow_offset: int = 0
    shadow_color: str = colors.DEFAULT_SHADOW_COLOR

    @classmethod
    def from_dict(cls, data: Any) -> "HookSettings":
        d = _dict(data, "hook")
        text = _str(d, "text", "", name="Hook text", max_len=300)
        words = text.split()
        indices = _parse_highlight_indices(d, words)
        raw_y = d.get("y")
        y = None if raw_y in (None, "", "auto") else _int(d, "y", 0, lo=0, hi=3840, name="Hook y")
        return cls(
            enabled=_bool(d, "enabled", True),
            text=text,
            font_id=_str(d, "fontId", "", name="Hook font", max_len=120),
            font_size=_int(d, "fontSize", 74, lo=16, hi=300, name="Hook font size"),
            color=_color(d, "color", colors.DEFAULT_TEXT_COLOR, name="Hook colour"),
            highlight_color=_color(d, "highlightColor", colors.DEFAULT_HIGHLIGHT_COLOR, name="Highlight colour"),
            highlight_indices=indices,
            align=_choice(d, "align", "center", ("left", "center", "right"), name="Hook alignment"),
            vertical_anchor=_choice(
                d, "verticalAnchor", "above_viewport", ("above_viewport", "canvas_top", "absolute"),
                name="Hook anchor",
            ),
            y=y,
            margin_x=_int(d, "marginX", 70, lo=0, hi=500, name="Hook side margin"),
            gap_above_viewport=_int(d, "gapAboveViewport", 60, lo=0, hi=800, name="Hook gap"),
            line_spacing=_num(d, "lineSpacing", 1.15, lo=0.8, hi=2.5, name="Hook line spacing"),
            uppercase=_bool(d, "uppercase", False),
            outline_width=_num(d, "outlineWidth", 0.0, lo=0.0, hi=10.0, name="Hook outline"),
            outline_color=_color(d, "outlineColor", colors.DEFAULT_OUTLINE_COLOR, name="Hook outline colour"),
            shadow_offset=_int(d, "shadowOffset", 0, lo=0, hi=40, name="Hook shadow offset"),
            shadow_color=_color(d, "shadowColor", colors.DEFAULT_SHADOW_COLOR, name="Hook shadow colour"),
        )

    @property
    def is_renderable(self) -> bool:
        """A hook is only drawn when it is enabled *and* has text."""
        return self.enabled and bool(self.text.strip())

    def words(self) -> list[str]:
        text = self.text.upper() if self.uppercase else self.text
        return text.split()

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "text": self.text,
            "fontId": self.font_id,
            "fontSize": self.font_size,
            "color": self.color,
            "highlightColor": self.highlight_color,
            "highlightIndices": list(self.highlight_indices),
            "align": self.align,
            "verticalAnchor": self.vertical_anchor,
            "y": self.y,
            "marginX": self.margin_x,
            "gapAboveViewport": self.gap_above_viewport,
            "lineSpacing": self.line_spacing,
            "uppercase": self.uppercase,
            "outlineWidth": self.outline_width,
            "outlineColor": self.outline_color,
            "shadowOffset": self.shadow_offset,
            "shadowColor": self.shadow_color,
        }


def _parse_highlight_indices(d: dict, words: Sequence[str]) -> tuple[int, ...]:
    """Resolve highlighted words from indices and/or literal words.

    The UI sends ``highlightIndices`` (produced by tapping words). ``highlightWords``
    is accepted as a convenience for API/preset use and matched case-insensitively.
    """
    found: set[int] = set()

    raw_indices = d.get("highlightIndices") or []
    if isinstance(raw_indices, (list, tuple)):
        for item in raw_indices:
            try:
                idx = int(item)
            except (TypeError, ValueError):
                raise ValidationError("Highlighted word indices must be whole numbers.") from None
            if 0 <= idx < len(words):
                found.add(idx)
    elif raw_indices:
        raise ValidationError("'highlightIndices' must be a list.")

    raw_words = d.get("highlightWords") or []
    if isinstance(raw_words, str):
        raw_words = [w for w in raw_words.split() if w]
    if isinstance(raw_words, (list, tuple)):
        wanted = {str(w).strip().lower().strip(".,!?:;\"'") for w in raw_words if str(w).strip()}
        for idx, word in enumerate(words):
            if word.lower().strip(".,!?:;\"'") in wanted:
                found.add(idx)

    return tuple(sorted(found))


# ---------------------------------------------------------------------------
# captions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CaptionSettings:
    enabled: bool = True
    font_id: str = ""
    font_size: int = 62
    bold: bool = True
    color: str = colors.DEFAULT_TEXT_COLOR
    outline_color: str = colors.DEFAULT_OUTLINE_COLOR
    #: 0..5 in user-friendly units; mapped to ASS pixels by ass_render.py
    outline_width: float = 0.5
    shadow_color: str = colors.DEFAULT_SHADOW_COLOR
    shadow_offset: float = 3.0
    shadow_strength: float = 1.0
    align: str = "center"
    vertical: str = "bottom"
    margin_x: int = 60
    margin_v: int = 90
    max_words_per_phrase: int = 3
    max_chars_per_phrase: int = 30
    uppercase: bool = True
    highlight_color: str = colors.DEFAULT_HIGHLIGHT_COLOR
    highlight_keywords: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, data: Any) -> "CaptionSettings":
        d = _dict(data, "captions")
        raw_keywords = d.get("highlightKeywords") or []
        if isinstance(raw_keywords, str):
            raw_keywords = [w for w in raw_keywords.replace(",", " ").split() if w]
        keywords = tuple(sorted({str(w).strip().lower() for w in raw_keywords if str(w).strip()}))
        return cls(
            enabled=_bool(d, "enabled", True),
            font_id=_str(d, "fontId", "", name="Caption font", max_len=120),
            font_size=_int(d, "fontSize", 62, lo=12, hi=200, name="Caption font size"),
            bold=_bool(d, "bold", True),
            color=_color(d, "color", colors.DEFAULT_TEXT_COLOR, name="Caption colour"),
            outline_color=_color(d, "outlineColor", colors.DEFAULT_OUTLINE_COLOR, name="Caption outline colour"),
            outline_width=_num(d, "outlineWidth", 0.5, lo=0.0, hi=5.0, name="Caption outline width"),
            shadow_color=_color(d, "shadowColor", colors.DEFAULT_SHADOW_COLOR, name="Caption shadow colour"),
            shadow_offset=_num(d, "shadowOffset", 3.0, lo=0.0, hi=20.0, name="Caption shadow offset"),
            shadow_strength=_num(d, "shadowStrength", 1.0, lo=0.0, hi=1.0, name="Caption shadow strength"),
            align=_choice(d, "align", "center", ("left", "center", "right"), name="Caption alignment"),
            vertical=_choice(d, "vertical", "bottom", ("top", "middle", "bottom"), name="Caption vertical position"),
            margin_x=_int(d, "marginX", 60, lo=0, hi=600, name="Caption side margin"),
            margin_v=_int(d, "marginV", 90, lo=0, hi=900, name="Caption vertical margin"),
            max_words_per_phrase=_int(d, "maxWordsPerPhrase", 3, lo=1, hi=12, name="Max words per phrase"),
            max_chars_per_phrase=_int(d, "maxCharsPerPhrase", 30, lo=6, hi=120, name="Max characters per phrase"),
            uppercase=_bool(d, "uppercase", True),
            highlight_color=_color(d, "highlightColor", colors.DEFAULT_HIGHLIGHT_COLOR, name="Caption highlight colour"),
            highlight_keywords=keywords,
        )

    def ass_alignment(self) -> int:
        """ASS \\an numpad alignment code."""
        row = {"bottom": 0, "middle": 3, "top": 6}[self.vertical]
        col = {"left": 1, "center": 2, "right": 3}[self.align]
        return row + col

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "fontId": self.font_id,
            "fontSize": self.font_size,
            "bold": self.bold,
            "color": self.color,
            "outlineColor": self.outline_color,
            "outlineWidth": self.outline_width,
            "shadowColor": self.shadow_color,
            "shadowOffset": self.shadow_offset,
            "shadowStrength": self.shadow_strength,
            "align": self.align,
            "vertical": self.vertical,
            "marginX": self.margin_x,
            "marginV": self.margin_v,
            "maxWordsPerPhrase": self.max_words_per_phrase,
            "maxCharsPerPhrase": self.max_chars_per_phrase,
            "uppercase": self.uppercase,
            "highlightColor": self.highlight_color,
            "highlightKeywords": list(self.highlight_keywords),
        }


# ---------------------------------------------------------------------------
# silence removal
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SilenceSettings:
    enabled: bool = True
    threshold_db: float = -34.0
    min_silence_duration: float = 0.45
    pad_before: float = 0.10
    pad_after: float = 0.15
    min_segment_duration: float = 0.20

    @classmethod
    def from_dict(cls, data: Any) -> "SilenceSettings":
        d = _dict(data, "silence")
        return cls(
            enabled=_bool(d, "enabled", True),
            threshold_db=_num(d, "thresholdDb", -34.0, lo=-90.0, hi=0.0, name="Silence threshold"),
            min_silence_duration=_num(d, "minSilenceDuration", 0.45, lo=0.05, hi=10.0, name="Minimum silence duration"),
            pad_before=_num(d, "padBefore", 0.10, lo=0.0, hi=2.0, name="Padding before speech"),
            pad_after=_num(d, "padAfter", 0.15, lo=0.0, hi=2.0, name="Padding after speech"),
            min_segment_duration=_num(d, "minSegmentDuration", 0.20, lo=0.05, hi=5.0, name="Minimum kept segment"),
        )

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "thresholdDb": self.threshold_db,
            "minSilenceDuration": self.min_silence_duration,
            "padBefore": self.pad_before,
            "padAfter": self.pad_after,
            "minSegmentDuration": self.min_segment_duration,
        }


# ---------------------------------------------------------------------------
# video effects
# ---------------------------------------------------------------------------

EFFECT_TYPES = (
    "normal",
    "zoom_in",
    "zoom_out",
    "pan_left",
    "pan_right",
    "pan_up",
    "pan_down",
)


@dataclass(frozen=True)
class VideoEffect:
    """A time-bounded transform applied to the video layer *before* clipping."""

    start: float
    end: float
    type: str = "normal"
    scale: float = 1.0
    scale_to: float | None = None
    pan_x: float = 0.0
    pan_y: float = 0.0

    @classmethod
    def from_dict(cls, data: Any) -> "VideoEffect":
        d = _dict(data, "effect")
        start = parse_timecode(d.get("start", 0), field="Effect start")
        end = parse_timecode(d.get("end", 0), field="Effect end")
        if end <= start:
            raise ValidationError("Each effect must end after it starts.")
        raw_to = d.get("scaleTo")
        return cls(
            start=start,
            end=end,
            type=_choice(d, "type", "normal", EFFECT_TYPES, name="Effect type"),
            scale=_num(d, "scale", 1.0, lo=1.0, hi=3.0, name="Effect scale"),
            scale_to=None if raw_to in (None, "") else _num(d, "scaleTo", 1.0, lo=1.0, hi=3.0, name="Effect end scale"),
            pan_x=_num(d, "panX", 0.0, lo=-1.0, hi=1.0, name="Effect pan X"),
            pan_y=_num(d, "panY", 0.0, lo=-1.0, hi=1.0, name="Effect pan Y"),
        )

    def to_dict(self) -> dict:
        return {
            "start": self.start,
            "end": self.end,
            "type": self.type,
            "scale": self.scale,
            "scaleTo": self.scale_to,
            "panX": self.pan_x,
            "panY": self.pan_y,
        }


@dataclass(frozen=True)
class EffectSettings:
    mode: str = "auto"
    base_scale: float = 1.0
    auto_zoom_amount: float = 0.06
    auto_cycle_seconds: float = 8.0
    auto_hold_seconds: float = 1.5
    effects: tuple[VideoEffect, ...] = ()

    @classmethod
    def from_dict(cls, data: Any) -> "EffectSettings":
        d = _dict(data, "effects")
        raw_effects = d.get("effects") or []
        if not isinstance(raw_effects, (list, tuple)):
            raise ValidationError("'effects' must be a list.")
        if len(raw_effects) > 200:
            raise ValidationError("Too many effects (max 200).")
        effects = tuple(VideoEffect.from_dict(item) for item in raw_effects)
        mode = _choice(d, "mode", "auto", ("none", "auto", "manual"), name="Effect mode")
        if mode == "manual" and not effects:
            mode = "none"
        return cls(
            mode=mode,
            base_scale=_num(d, "baseScale", 1.0, lo=1.0, hi=2.0, name="Base scale"),
            auto_zoom_amount=_num(d, "autoZoomAmount", 0.06, lo=0.0, hi=0.5, name="Auto zoom amount"),
            auto_cycle_seconds=_num(d, "autoCycleSeconds", 8.0, lo=2.0, hi=60.0, name="Auto zoom cycle"),
            auto_hold_seconds=_num(d, "autoHoldSeconds", 1.5, lo=0.0, hi=20.0, name="Auto zoom hold"),
            effects=effects,
        )

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "baseScale": self.base_scale,
            "autoZoomAmount": self.auto_zoom_amount,
            "autoCycleSeconds": self.auto_cycle_seconds,
            "autoHoldSeconds": self.auto_hold_seconds,
            "effects": [e.to_dict() for e in self.effects],
        }


# ---------------------------------------------------------------------------
# output
# ---------------------------------------------------------------------------

OUTPUT_PRESETS = {
    "1080p30": {"width": 1080, "height": 1920, "fps": 30},
    "1080p60": {"width": 1080, "height": 1920, "fps": 60},
    "720p30": {"width": 720, "height": 1280, "fps": 30},
}


@dataclass(frozen=True)
class OutputSettings:
    width: int = 1080
    height: int = 1920
    fps: int = 30
    crf: int = 19
    x264_preset: str = "medium"
    audio_bitrate_kbps: int = 192
    preset_id: str = "1080p30"

    @classmethod
    def from_dict(cls, data: Any) -> "OutputSettings":
        d = _dict(data, "output")
        preset_id = _str(d, "presetId", "", name="Output preset", max_len=40)
        base = OUTPUT_PRESETS.get(preset_id, OUTPUT_PRESETS["1080p30"])
        fps = _int(d, "fps", base["fps"], lo=1, hi=120, name="Output FPS")
        if fps not in (24, 25, 30, 50, 60):
            raise ValidationError("Output FPS must be one of 24, 25, 30, 50 or 60.")
        width = _int(d, "width", base["width"], lo=240, hi=2160, name="Output width")
        height = _int(d, "height", base["height"], lo=240, hi=3840, name="Output height")
        if width % 2 or height % 2:
            raise ValidationError("Output width and height must both be even numbers.")
        return cls(
            width=width,
            height=height,
            fps=fps,
            crf=_int(d, "crf", 19, lo=10, hi=35, name="CRF"),
            x264_preset=_choice(
                d, "x264Preset", "medium",
                ("ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow"),
                name="x264 preset",
            ),
            audio_bitrate_kbps=_int(d, "audioBitrateKbps", 192, lo=64, hi=320, name="Audio bitrate"),
            preset_id=preset_id or f"{height}p{fps}",
        )

    def to_dict(self) -> dict:
        return {
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "crf": self.crf,
            "x264Preset": self.x264_preset,
            "audioBitrateKbps": self.audio_bitrate_kbps,
            "presetId": self.preset_id,
        }


# ---------------------------------------------------------------------------
# transcription
# ---------------------------------------------------------------------------

TRANSCRIPTION_BACKENDS = ("faster_whisper", "manual", "none")


@dataclass(frozen=True)
class TranscriptionSettings:
    backend: str = "faster_whisper"
    model_size: str = "base"
    language: str = "auto"
    beam_size: int = 1
    manual_text: str = ""

    @classmethod
    def from_dict(cls, data: Any, *, default_backend: str = "faster_whisper") -> "TranscriptionSettings":
        d = _dict(data, "transcription")
        return cls(
            backend=_choice(d, "backend", default_backend, TRANSCRIPTION_BACKENDS, name="Transcription backend"),
            model_size=_choice(
                d, "modelSize", "base",
                ("tiny", "tiny_en", "base", "base_en", "small", "small_en", "medium"),
                name="Whisper model size",
            ),
            language=_str(d, "language", "auto", name="Language", max_len=12) or "auto",
            beam_size=_int(d, "beamSize", 1, lo=1, hi=10, name="Beam size"),
            manual_text=_str(d, "manualText", "", name="Manual caption text", max_len=20000),
        )

    def whisper_model_name(self) -> str:
        return self.model_size.replace("_en", ".en")

    def to_dict(self) -> dict:
        return {
            "backend": self.backend,
            "modelSize": self.model_size,
            "language": self.language,
            "beamSize": self.beam_size,
            "manualText": self.manual_text,
        }


# ---------------------------------------------------------------------------
# clip + render request
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClipSelection:
    start: float
    end: float

    @property
    def duration(self) -> float:
        return round(self.end - self.start, 3)

    @classmethod
    def from_dict(cls, data: Any, *, source_duration: float | None = None) -> "ClipSelection":
        d = _dict(data, "clip")
        if "start" not in d or "end" not in d:
            raise ValidationError("Clip start and end are required.")
        start = parse_timecode(d["start"], field="Clip start")
        end = parse_timecode(d["end"], field="Clip end")
        start, end = validate_clip(start, end, source_duration)
        return cls(start=start, end=end)

    def to_dict(self) -> dict:
        return {"start": self.start, "end": self.end, "duration": self.duration}


@dataclass(frozen=True)
class RenderRequest:
    source_id: str
    clip: ClipSelection
    viewport: ViewportSettings
    hook: HookSettings
    captions: CaptionSettings
    silence: SilenceSettings
    effects: EffectSettings
    output: OutputSettings
    transcription: TranscriptionSettings

    @classmethod
    def from_dict(
        cls,
        data: Any,
        *,
        source_duration: float | None = None,
        default_transcription_backend: str = "faster_whisper",
    ) -> "RenderRequest":
        d = _dict(data, "request")
        source_id = _str(d, "sourceId", "", name="Source id", max_len=80)
        if not source_id:
            raise ValidationError("Upload a video before generating.")
        viewport = ViewportSettings.from_dict(d.get("viewport"))
        output = OutputSettings.from_dict(d.get("output"))
        viewport.validate_against_canvas(output.width, output.height)
        return cls(
            source_id=source_id,
            clip=ClipSelection.from_dict(d.get("clip"), source_duration=source_duration),
            viewport=viewport,
            hook=HookSettings.from_dict(d.get("hook")),
            captions=CaptionSettings.from_dict(d.get("captions")),
            silence=SilenceSettings.from_dict(d.get("silence")),
            effects=EffectSettings.from_dict(d.get("effects")),
            output=output,
            transcription=TranscriptionSettings.from_dict(
                d.get("transcription"), default_backend=default_transcription_backend
            ),
        )

    def to_dict(self) -> dict:
        return {
            "sourceId": self.source_id,
            "clip": self.clip.to_dict(),
            "viewport": self.viewport.to_dict(),
            "hook": self.hook.to_dict(),
            "captions": self.captions.to_dict(),
            "silence": self.silence.to_dict(),
            "effects": self.effects.to_dict(),
            "output": self.output.to_dict(),
            "transcription": self.transcription.to_dict(),
        }


# ---------------------------------------------------------------------------
# presets
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Preset:
    id: str
    name: str
    viewport: ViewportSettings
    hook: HookSettings
    captions: CaptionSettings
    silence: SilenceSettings
    effects: EffectSettings
    output: OutputSettings
    transcription: TranscriptionSettings
    built_in: bool = False
    created_at: float = field(default_factory=time.time)

    @classmethod
    def from_dict(cls, data: Any) -> "Preset":
        d = _dict(data, "preset")
        name = _str(d, "name", "", name="Preset name", max_len=60)
        if not name:
            raise ValidationError("Preset name is required.")
        return cls(
            id=_str(d, "id", "", name="Preset id", max_len=80) or f"preset-{uuid.uuid4().hex[:8]}",
            name=name,
            viewport=ViewportSettings.from_dict(d.get("viewport")),
            hook=HookSettings.from_dict(d.get("hook")),
            captions=CaptionSettings.from_dict(d.get("captions")),
            silence=SilenceSettings.from_dict(d.get("silence")),
            effects=EffectSettings.from_dict(d.get("effects")),
            output=OutputSettings.from_dict(d.get("output")),
            transcription=TranscriptionSettings.from_dict(d.get("transcription")),
            built_in=_bool(d, "builtIn", False),
            created_at=float(d["createdAt"]) if d.get("createdAt") is not None else time.time(),
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "builtIn": self.built_in,
            "createdAt": self.created_at,
            "viewport": self.viewport.to_dict(),
            "hook": self.hook.to_dict(),
            "captions": self.captions.to_dict(),
            "silence": self.silence.to_dict(),
            "effects": self.effects.to_dict(),
            "output": self.output.to_dict(),
            "transcription": self.transcription.to_dict(),
        }


def default_preset() -> Preset:
    """The built-in 'My Default' preset described in the product spec."""
    return Preset(
        id="my-default",
        name="My Default",
        viewport=ViewportSettings(),
        hook=HookSettings(font_id="rubik-bold"),
        captions=CaptionSettings(font_id="indivisible"),
        silence=SilenceSettings(),
        effects=EffectSettings(),
        output=OutputSettings(),
        transcription=TranscriptionSettings(),
        built_in=True,
        created_at=0.0,
    )


# ---------------------------------------------------------------------------
# jobs
# ---------------------------------------------------------------------------


class JobStage(str, Enum):
    QUEUED = "queued"
    ANALYZING = "analyzing"
    EXTRACTING = "extracting"
    DETECTING_SILENCE = "detecting_silence"
    REMOVING_SILENCE = "removing_silence"
    TRANSCRIBING = "transcribing"
    BUILDING_CAPTIONS = "building_captions"
    RENDERING_HOOK = "rendering_hook"
    COMPOSITING = "compositing"
    ENCODING = "encoding"
    FINALIZING = "finalizing"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


STAGE_LABELS = {
    JobStage.QUEUED: "Queued",
    JobStage.ANALYZING: "Analyzing video",
    JobStage.EXTRACTING: "Extracting clip",
    JobStage.DETECTING_SILENCE: "Detecting speech",
    JobStage.REMOVING_SILENCE: "Removing silence",
    JobStage.TRANSCRIBING: "Transcribing audio",
    JobStage.BUILDING_CAPTIONS: "Generating captions",
    JobStage.RENDERING_HOOK: "Rendering hook",
    JobStage.COMPOSITING: "Compositing square viewport",
    JobStage.ENCODING: "Rendering final video",
    JobStage.FINALIZING: "Finalizing",
    JobStage.DONE: "Done",
    JobStage.FAILED: "Failed",
    JobStage.CANCELLED: "Cancelled",
}

#: Nominal share of total work per stage, used to turn per-stage progress into
#: an overall percentage. These are weights, not fabricated timers: the overall
#: value only advances when a stage actually reports progress.
STAGE_WEIGHTS = {
    JobStage.QUEUED: 0.0,
    JobStage.ANALYZING: 0.02,
    JobStage.EXTRACTING: 0.10,
    JobStage.DETECTING_SILENCE: 0.05,
    JobStage.REMOVING_SILENCE: 0.10,
    JobStage.TRANSCRIBING: 0.28,
    JobStage.BUILDING_CAPTIONS: 0.02,
    JobStage.RENDERING_HOOK: 0.03,
    JobStage.COMPOSITING: 0.0,
    JobStage.ENCODING: 0.38,
    JobStage.FINALIZING: 0.02,
}


@dataclass
class RenderStatus:
    stage: JobStage = JobStage.QUEUED
    stage_progress: float | None = 0.0
    overall_progress: float = 0.0
    message: str = "Queued"
    determinate: bool = True
    error: str | None = None
    error_code: str | None = None

    def to_dict(self) -> dict:
        return {
            "stage": self.stage.value,
            "stageLabel": STAGE_LABELS.get(self.stage, self.stage.value),
            "stageProgress": self.stage_progress,
            "overallProgress": round(self.overall_progress, 4),
            "message": self.message,
            "determinate": self.determinate,
            "error": self.error,
            "errorCode": self.error_code,
        }


@dataclass
class RenderJob:
    id: str
    request: RenderRequest
    status: RenderStatus = field(default_factory=RenderStatus)
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    output_path: str | None = None
    output_bytes: int | None = None
    output_meta: dict | None = None
    caption_count: int = 0
    removed_silence_seconds: float = 0.0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "status": self.status.to_dict(),
            "createdAt": self.created_at,
            "startedAt": self.started_at,
            "finishedAt": self.finished_at,
            "hasOutput": bool(self.output_path),
            "outputBytes": self.output_bytes,
            "outputMeta": self.output_meta,
            "captionCount": self.caption_count,
            "removedSilenceSeconds": round(self.removed_silence_seconds, 2),
            "warnings": list(self.warnings),
            "downloadUrl": f"/api/jobs/{self.id}/download" if self.output_path else None,
            "previewUrl": f"/api/jobs/{self.id}/preview" if self.output_path else None,
        }
