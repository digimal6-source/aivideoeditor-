"""Pluggable local speech-to-text backends.

Backends are resolved by name so the render pipeline never imports a speech
library directly. That keeps the application runnable (and testable) on machines
where no model is installed, and makes swapping in another local engine later a
one-file change.

No paid or network transcription service is used anywhere.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Protocol

from ...captions import Word, words_from_text
from ...errors import DependencyError
from ...models import TranscriptionSettings
from ...settings import Settings

ProgressCallback = Callable[[float], None]


class TranscriptionBackend(Protocol):
    name: str

    def available(self) -> tuple[bool, str]: ...

    def transcribe(
        self,
        audio_path: Path,
        *,
        duration: float,
        settings: TranscriptionSettings,
        on_progress: ProgressCallback | None = None,
    ) -> list[Word]: ...


class ManualBackend:
    """Times a transcript the user pasted in, evenly across the clip.

    This is a genuine, fully-working captioning path - not a stub. It exists so
    the app still produces real burned-in captions when no speech model is
    installed, and so the render pipeline is testable without one.
    """

    name = "manual"

    def available(self) -> tuple[bool, str]:
        return True, ""

    def transcribe(
        self,
        audio_path: Path,
        *,
        duration: float,
        settings: TranscriptionSettings,
        on_progress: ProgressCallback | None = None,
    ) -> list[Word]:
        del audio_path
        if on_progress:
            on_progress(1.0)
        return words_from_text(settings.manual_text, duration)


class NullBackend:
    """Captions disabled."""

    name = "none"

    def available(self) -> tuple[bool, str]:
        return True, ""

    def transcribe(self, audio_path: Path, **_: object) -> list[Word]:
        del audio_path
        return []


def get_backend(name: str, settings: Settings) -> TranscriptionBackend:
    if name == "manual":
        return ManualBackend()
    if name == "none":
        return NullBackend()
    if name == "faster_whisper":
        from .faster_whisper_backend import FasterWhisperBackend

        return FasterWhisperBackend(settings)
    raise DependencyError(f"Unknown transcription backend '{name}'.")


def describe_backends(settings: Settings) -> list[dict]:
    out = []
    for name in ("faster_whisper", "manual", "none"):
        try:
            backend = get_backend(name, settings)
            ok, reason = backend.available()
        except Exception as exc:  # pragma: no cover - defensive
            ok, reason = False, str(exc)
        out.append({"id": name, "available": ok, "reason": reason})
    return out
