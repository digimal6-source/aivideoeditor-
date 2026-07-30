"""Time-based zoom/pan effects for the video layer.

The effect list is a plain, serialisable timeline of :class:`VideoEffect`
keyframes, so a manual keyframe editor can later write the exact same structure
that the automatic generator produces today. Nothing here is random: the same
input always yields the same expression.

The generated expressions are consumed by FFmpeg's ``zoompan`` filter, which is
the only filter that can change apparent scale per-frame while keeping the
output frame size constant. Constant output size is what allows the square
viewport to stay perfectly fixed.
"""

from __future__ import annotations

from ..models import EffectSettings, VideoEffect

#: pan_x / pan_y magnitude used by the directional pan effect types
PAN_TRAVEL = 0.6


def auto_timeline(duration: float, settings: EffectSettings) -> list[VideoEffect]:
    """A subtle, deterministic breathing zoom.

    Holds a neutral framing for ``auto_hold_seconds``, then alternates slowly
    between 1.0 and 1.0 + ``auto_zoom_amount`` on a fixed cycle. There is no
    randomness and no per-cut punch-in, so the result reads as intentional
    camera movement rather than automated jitter.
    """
    if duration <= 0 or settings.auto_zoom_amount <= 0:
        return [VideoEffect(0.0, max(duration, 0.001), "normal", settings.base_scale)]

    base = settings.base_scale
    peak = round(base + settings.auto_zoom_amount, 4)
    timeline: list[VideoEffect] = []

    cursor = 0.0
    hold = min(settings.auto_hold_seconds, duration)
    if hold > 0:
        timeline.append(VideoEffect(0.0, hold, "normal", base, scale_to=base))
        cursor = hold

    going_up = True
    while cursor < duration - 1e-6:
        end = min(cursor + settings.auto_cycle_seconds, duration)
        start_scale, end_scale = (base, peak) if going_up else (peak, base)
        timeline.append(
            VideoEffect(
                start=round(cursor, 3),
                end=round(end, 3),
                type="zoom_in" if going_up else "zoom_out",
                scale=start_scale,
                scale_to=end_scale,
            )
        )
        cursor = end
        going_up = not going_up

    return timeline


def resolve_timeline(duration: float, settings: EffectSettings) -> list[VideoEffect]:
    if settings.mode == "auto":
        return auto_timeline(duration, settings)
    if settings.mode == "manual" and settings.effects:
        return _normalise_manual(duration, settings)
    return [VideoEffect(0.0, max(duration, 0.001), "normal", settings.base_scale)]


def _normalise_manual(duration: float, settings: EffectSettings) -> list[VideoEffect]:
    """Sort user effects and fill any gaps with neutral segments."""
    base = settings.base_scale
    ordered = sorted(settings.effects, key=lambda e: e.start)
    result: list[VideoEffect] = []
    cursor = 0.0
    for effect in ordered:
        start = max(0.0, min(effect.start, duration))
        end = max(0.0, min(effect.end, duration))
        if end <= start:
            continue
        if start > cursor + 1e-6:
            result.append(VideoEffect(cursor, start, "normal", base, scale_to=base))
        result.append(effect)
        cursor = end
    if cursor < duration - 1e-6:
        result.append(VideoEffect(cursor, duration, "normal", base, scale_to=base))
    return result or [VideoEffect(0.0, max(duration, 0.001), "normal", base)]


