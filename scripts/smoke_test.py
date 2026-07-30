#!/usr/bin/env python3
"""End-to-end smoke test for Clipforge.

This is the script to run when you want proof that the machine can actually
render a video, not just that the unit tests pass. It exercises the real
pipeline: FFmpeg clip extraction, silence removal, caption grouping, ASS
burn-in inside the square viewport, hook overlay, and H.264/AAC encoding.

It uses a generated test fixture (no copyrighted media is committed to the
repository) and the 'manual' transcription backend, so it runs offline and
never downloads a speech model. Pass --whisper to exercise faster-whisper
instead, once you have installed it.

Usage:
    python scripts/smoke_test.py
    python scripts/smoke_test.py --keep       # leave the rendered MP4 behind
    python scripts/smoke_test.py --fps 60
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

OK = "  ok  "
BAD = " FAIL "


class SmokeFailure(Exception):
    pass


def step(title: str) -> None:
    print(f"\n\u2022 {title}")


def check(label: str, passed: bool, detail: str = "") -> None:
    marker = OK if passed else BAD
    print(f"  [{marker}] {label}{(' \u2014 ' + detail) if detail else ''}")
    if not passed:
        raise SmokeFailure(label)


# ---------------------------------------------------------------- stage 1


def check_ffmpeg() -> None:
    step("Checking FFmpeg")
    for binary in ("ffmpeg", "ffprobe"):
        path = shutil.which(binary)
        check(f"{binary} on PATH", path is not None, path or "not found")

    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-version"], capture_output=True, text=True
    )
    check("ffmpeg runs", result.returncode == 0)
    first_line = result.stdout.splitlines()[0] if result.stdout else ""
    print(f"        {first_line}")

    filters = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True)
    for required in ("silencedetect", "zoompan", "overlay", "ass", "scale", "crop"):
        check(f"filter '{required}' available", f" {required} " in filters.stdout)


# ---------------------------------------------------------------- stage 2


def check_imports() -> None:
    step("Checking Python dependencies")
    check(
        "Python >= 3.10",
        sys.version_info >= (3, 10),
        f"running {sys.version.split()[0]}",
    )
    modules = [
        "app.settings",
        "app.models",
        "app.captions",
        "app.colors",
        "app.fonts",
        "app.storage",
        "app.processor.ffmpeg",
        "app.processor.silence",
        "app.processor.effects",
        "app.processor.ass_render",
        "app.processor.hook_render",
        "app.processor.compositor",
        "app.processor.render",
        "app.api.service",
        "app.api.http_server",
    ]
    import importlib

    for name in modules:
        try:
            importlib.import_module(name)
            ok, detail = True, ""
        except Exception as exc:  # pragma: no cover - reported to the operator
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        check(f"import {name}", ok, detail)

    try:
        from PIL import Image  # noqa: F401

        check("Pillow (hook text rendering)", True)
    except Exception as exc:
        check("Pillow (hook text rendering)", False, str(exc))


# ---------------------------------------------------------------- stage 3


def ensure_fixture(path: Path, seconds: int) -> Path:
    step("Preparing test fixture")
    if path.is_file() and path.stat().st_size > 0:
        check("fixture already present", True, str(path))
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "make_fixture.py"), str(path), str(seconds)],
        capture_output=True,
        text=True,
    )
    check("fixture generated", result.returncode == 0, (result.stderr or "").strip()[-300:])
    check("fixture is non-empty", path.is_file() and path.stat().st_size > 0)
    print(f"        {path} ({path.stat().st_size / 1024:.0f} KB)")
    return path


# ---------------------------------------------------------------- stage 4


def run_pipeline(fixture: Path, data_dir: Path, fps: int, use_whisper: bool) -> dict:
    step("Running the render pipeline")

    os.environ["DATA_DIR"] = str(data_dir)
    from app.settings import get_settings, reset_settings_cache

    reset_settings_cache()
    settings = get_settings()

    from app.api.service import AppService

    service = AppService(settings)

    health = service.health()
    check("service reports FFmpeg available", bool(health["ffmpeg"]["available"]))

    with fixture.open("rb") as handle:
        created = service.create_source(handle, fixture.name)
    source = created["source"]
    media = created["media"]
    check("upload accepted", bool(source["id"]), source["id"])
    check("source probed", (media.get("duration") or 0) > 0, f"{media['duration']:.2f}s source")
    print(f"        source: {media['width']}x{media['height']} @ {media['fps']} fps")

    backend = "faster_whisper" if use_whisper else "manual"
    payload = {
        "sourceId": source["id"],
        "clip": {"start": "0:02", "end": "0:12"},
        "viewport": {"size": 1000, "y": 620},
        "hook": {
            "enabled": True,
            "text": "Hiring Is Now The SLOWEST Way To Ship",
            "fontSize": 74,
            "highlightColor": "#8B5CF6",
            "highlightIndices": [4],
            "align": "center",
            "uppercase": False,
        },
        "captions": {
            "enabled": True,
            "maxWordsPerPhrase": 3,
            "fontSize": 62,
            "outlineWidth": 0.5,
            "shadowOffset": 3.0,
            "uppercase": True,
        },
        "silence": {"enabled": True},
        "effects": {"mode": "auto"},
        "output": {"presetId": "1080p60" if fps == 60 else "1080p30"},
        "transcription": {
            "backend": backend,
            "manualText": (
                "This is the most important thing you need to understand right now. "
                "Hiring is now the slowest way to ship anything at all."
            ),
        },
    }

    job = service.create_job(payload)
    check("job accepted", bool(job["id"]), job["id"])

    seen: list[str] = []
    deadline = time.time() + 900
    while time.time() < deadline:
        job = service.get_job(job["id"])
        stage_name = job["status"]["stage"]
        if stage_name not in seen:
            seen.append(stage_name)
            percent = int((job["status"]["overallProgress"] or 0) * 100)
            print(f"        {percent:3d}%  {job['status']['message']}")
        if stage_name in ("done", "failed", "cancelled"):
            break
        time.sleep(0.05)

    service.shutdown()

    check(
        "pipeline finished",
        job["status"]["stage"] == "done",
        job["status"].get("error") or job["status"]["stage"],
    )
    check("progress reached 100%", abs((job["status"]["overallProgress"] or 0) - 1.0) < 1e-6)
    real_stages = [s for s in seen if s not in ("queued", "done")]
    print("        stages seen: " + " \u2192 ".join(real_stages))
    # Sampling a status endpoint always misses sub-poll stages, so only the
    # long-running ones are asserted on.
    for required in ("extracting", "encoding"):
        check(f"stage '{required}' was reported", required in real_stages)
    check("several real stages reported", len(real_stages) >= 3)
    return job


# ---------------------------------------------------------------- stage 5


def verify_output(job: dict, fps: int) -> None:
    step("Verifying the rendered file")
    meta = job["outputMeta"] or {}

    check("output file exists", bool(job["hasOutput"]))
    check("output is non-trivial in size", (job["outputBytes"] or 0) > 50_000,
          f"{(job['outputBytes'] or 0) / 1024:.0f} KB")
    check("width is 1080", meta.get("width") == 1080, str(meta.get("width")))
    check("height is 1920", meta.get("height") == 1920, str(meta.get("height")))
    check(
        "aspect ratio is 9:16",
        abs((meta.get("width", 0) / max(meta.get("height", 1), 1)) - 9 / 16) < 1e-6,
    )
    check(f"frame rate is {fps}", abs((meta.get("fps") or 0) - fps) < 0.01, str(meta.get("fps")))
    check("video codec is H.264", meta.get("videoCodec") == "h264", str(meta.get("videoCodec")))
    check("audio codec is AAC", meta.get("audioCodec") == "aac", str(meta.get("audioCodec")))
    check("audio track present", bool(meta.get("hasAudio")))
    check("duration is plausible", 0.5 < (meta.get("duration") or 0) <= 11.0,
          f"{meta.get('duration')}s")
    check("captions were produced", (job["captionCount"] or 0) > 0,
          f"{job['captionCount']} phrases")
    check("silence removal ran", job["removedSilenceSeconds"] is not None,
          f"{job['removedSilenceSeconds']:.2f}s removed")

    for warning in job.get("warnings") or []:
        print(f"        note: {warning}")


# ---------------------------------------------------------------- stage 6


def verify_fixed_viewport(output: Path, viewport_size: int, viewport_y: int) -> None:
    """Prove the square never moves by comparing frames from different times.

    The auto zoom is running throughout, so if the viewport were being scaled
    along with the video, the bounding box of the non-black region would change
    between these two frames.
    """
    step("Verifying the square viewport is fixed while the video zooms")
    try:
        from PIL import Image
    except Exception as exc:  # pragma: no cover
        check("Pillow available for frame inspection", False, str(exc))
        return

    boxes = []
    with tempfile.TemporaryDirectory() as tmp:
        for index, timestamp in enumerate(("0.5", "4.0")):
            frame = Path(tmp) / f"frame{index}.png"
            subprocess.run(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                    "-ss", timestamp, "-i", str(output), "-frames:v", "1", str(frame),
                ],
                capture_output=True,
                check=False,
            )
            check(f"frame extracted at t={timestamp}s", frame.is_file())
            image = Image.open(frame).convert("L")
            check("frame is 1080x1920", image.size == (1080, 1920), str(image.size))
            # Crop away the hook band so only the video square is measured.
            band = image.crop((0, viewport_y, 1080, viewport_y + viewport_size))
            boxes.append(band.point(lambda v: 255 if v > 16 else 0).getbbox())

    check("video content found inside the viewport band", all(b is not None for b in boxes))
    check(
        "viewport bounding box is identical across frames",
        boxes[0] == boxes[1],
        f"{boxes[0]} vs {boxes[1]}",
    )
    left, top, right, bottom = boxes[0]
    check(
        "viewport fills the configured square",
        (right - left) >= viewport_size - 2 and (bottom - top) >= viewport_size - 2,
        f"{right - left}x{bottom - top} (expected {viewport_size})",
    )
    check("black bars on both sides of the square", left >= 39 and right <= 1041,
          f"left={left} right={right}")


# ---------------------------------------------------------------- main


def main() -> int:
    parser = argparse.ArgumentParser(description="Clipforge smoke test")
    parser.add_argument("--fps", type=int, default=30, choices=[30, 60])
    parser.add_argument("--keep", action="store_true", help="keep the rendered output")
    parser.add_argument("--whisper", action="store_true", help="use faster-whisper instead of a manual transcript")
    parser.add_argument("--fixture", default=str(REPO_ROOT / "data" / "fixtures" / "sample.mp4"))
    args = parser.parse_args()

    print("=" * 66)
    print(" Clipforge smoke test")
    print("=" * 66)

    started = time.time()
    workdir = Path(tempfile.mkdtemp(prefix="clipforge-smoke-"))

    try:
        check_ffmpeg()
        check_imports()
        fixture = ensure_fixture(Path(args.fixture), 20)
        job = run_pipeline(fixture, workdir, args.fps, args.whisper)
        verify_output(job, args.fps)

        output = workdir / "outputs" / f"{job['id']}.mp4"
        if not output.is_file():
            candidates = list((workdir / "outputs").glob("*.mp4"))
            output = candidates[0] if candidates else output
        check("rendered MP4 located on disk", output.is_file(), str(output))

        verify_fixed_viewport(output, viewport_size=1000, viewport_y=620)

        if args.keep:
            destination = REPO_ROOT / "data" / "outputs" / f"smoke-{int(started)}.mp4"
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(output, destination)
            print(f"\n  Rendered file kept at: {destination}")

    except SmokeFailure as failure:
        print(f"\nSMOKE TEST FAILED: {failure}")
        print("See TROUBLESHOOTING.md for the most common causes.")
        return 1
    except Exception as exc:  # pragma: no cover
        print(f"\nSMOKE TEST ERRORED: {type(exc).__name__}: {exc}")
        return 2
    finally:
        if not args.keep:
            shutil.rmtree(workdir, ignore_errors=True)

    print("\n" + "=" * 66)
    print(f" ALL CHECKS PASSED in {time.time() - started:.1f}s")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
