"""Safe FFmpeg/FFprobe invocation.

Every external call is made with an argument **array** - never a shell string -
so no user-supplied text can ever be interpreted as a command. Filter graphs are
built from validated numbers and from paths we generated ourselves.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Sequence

from ..errors import DependencyError, ProcessingError
from ..settings import Settings

log = logging.getLogger(__name__)

ProgressCallback = Callable[[float], None]

_OUT_TIME_US = re.compile(r"^out_time_(?:us|ms)=(-?\d+)$", re.MULTILINE)


def _resolve(binary: str, label: str) -> str:
    found = shutil.which(binary) or (binary if Path(binary).is_file() else None)
    if not found:
        raise DependencyError(
            f"{label} was not found. Install it with: sudo apt-get install -y ffmpeg"
        )
    return found


class FFmpeg:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    # -- availability ----------------------------------------------------

    def ffmpeg_bin(self) -> str:
        return _resolve(self.settings.ffmpeg_bin, "FFmpeg")

    def ffprobe_bin(self) -> str:
        return _resolve(self.settings.ffprobe_bin, "FFprobe")

    def version(self) -> str:
        result = subprocess.run(
            [self.ffmpeg_bin(), "-version"], capture_output=True, text=True, check=False
        )
        return (result.stdout or "").splitlines()[0] if result.stdout else "unknown"

    # -- execution -------------------------------------------------------

    def run(
        self,
        args: Sequence[str],
        *,
        stage: str,
        expected_duration: float | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> str:
        """Run FFmpeg and return its stderr log.

        When ``expected_duration`` and ``on_progress`` are supplied, real encode
        progress is streamed from FFmpeg's ``-progress`` output. Progress is
        never simulated: if FFmpeg reports nothing, the caller shows an
        indeterminate indicator instead.
        """
        command = [self.ffmpeg_bin(), "-hide_banner", "-nostdin", "-y"]
        if on_progress and expected_duration:
            command += ["-progress", "pipe:1", "-nostats"]
        command += list(args)

        log.info("[%s] %s", stage, " ".join(command))
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except OSError as exc:  # pragma: no cover - only on a broken install
            raise DependencyError("FFmpeg could not be started.", detail=str(exc)) from exc

        stderr_tail: list[str] = []
        try:
            if on_progress and expected_duration and process.stdout is not None:
                for line in process.stdout:
                    match = _OUT_TIME_US.match(line.strip())
                    if not match:
                        continue
                    raw = int(match.group(1))
                    seconds = raw / 1_000_000 if "out_time_us" in line else raw / 1000
                    if seconds >= 0:
                        on_progress(max(0.0, min(1.0, seconds / expected_duration)))
            process.wait(timeout=self.settings.ffmpeg_timeout_seconds)
        except subprocess.TimeoutExpired:
            process.kill()
            raise ProcessingError(
                f"Rendering timed out during '{stage}'. Try a shorter clip or a lower FPS."
            ) from None
        finally:
            if process.stderr is not None:
                stderr_tail = process.stderr.read().splitlines()[-60:]
            for pipe in (process.stdout, process.stderr):
                if pipe is not None:
                    pipe.close()

        stderr_text = "\n".join(stderr_tail)
        if process.returncode != 0:
            log.error("[%s] FFmpeg failed (%s):\n%s", stage, process.returncode, stderr_text)
            raise ProcessingError(
                f"Video processing failed while {stage}. See the server log for details.",
                detail=stderr_text,
            )
        return stderr_text

    def run_probe(self, args: Sequence[str]) -> str:
        command = [self.ffprobe_bin(), "-hide_banner", *args]
        log.info("[probe] %s", " ".join(command))
        result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=120)
        if result.returncode != 0:
            raise ProcessingError(
                "That video could not be read. It may be corrupt or in an unsupported format.",
                detail=result.stderr,
            )
        return result.stdout
