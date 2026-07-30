#!/usr/bin/env python3
"""Generate a small, fully synthetic test video.

No copyrighted media is committed to this repository. The fixture is built on
demand with FFmpeg: a moving test pattern plus a speech-like tone track that
contains deliberate silent gaps, so silence removal has something real to do.

Usage:
    python3 scripts/make_fixture.py [output.mp4] [seconds]
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def build_fixture(destination: Path, seconds: int = 20, fps: int = 25) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)

    # Audible bursts separated by true silence, so silencedetect has real gaps.
    bursts = []
    for index in range(seconds // 4):
        start = index * 4
        bursts.append(
            f"between(t,{start + 0.3},{start + 2.2})"
        )
    gate = "+".join(bursts) if bursts else "1"

    command = [
        "ffmpeg", "-hide_banner", "-nostdin", "-y",
        "-f", "lavfi", "-i", f"testsrc2=size=1280x720:rate={fps}:duration={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency=220:sample_rate=48000:duration={seconds}",
        "-filter_complex",
        f"[1:a]volume='if({gate},0.8,0.0)':eval=frame[a]",
        "-map", "0:v", "-map", "[a]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        "-shortest",
        str(destination),
    ]
    subprocess.run(command, check=True, capture_output=True, text=True)
    return destination


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/fixtures/sample.mp4")
    length = int(sys.argv[2]) if len(sys.argv) > 2 else 20
    path = build_fixture(target, length)
    print(f"Fixture written to {path} ({path.stat().st_size / 1024:.0f} KB)")
