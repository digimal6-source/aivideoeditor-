"""On-screen hook rendering.

The hook is drawn once with Pillow into a full-canvas transparent PNG and then
overlaid on the final composition. Doing it this way (rather than with FFmpeg's
`drawtext`) buys three things the spec asks for:

* per-word highlight colours without any markup in the user's text;
* real word-wrapping measured against the actual font metrics;
* the hook is composited *after* the video layer, so it can never be affected by
  the zoom/pan applied behind the square viewport.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .. import colors
from ..models import HookSettings, ViewportSettings


@dataclass(frozen=True)
class HookLayout:
    lines: tuple[tuple[tuple[str, bool], ...], ...]
    line_height: int
    total_height: int
    top: int
    overflowed: bool


def _load_font(font_path: Path, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(font_path), size)


def _text_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> int:
    return int(draw.textlength(text, font=font))


def layout_hook(
    draw: ImageDraw.ImageDraw,
    settings: HookSettings,
    font: ImageFont.FreeTypeFont,
    *,
    canvas_width: int,
    viewport: ViewportSettings,
) -> HookLayout:
    """Wrap the hook to the available width and place it above the viewport."""
    words = settings.words()
    flags = [index in settings.highlight_indices for index in range(len(words))]
    max_width = max(80, canvas_width - 2 * settings.margin_x)

    lines: list[list[tuple[str, bool]]] = []
    current: list[tuple[str, bool]] = []
    for word, highlighted in zip(words, flags):
        candidate = " ".join([w for w, _ in current] + [word])
        if current and _text_width(draw, candidate, font) > max_width:
            lines.append(current)
            current = [(word, highlighted)]
        else:
            current.append((word, highlighted))
    if current:
        lines.append(current)

    ascent, descent = font.getmetrics()
    line_height = int((ascent + descent) * settings.line_spacing)
    total_height = line_height * max(1, len(lines))

    if settings.vertical_anchor == "absolute" and settings.y is not None:
        top = settings.y
    elif settings.vertical_anchor == "canvas_top":
        top = settings.gap_above_viewport
    else:  # above_viewport
        top = viewport.y - settings.gap_above_viewport - total_height

    overflowed = top < 0
    top = max(0, top)
    return HookLayout(
        lines=tuple(tuple(line) for line in lines),
        line_height=line_height,
        total_height=total_height,
        top=top,
        overflowed=overflowed,
    )


def render_hook_png(
    settings: HookSettings,
    viewport: ViewportSettings,
    *,
    font_path: Path,
    destination: Path,
    canvas_width: int,
    canvas_height: int,
) -> tuple[Path, list[str]]:
    """Render the hook onto a transparent full-canvas PNG. Returns (path, warnings)."""
    warnings: list[str] = []
    image = Image.new("RGBA", (canvas_width, canvas_height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    font_size = settings.font_size
    font = _load_font(font_path, font_size)
    layout = layout_hook(draw, settings, font, canvas_width=canvas_width, viewport=viewport)

    # Shrink to fit rather than colliding with the viewport or the canvas top.
    guard = 0
    while layout.overflowed and font_size > 20 and guard < 40:
        font_size -= 2
        guard += 1
        font = _load_font(font_path, font_size)
        layout = layout_hook(draw, settings, font, canvas_width=canvas_width, viewport=viewport)
    if layout.overflowed:
        warnings.append(
            "The hook is too long to fit above the square viewport and was clamped "
            "to the top of the canvas. Shorten it or lower the viewport."
        )
    if font_size != settings.font_size:
        warnings.append(
            f"The hook font size was reduced from {settings.font_size}px to {font_size}px so "
            "the text fits above the video."
        )

    base_rgb = colors.hex_to_rgb(settings.color)
    accent_rgb = colors.hex_to_rgb(settings.highlight_color)
    outline_rgb = colors.hex_to_rgb(settings.outline_color)
    shadow_rgb = colors.hex_to_rgb(settings.shadow_color)
    space_width = _text_width(draw, " ", font)

    for row, line in enumerate(layout.lines):
        line_text = " ".join(word for word, _ in line)
        line_width = _text_width(draw, line_text, font)
        if settings.align == "left":
            x = settings.margin_x
        elif settings.align == "right":
            x = canvas_width - settings.margin_x - line_width
        else:
            x = (canvas_width - line_width) // 2
        y = layout.top + row * layout.line_height

        for word, highlighted in line:
            fill = accent_rgb if highlighted else base_rgb
            if settings.shadow_offset:
                draw.text(
                    (x + settings.shadow_offset, y + settings.shadow_offset),
                    word,
                    font=font,
                    fill=(*shadow_rgb, 190),
                )
            draw.text(
                (x, y),
                word,
                font=font,
                fill=(*fill, 255),
                stroke_width=int(round(settings.outline_width)),
                stroke_fill=(*outline_rgb, 255) if settings.outline_width else None,
            )
            x += _text_width(draw, word, font) + space_width

    destination.parent.mkdir(parents=True, exist_ok=True)
    image.save(destination, format="PNG")
    return destination, warnings
