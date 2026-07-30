"""Centralised colour system.

Every colour used by the renderer flows through this module. Adding or removing
a preset is a one-line change here and needs no edits anywhere else.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .errors import ValidationError

_HEX_RE = re.compile(r"^#?([0-9a-fA-F]{6}|[0-9a-fA-F]{3})$")


@dataclass(frozen=True)
class ColorPreset:
    id: str
    name: str
    hex: str

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "hex": self.hex}


#: Ordered palette shown in the UI. The first entry is the default.
HIGHLIGHT_PRESETS: tuple[ColorPreset, ...] = (
    ColorPreset("electric-violet", "Electric Violet", "#8B5CF6"),
    ColorPreset("neon-purple", "Neon Purple", "#A855F7"),
    ColorPreset("electric-blue", "Electric Blue", "#3B82F6"),
    ColorPreset("cyan", "Cyan", "#06B6D4"),
    ColorPreset("lime", "Lime", "#A3E635"),
    ColorPreset("amber", "Amber", "#F59E0B"),
    ColorPreset("orange", "Orange", "#F97316"),
    ColorPreset("pink", "Pink", "#EC4899"),
    ColorPreset("red", "Red", "#EF4444"),
    ColorPreset("emerald", "Emerald", "#10B981"),
)

DEFAULT_HIGHLIGHT_COLOR = HIGHLIGHT_PRESETS[0].hex  # Electric Violet #8B5CF6
DEFAULT_TEXT_COLOR = "#FFFFFF"
DEFAULT_OUTLINE_COLOR = "#000000"
DEFAULT_SHADOW_COLOR = "#000000"
DEFAULT_BACKGROUND_COLOR = "#000000"


def preset_by_id(preset_id: str) -> ColorPreset | None:
    for preset in HIGHLIGHT_PRESETS:
        if preset.id == preset_id:
            return preset
    return None


def palette() -> list[dict]:
    return [preset.to_dict() for preset in HIGHLIGHT_PRESETS]


def is_valid_hex(value: str) -> bool:
    return bool(isinstance(value, str) and _HEX_RE.match(value.strip()))


def normalize_hex(value: str, *, field: str = "color") -> str:
    """Normalise any accepted colour input to an upper-case ``#RRGGBB`` string.

    Accepts ``#abc``, ``abc``, ``#AABBCC``, ``aabbcc`` and known preset ids.
    """
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be a HEX colour string.")
    text = value.strip()
    if not text:
        raise ValidationError(f"{field} is required.")

    preset = preset_by_id(text)
    if preset is not None:
        return preset.hex

    match = _HEX_RE.match(text)
    if not match:
        raise ValidationError(
            f"{field} '{value}' is not a valid HEX colour. Use a format like #8B5CF6."
        )
    digits = match.group(1)
    if len(digits) == 3:
        digits = "".join(ch * 2 for ch in digits)
    return "#" + digits.upper()


def hex_to_rgb(value: str) -> tuple[int, int, int]:
    hexcode = normalize_hex(value)[1:]
    return (int(hexcode[0:2], 16), int(hexcode[2:4], 16), int(hexcode[4:6], 16))


def hex_to_rgba(value: str, alpha: int = 255) -> tuple[int, int, int, int]:
    r, g, b = hex_to_rgb(value)
    return (r, g, b, max(0, min(255, int(alpha))))


def hex_to_ass(value: str, alpha: int = 0) -> str:
    """Convert ``#RRGGBB`` to an ASS colour literal ``&HAABBGGRR``.

    ASS alpha is inverted: 0x00 is fully opaque, 0xFF fully transparent.
    """
    r, g, b = hex_to_rgb(value)
    alpha = max(0, min(255, int(alpha)))
    return f"&H{alpha:02X}{b:02X}{g:02X}{r:02X}"


def hex_to_ffmpeg(value: str) -> str:
    """Convert ``#RRGGBB`` to the ``0xRRGGBB`` literal FFmpeg expects."""
    return "0x" + normalize_hex(value)[1:]
