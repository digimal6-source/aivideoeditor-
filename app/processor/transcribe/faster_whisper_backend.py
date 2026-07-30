"""faster-whisper backend - local, free, CPU-capable speech-to-text.

The model is loaded lazily and cached at module level, so a long session only
pays the load cost once and the weights are never re-downloaded per job. The
download directory is controlled by WHISPER_DOWNLOAD_ROOT so a Codespace keeps
the cache on the workspace volume.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from ...captions import Word
from ...errors import DependencyError, ProcessingError
from ...models import TranscriptionSettings
from ...settings import Settings

log = logging.getLogger(__name__)

_MODEL_CACHE: dict[tuple[str, str, str], object] = {}
_LOCK = threading.Lock()


class FasterWhisperBackend:
    name = "faster_whisper"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    # -- availability ----------------------------------------------------

    def available(self) -> tuple[bool, str]:
        try:
            import faster_whisper  # noqa: F401
        except Exception as exc:  # ImportError, or a broken ctranslate2 build
            return False, (
                "faster-whisper is not installed. Run: pip install -r requirements.txt "
                f"({exc.__class__.__name__})"
            )
        return True, ""

    # -- model management -------------------------------------------------

    def _load_model(self, settings: TranscriptionSettings):
        ok, reason = self.available()
        if not ok:
            raise DependencyError(
                "Automatic captions need the faster-whisper model, which is not installed. "
                "Install it, or switch the caption source to 'Paste transcript' / 'No captions'.",
                detail=reason,
            )
        from faster_whisper import WhisperModel

        key = (
            settings.whisper_model_name(),
            self.settings.whisper_device,
            self.settings.whisper_compute_type,
        )
        with _LOCK:
            model = _MODEL_CACHE.get(key)
            if model is None:
                download_root = Path(self.settings.whisper_download_root)
                download_root.mkdir(parents=True, exist_ok=True)
                log.info("Loading faster-whisper model %s (%s)", key[0], key[1])
                model = WhisperModel(
                    key[0],
                    device=key[1],
                    compute_type=key[2],
                    download_root=str(download_root),
                )
                _MODEL_CACHE[key] = model
            return model

    # -- transcription -----------------------------------------------------

    def transcribe(
        self,
        audio_path: Path,
        *,
        duration: float,
        settings: TranscriptionSettings,
        on_progress=None,
    ) -> list[Word]:
        model = self._load_model(settings)
        language = None if settings.language in ("", "auto") else settings.language
        try:
            segments, _info = model.transcribe(
                str(audio_path),
                language=language,
                beam_size=settings.beam_size,
                word_timestamps=True,
                vad_filter=False,
                condition_on_previous_text=False,
            )
            words: list[Word] = []
            for segment in segments:
                for word in getattr(segment, "words", None) or []:
                    text = (word.word or "").strip()
                    if not text:
                        continue
                    words.append(Word(text, float(word.start), float(word.end)))
                if on_progress and duration > 0:
                    on_progress(max(0.0, min(1.0, float(segment.end) / duration)))
            if on_progress:
                on_progress(1.0)
        except DependencyError:
            raise
        except Exception as exc:  # pragma: no cover - depends on model runtime
            raise ProcessingError(
                "Speech recognition failed. Try a smaller Whisper model, or switch the "
                "caption source to 'Paste transcript'.",
                detail=f"{exc.__class__.__name__}: {exc}",
            ) from exc
        return words
