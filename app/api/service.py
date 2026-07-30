"""Application service layer.

All business logic the HTTP layer needs lives here, expressed in plain Python
types. The HTTP layer only parses requests and serialises results, which is what
makes it cheap to move this app onto ASGI, or to split the worker onto another
machine, later on.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import BinaryIO

from .. import colors
from ..errors import NotFoundError, UnsupportedMediaError, ValidationError
from ..fonts import FontRegistry
from ..models import (
    EFFECT_TYPES,
    OUTPUT_PRESETS,
    TRANSCRIPTION_BACKENDS,
    Preset,
    RenderRequest,
    default_preset,
)
from ..presets import PresetStore
from ..processor.ffmpeg import FFmpeg
from ..processor.probe import probe
from ..processor.render import RenderPipeline
from ..processor.transcribe import describe_backends
from ..settings import Settings
from ..storage import LocalStorage, file_extension
from .jobs import JobManager

log = logging.getLogger(__name__)


class AppService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.storage = LocalStorage(settings)
        self.fonts = FontRegistry(settings)
        self.fonts.scan()
        self.presets = PresetStore(settings)
        self.ffmpeg = FFmpeg(settings)
        self.pipeline = RenderPipeline(settings, self.storage, self.fonts, self.ffmpeg)
        self.jobs = JobManager(settings, self.storage, self.pipeline)

    # -- health & config -------------------------------------------------

    def health(self) -> dict:
        try:
            ffmpeg_version = self.ffmpeg.version()
            ffmpeg_ok = True
        except Exception as exc:  # pragma: no cover - only without FFmpeg
            ffmpeg_version = str(exc)
            ffmpeg_ok = False
        return {
            "status": "ok" if ffmpeg_ok else "degraded",
            "ffmpeg": {"available": ffmpeg_ok, "version": ffmpeg_version},
            "transcription": describe_backends(self.settings),
            "freeDiskBytes": self.storage.free_bytes(),
        }

    def config(self) -> dict:
        """Everything the UI needs to render itself - no hardcoded values client-side."""
        return {
            "colors": colors.palette(),
            "defaults": {
                "highlightColor": colors.DEFAULT_HIGHLIGHT_COLOR,
                "textColor": colors.DEFAULT_TEXT_COLOR,
                "outlineColor": colors.DEFAULT_OUTLINE_COLOR,
                "shadowColor": colors.DEFAULT_SHADOW_COLOR,
                "backgroundColor": colors.DEFAULT_BACKGROUND_COLOR,
            },
            "fonts": self.fonts.list_fonts(),
            "presets": [p.to_dict() for p in self.presets.list()],
            "effectTypes": list(EFFECT_TYPES),
            "outputPresets": sorted(OUTPUT_PRESETS),
            "transcriptionBackends": describe_backends(self.settings),
            "limits": {
                "maxUploadBytes": self.settings.max_upload_bytes,
                "videoExtensions": sorted(self.settings.allowed_video_extensions),
                "fontExtensions": sorted(self.settings.allowed_font_extensions),
            },
            "whisper": {
                "modelSize": self.settings.whisper_model_size,
                "device": self.settings.whisper_device,
            },
        }

    # -- sources ---------------------------------------------------------

    def create_source(
        self, stream: BinaryIO, filename: str, declared_size: int | None = None
    ) -> dict:
        extension = file_extension(filename)
        if extension not in self.settings.allowed_video_extensions:
            allowed = ", ".join(sorted(self.settings.allowed_video_extensions))
            raise UnsupportedMediaError(
                f"'{extension or filename}' is not a supported video format. Use one of: {allowed}"
            )

        asset = self.storage.save_source(stream, filename, declared_size)
        try:
            info = probe(self.ffmpeg, self.storage.source_path(asset.id))
        except Exception:
            # A file we cannot decode is useless; do not keep it around.
            self.storage.delete_source(asset.id)
            raise

        asset.media = info.to_dict()
        self.storage.write_source(asset)
        return {"source": asset.to_dict(), "media": info.to_dict()}

    def get_source(self, source_id: str) -> dict:
        return self.storage.load_source(source_id).to_dict()

    def list_sources(self) -> list[dict]:
        return [a.to_dict() for a in self.storage.list_sources()]

    def delete_source(self, source_id: str) -> None:
        self.storage.delete_source(source_id)

    # -- fonts -----------------------------------------------------------

    def upload_font(self, stream: BinaryIO, filename: str) -> dict:
        extension = file_extension(filename)
        if extension not in self.settings.allowed_font_extensions:
            raise UnsupportedMediaError(
                "Only .ttf and .otf font files can be uploaded."
            )
        record = self.fonts.save_upload(stream, filename)
        return record.to_dict()

    def list_fonts(self) -> list[dict]:
        self.fonts.scan()
        return self.fonts.list_fonts()

    def font_file(self, font_id: str) -> Path:
        """Serve a font file so the browser preview can use the real typeface."""
        record = self.fonts.get(font_id)
        if record is None or not record.path.is_file():
            raise NotFoundError("That font is not installed.")
        return record.path

    def delete_font(self, font_id: str) -> None:
        if not self.fonts.delete(font_id):
            raise NotFoundError("That font is not installed.")

    # -- presets ---------------------------------------------------------

    def list_presets(self) -> list[dict]:
        return [p.to_dict() for p in self.presets.list()]

    def save_preset(self, payload: dict) -> dict:
        preset = Preset.from_dict(payload)
        return self.presets.save(preset).to_dict()

    def delete_preset(self, preset_id: str) -> None:
        self.presets.delete(preset_id)

    def default_preset(self) -> dict:
        return default_preset().to_dict()

    # -- rendering -------------------------------------------------------

    def create_job(self, payload: dict) -> dict:
        source_id = str(payload.get("sourceId") or "").strip()
        if not source_id:
            raise ValidationError("Upload a video before generating a clip.")
        asset = self.storage.load_source(source_id)

        backend = str(
            (payload.get("transcription") or {}).get("backend")
            or self.settings.transcription_backend
        )
        if backend not in TRANSCRIPTION_BACKENDS:
            raise ValidationError(f"Unknown caption source '{backend}'.")

        duration = float(asset.media.get("duration") or 0.0)
        if duration <= 0:
            raise ValidationError(
                "That video's duration is unknown, so the clip range cannot be checked. "
                "Try uploading it again."
            )
        request = RenderRequest.from_dict(payload, source_duration=duration)
        job = self.jobs.submit(request)
        return job.to_dict()

    def get_job(self, job_id: str) -> dict:
        return self.jobs.get(job_id).to_dict()

    def list_jobs(self) -> list[dict]:
        return [j.to_dict() for j in self.jobs.list()]

    def cancel_job(self, job_id: str) -> dict:
        return self.jobs.cancel(job_id).to_dict()

    def job_output(self, job_id: str) -> Path:
        job = self.jobs.get(job_id)
        if not job.output_path:
            raise NotFoundError("That render has not finished yet.")
        path = Path(job.output_path)
        if not path.is_file():
            raise NotFoundError("The rendered file has already been cleaned up.")
        return path

    # -- housekeeping ----------------------------------------------------

    def purge(self) -> dict:
        removed = self.jobs.purge_expired()
        return {"removed": removed, "count": len(removed)}

    def shutdown(self) -> None:
        self.jobs.shutdown()
