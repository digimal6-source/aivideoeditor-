"""The layer compositor - the heart of the application.

Layer stack, bottom to top, exactly as specified:

    1. 1080x1920 black canvas
    2. video layer, zoomed/panned  <- transform happens HERE
    3. fixed 1:1 square viewport   <- clips the transformed video
    4. caption layer               <- burned in while still square, so clipped too
    5. on-screen hook              <- composited last, above the square

The critical property is that the transform is applied to a frame that is
*already* the square working size, using ``zoompan``, whose output size is
constant. The viewport therefore never changes size, never zooms and never
moves - the pixels behind it move instead. Captions are burned in before the
square is placed on the canvas, so libass clips them to the square; the hook is
overlaid after, so it lives outside the square and is untouched by the zoom.

The square is worked on at ``size * supersample`` and downscaled at the end, so
zooming does not soften captions.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .. import colors
from ..models import OutputSettings, VideoEffect, ViewportSettings
from .effects import build_expressions, is_static
from .ffmpeg import FFmpeg, ProgressCallback


def _escape_filter_path(path: Path) -> str:
    """Escape a path for use inside an FFmpeg filter argument."""
    text = str(path)
    for char in ("\\", ":", "'", "[", "]", ",", ";"):
        text = text.replace(char, "\\" + char)
    return text


@dataclass(frozen=True)
class CompositionInputs:
    video: Path
    hook_png: Path | None
    ass_file: Path | None
    fonts_dir: Path
    has_audio: bool


def build_filter_graph(
    inputs: CompositionInputs,
    viewport: ViewportSettings,
    output: OutputSettings,
    timeline: list[VideoEffect],
) -> str:
    size = viewport.size
    work = size * max(1, viewport.supersample)
    fps = output.fps

    # --- video layer: normalise to a square working frame ---------------
    chain = [
        f"fps={fps}",
        f"scale={work}:{work}:force_original_aspect_ratio=increase:flags=bicubic",
        f"crop={work}:{work}",
    ]

    # --- zoom / pan, applied BEFORE the square viewport -----------------
    if not is_static(timeline):
        expressions = build_expressions(timeline, fps)
        chain.append(
            "zoompan="
            f"z='{expressions['z']}':"
            f"x='{expressions['x']}':"
            f"y='{expressions['y']}':"
            f"d=1:s={work}x{work}:fps={fps}"
        )

    chain.append(f"scale={size}:{size}:flags=bicubic")
    chain.append("setsar=1")

    # --- captions, burned in while the frame is still the square --------
    if inputs.ass_file is not None:
        chain.append(
            f"ass=filename='{_escape_filter_path(inputs.ass_file)}'"
            f":fontsdir='{_escape_filter_path(inputs.fonts_dir)}'"
        )

    background = colors.hex_to_ffmpeg(viewport.background_color)
    viewport_x = viewport.resolved_x(output.width)

    parts = [
        "[0:v]" + ",".join(chain) + "[sq]",
        f"color=c={background}:s={output.width}x{output.height}:r={fps}[bg]",
        f"[bg][sq]overlay=x={viewport_x}:y={viewport.y}:shortest=1[stage]",
    ]

    # --- hook, composited last so the zoom can never touch it -----------
    if inputs.hook_png is not None:
        parts.append("[1:v]format=rgba[hook]")
        parts.append("[stage][hook]overlay=x=0:y=0:format=auto[withhook]")
        parts.append("[withhook]format=yuv420p[vout]")
    else:
        parts.append("[stage]format=yuv420p[vout]")

    return ";".join(parts)


def compose(
    ffmpeg: FFmpeg,
    *,
    inputs: CompositionInputs,
    viewport: ViewportSettings,
    output: OutputSettings,
    timeline: list[VideoEffect],
    destination: Path,
    duration: float,
    on_progress: ProgressCallback | None = None,
) -> Path:
    args = ["-i", str(inputs.video)]
    if inputs.hook_png is not None:
        args += ["-i", str(inputs.hook_png)]

    args += [
        "-filter_complex", build_filter_graph(inputs, viewport, output, timeline),
        "-map", "[vout]",
    ]
    if inputs.has_audio:
        args += ["-map", "0:a:0", "-c:a", "aac", "-b:a", f"{output.audio_bitrate_kbps}k", "-ar", "48000"]
    else:
        args += ["-an"]

    args += [
        "-c:v", "libx264",
        "-profile:v", "high",
        "-level", "4.2",
        "-preset", output.x264_preset,
        "-crf", str(output.crf),
        "-r", str(output.fps),
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        "-shortest",
        str(destination),
    ]

    ffmpeg.run(
        args,
        stage="rendering the final video",
        expected_duration=duration,
        on_progress=on_progress,
    )
    return destination
