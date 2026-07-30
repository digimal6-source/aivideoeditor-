"""Pipeline orchestration.

Implements the exact rendering order from the specification:

    extract range -> optional silence removal -> transcription ->
    caption segmentation -> video transform -> black 9:16 canvas ->
    fixed 1:1 viewport clip -> captions inside the viewport ->
    hook above the viewport -> H.264 + AAC MP4

Every stage reports real progress. Nothing is simulated: where FFmpeg cannot
report a percentage, the stage is marked indeterminate instead of inventing one.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..captions import Phrase, Word, enforce_minimum_display, group_words
from ..errors import AppError, ProcessingError
from ..fonts import FontRegistry
from ..models import JobStage, RenderRequest
from ..settings import Settings
from ..storage import LocalStorage
from . import ass_render, clip as clip_stage, hook_render, silence as silence_stage
from .compositor import CompositionInputs, compose
from .effects import resolve_timeline
from .ffmpeg import FFmpeg
from .probe import MediaInfo, probe
from .transcribe import get_backend

log = logging.getLogger(__name__)

#: stage, fraction-within-stage (or None for indeterminate), message
StageReporter = Callable[[JobStage, float | None, str], None]


def _noop(stage: JobStage, progress: float | None, message: str) -> None:
    del stage, progress, message


@dataclass
class RenderOutcome:
    output_path: Path
    media: MediaInfo
    caption_count: int
    removed_silence_seconds: float
    phrases: list[Phrase] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class RenderPipeline:
    def __init__(
        self,
        settings: Settings,
        storage: LocalStorage,
        fonts: FontRegistry,
        ffmpeg: FFmpeg | None = None,
    ) -> None:
        self.settings = settings
        self.storage = storage
        self.fonts = fonts
        self.ffmpeg = ffmpeg or FFmpeg(settings)

    # ------------------------------------------------------------------

    def run(
        self,
        job_id: str,
        request: RenderRequest,
        *,
        report: StageReporter = _noop,
    ) -> RenderOutcome:
        workspace = self.storage.job_workspace(job_id)
        warnings: list[str] = []

        # --- 1. analyse the source -----------------------------------
        report(JobStage.ANALYZING, None, "Analyzing video")
        source_path = self.storage.source_path(request.source_id)
        source_info = probe(self.ffmpeg, source_path)
        if request.clip.end > source_info.duration + 0.25:
            raise ProcessingError(
                f"The clip ends at {request.clip.end:.1f}s but the video is only "
                f"{source_info.duration:.1f}s long."
            )
        report(JobStage.ANALYZING, 1.0, "Analyzing video")

        # --- 2. extract the selected range ---------------------------
        clip_path = workspace / "clip.mp4"
        report(JobStage.EXTRACTING, 0.0, "Extracting clip")
        clip_stage.extract_clip(
            self.ffmpeg,
            source=source_path,
            destination=clip_path,
            clip=request.clip,
            has_audio=source_info.has_audio,
            on_progress=lambda f: report(JobStage.EXTRACTING, f, "Extracting clip"),
        )
        working = clip_path
        working_duration = request.clip.duration
        has_audio = source_info.has_audio

        # --- 3. optional silence removal -----------------------------
        removed_seconds = 0.0
        if request.silence.enabled and has_audio:
            report(JobStage.DETECTING_SILENCE, None, "Detecting speech")
            cut_path = workspace / "cut.mp4"
            result = silence_stage.remove_silence(
                self.ffmpeg,
                source=working,
                destination=cut_path,
                duration=working_duration,
                has_audio=has_audio,
                settings=request.silence,
                on_progress=lambda f: report(JobStage.REMOVING_SILENCE, f, "Removing silence"),
            )
            working = result.path
            removed_seconds = result.removed_seconds
            if result.changed:
                working_duration = max(0.2, result.new_duration)
                log.info("Silence removal cut %.2fs", removed_seconds)
            else:
                warnings.append(
                    "No obvious dead air was found, so the clip was left at full length."
                )
            report(JobStage.REMOVING_SILENCE, 1.0, "Removing silence")
        elif request.silence.enabled and not has_audio:
            warnings.append("The video has no audio track, so silence removal was skipped.")

        # Re-probe: the cut duration is authoritative for captions and progress.
        working_info = probe(self.ffmpeg, working)
        working_duration = working_info.duration or working_duration
        has_audio = working_info.has_audio

        # --- 4. transcription ----------------------------------------
        phrases: list[Phrase] = []
        if request.captions.enabled and request.transcription.backend != "none":
            report(JobStage.TRANSCRIBING, None, "Transcribing audio")
            words = self._transcribe(
                workspace, working, working_duration, request, has_audio, report, warnings
            )
            report(JobStage.BUILDING_CAPTIONS, 0.0, "Generating captions")
            phrases = enforce_minimum_display(group_words(words, request.captions))
            (workspace / "transcript.json").write_text(
                json.dumps([w.to_dict() for w in words], indent=2), encoding="utf-8"
            )
            report(JobStage.BUILDING_CAPTIONS, 1.0, "Generating captions")

        # --- 5. caption subtitle file --------------------------------
        ass_path: Path | None = None
        if phrases:
            caption_font, warning = self.fonts.resolve(request.captions.font_id, purpose="captions")
            if warning:
                warnings.append(warning)
            ass_path = ass_render.write_ass(
                phrases,
                request.captions,
                destination=workspace / "captions.ass",
                viewport_size=request.viewport.size,
                font_family=caption_font.family,
            )
        elif request.captions.enabled:
            warnings.append("No speech was detected, so no captions were burned in.")

        # --- 6. hook layer -------------------------------------------
        hook_png: Path | None = None
        if request.hook.is_renderable:
            report(JobStage.RENDERING_HOOK, 0.0, "Rendering hook")
            hook_font, warning = self.fonts.resolve(request.hook.font_id, purpose="hook")
            if warning:
                warnings.append(warning)
            hook_png, hook_warnings = hook_render.render_hook_png(
                request.hook,
                request.viewport,
                font_path=Path(hook_font.path),
                destination=workspace / "hook.png",
                canvas_width=request.output.width,
                canvas_height=request.output.height,
            )
            warnings.extend(hook_warnings)
            report(JobStage.RENDERING_HOOK, 1.0, "Rendering hook")

        # --- 7. composition + encode ---------------------------------
        report(JobStage.COMPOSITING, 1.0, "Compositing square viewport")
        timeline = resolve_timeline(working_duration, request.effects)
        output_path = self.storage.output_path(job_id)
        report(JobStage.ENCODING, 0.0, "Rendering final video")
        compose(
            self.ffmpeg,
            inputs=CompositionInputs(
                video=working,
                hook_png=hook_png,
                ass_file=ass_path,
                fonts_dir=self.fonts.fonts_dir_for_libass(),
                has_audio=has_audio,
            ),
            viewport=request.viewport,
            output=request.output,
            timeline=timeline,
            destination=output_path,
            duration=working_duration,
            on_progress=lambda f: report(JobStage.ENCODING, f, "Rendering final video"),
        )
        report(JobStage.ENCODING, 1.0, "Rendering final video")

        # --- 8. verify + clean up ------------------------------------
        report(JobStage.FINALIZING, 0.0, "Finalizing")
        if not output_path.is_file() or output_path.stat().st_size == 0:
            raise ProcessingError("Rendering finished but produced no output file.")
        final_info = probe(self.ffmpeg, output_path)
        if (final_info.width, final_info.height) != (request.output.width, request.output.height):
            raise ProcessingError(
                f"The rendered video is {final_info.width}x{final_info.height} but "
                f"{request.output.width}x{request.output.height} was expected."
            )
        self.storage.cleanup_job(job_id, keep_output=True)
        report(JobStage.FINALIZING, 1.0, "Finalizing")

        return RenderOutcome(
            output_path=output_path,
            media=final_info,
            caption_count=len(phrases),
            removed_silence_seconds=removed_seconds,
            phrases=phrases,
            warnings=warnings,
        )

    # ------------------------------------------------------------------

    def _transcribe(
        self,
        workspace: Path,
        working: Path,
        duration: float,
        request: RenderRequest,
        has_audio: bool,
        report: StageReporter,
        warnings: list[str],
    ) -> list[Word]:
        backend_name = request.transcription.backend
        if backend_name == "faster_whisper" and not has_audio:
            warnings.append("The clip has no audio, so captions could not be generated.")
            return []

        backend = get_backend(backend_name, self.settings)
        available, reason = backend.available()
        if not available:
            if request.transcription.manual_text.strip():
                warnings.append(
                    "Automatic transcription is unavailable, so the transcript you pasted "
                    "was used instead."
                )
                backend = get_backend("manual", self.settings)
            else:
                raise AppError(
                    "Automatic captions are unavailable on this machine. Install faster-whisper, "
                    "paste a transcript, or turn captions off.",
                    code="transcription_unavailable",
                    http_status=503,
                    detail=reason,
                )

        audio_path = workspace / "audio.wav"
        if backend.name == "faster_whisper":
            clip_stage.extract_audio(self.ffmpeg, source=working, destination=audio_path)

        return backend.transcribe(
            audio_path,
            duration=duration,
            settings=request.transcription,
            on_progress=lambda f: report(JobStage.TRANSCRIBING, f, "Transcribing audio"),
        )