def _segment_values(effect: VideoEffect) -> tuple[float, float, float, float, float, float]:
    """(zoom_from, zoom_to, panx_from, panx_to, pany_from, pany_to)"""
    zoom_from = effect.scale
    zoom_to = effect.scale_to if effect.scale_to is not None else effect.scale
    panx_from = panx_to = effect.pan_x
    pany_from = pany_to = effect.pan_y

    if effect.type == "zoom_in" and effect.scale_to is None:
        zoom_from, zoom_to = 1.0, effect.scale
    elif effect.type == "zoom_out" and effect.scale_to is None:
        zoom_from, zoom_to = effect.scale, 1.0
    elif effect.type == "pan_left":
        panx_from, panx_to = PAN_TRAVEL, -PAN_TRAVEL
    elif effect.type == "pan_right":
        panx_from, panx_to = -PAN_TRAVEL, PAN_TRAVEL
    elif effect.type == "pan_up":
        pany_from, pany_to = PAN_TRAVEL, -PAN_TRAVEL
    elif effect.type == "pan_down":
        pany_from, pany_to = -PAN_TRAVEL, PAN_TRAVEL

    # A pan is only visible when there is something to pan across.
    if effect.type.startswith("pan") and max(zoom_from, zoom_to) <= 1.0001:
        zoom_from = zoom_to = max(1.08, effect.scale)
    return zoom_from, zoom_to, panx_from, panx_to, pany_from, pany_to


def _lerp_expr(time_var: str, start: float, end: float, a: float, b: float) -> str:
    if abs(b - a) < 1e-6 or end - start < 1e-6:
        return f"{a:.6f}"
    span = end - start
    return f"({a:.6f}+({b - a:.6f})*(({time_var})-{start:.6f})/{span:.6f})"


def _piecewise(time_var: str, segments: list[tuple[float, float, str]], fallback: str) -> str:
    """Nested if() chain: if(lt(t,e0),expr0, if(lt(t,e1),expr1, ... fallback))"""
    expression = fallback
    for _, end, expr in reversed(segments):
        expression = f"if(lt({time_var},{end:.6f}),{expr},{expression})"
    return expression


def build_expressions(timeline: list[VideoEffect], fps: int) -> dict[str, str]:
    """Return zoompan-compatible ``z``, ``x`` and ``y`` expressions.

    ``zoompan`` does not expose ``t``; the current output frame index ``on`` is
    used instead, converted to seconds with the known output frame rate.
    """
    time_var = f"(on/{fps})"

    zoom_segments: list[tuple[float, float, str]] = []
    panx_segments: list[tuple[float, float, str]] = []
    pany_segments: list[tuple[float, float, str]] = []

    last_zoom = last_px = last_py = None
    for effect in timeline:
        z0, z1, x0, x1, y0, y1 = _segment_values(effect)
        zoom_segments.append(
            (effect.start, effect.end, _lerp_expr(time_var, effect.start, effect.end, z0, z1))
        )
        panx_segments.append(
            (effect.start, effect.end, _lerp_expr(time_var, effect.start, effect.end, x0, x1))
        )
        pany_segments.append(
            (effect.start, effect.end, _lerp_expr(time_var, effect.start, effect.end, y0, y1))
        )
        last_zoom, last_px, last_py = z1, x1, y1

    zoom_expr = _piecewise(time_var, zoom_segments, f"{last_zoom if last_zoom else 1.0:.6f}")
    panx_expr = _piecewise(time_var, panx_segments, f"{last_px if last_px else 0.0:.6f}")
    pany_expr = _piecewise(time_var, pany_segments, f"{last_py if last_py else 0.0:.6f}")

    # zoompan's x/y are the top-left of the cropped window in input pixels.
    # Centre it, then offset by the pan factor across the available travel.
    x_expr = f"(iw-iw/zoom)/2*(1+({panx_expr}))"
    y_expr = f"(ih-ih/zoom)/2*(1+({pany_expr}))"
    return {"z": zoom_expr, "x": x_expr, "y": y_expr}


def is_static(timeline: list[VideoEffect]) -> bool:
    """True when the timeline never moves, letting us skip zoompan entirely."""
    for effect in timeline:
        z0, z1, x0, x1, y0, y1 = _segment_values(effect)
        if abs(z0 - 1.0) > 1e-6 or abs(z1 - 1.0) > 1e-6:
            return False
        if any(abs(v) > 1e-6 for v in (x0, x1, y0, y1)):
            return False
    return True
