"""Environment-driven application settings.

Nothing in the codebase reads ``os.environ`` directly except this module, and
nothing hardcodes a filesystem path. That is what makes it possible to later
move uploads/outputs to object storage or split the worker onto its own host.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path


def _env_str(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return Path(raw.strip()).expanduser()


REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    # --- HTTP -------------------------------------------------------------
    host: str
    port: int
    cors_allow_origin: str

    # --- Storage ----------------------------------------------------------
    data_dir: Path
    upload_dir: Path
    output_dir: Path
    jobs_dir: Path
    fonts_dir: Path
    presets_file: Path

    # --- Limits -----------------------------------------------------------
    max_upload_bytes: int
    allowed_video_extensions: frozenset[str]
    allowed_font_extensions: frozenset[str]
    job_retention_hours: float
    max_concurrent_jobs: int

    # --- External tools ---------------------------------------------------
    ffmpeg_bin: str
    ffprobe_bin: str
    ffmpeg_timeout_seconds: int

    # --- Transcription ----------------------------------------------------
    transcription_backend: str
    whisper_model_size: str
    whisper_device: str
    whisper_compute_type: str
    whisper_language: str
    whisper_beam_size: int
    whisper_download_root: Path

    # --- Misc -------------------------------------------------------------
    log_level: str
    web_dir: Path = field(default=REPO_ROOT / "app" / "web")

    def ensure_dirs(self) -> None:
        for directory in (
            self.data_dir,
            self.upload_dir,
            self.output_dir,
            self.jobs_dir,
            self.fonts_dir,
            self.presets_file.parent,
            self.whisper_download_root,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    def public_dict(self) -> dict:
        """Settings that are safe to expose to the browser."""
        return {
            "maxUploadBytes": self.max_upload_bytes,
            "allowedVideoExtensions": sorted(self.allowed_video_extensions),
            "allowedFontExtensions": sorted(self.allowed_font_extensions),
            "transcriptionBackend": self.transcription_backend,
            "whisperModelSize": self.whisper_model_size,
            "jobRetentionHours": self.job_retention_hours,
        }


def build_settings() -> Settings:
    data_dir = _env_path("DATA_DIR", REPO_ROOT / "data").resolve()
    return Settings(
        host=_env_str("API_HOST", "0.0.0.0"),
        port=_env_int("API_PORT", 8000),
        cors_allow_origin=_env_str("CORS_ALLOW_ORIGIN", "*"),
        data_dir=data_dir,
        upload_dir=_env_path("UPLOAD_DIR", data_dir / "uploads").resolve(),
        output_dir=_env_path("OUTPUT_DIR", data_dir / "outputs").resolve(),
        jobs_dir=_env_path("JOBS_DIR", data_dir / "jobs").resolve(),
        fonts_dir=_env_path("FONTS_DIR", REPO_ROOT / "fonts").resolve(),
        presets_file=_env_path("PRESETS_FILE", data_dir / "presets.json").resolve(),
        max_upload_bytes=_env_int("MAX_UPLOAD_BYTES", 4 * 1024 * 1024 * 1024),
        allowed_video_extensions=frozenset(
            ext.strip().lower()
            for ext in _env_str("ALLOWED_VIDEO_EXTENSIONS", ".mp4,.mov,.mkv,.webm,.m4v,.avi").split(",")
            if ext.strip()
        ),
        allowed_font_extensions=frozenset({".ttf", ".otf"}),
        job_retention_hours=_env_float("JOB_RETENTION_HOURS", 12.0),
        max_concurrent_jobs=_env_int("MAX_CONCURRENT_JOBS", 1),
        ffmpeg_bin=_env_str("FFMPEG_BIN", "ffmpeg"),
        ffprobe_bin=_env_str("FFPROBE_BIN", "ffprobe"),
        ffmpeg_timeout_seconds=_env_int("FFMPEG_TIMEOUT_SECONDS", 3600),
        transcription_backend=_env_str("TRANSCRIPTION_BACKEND", "faster_whisper"),
        whisper_model_size=_env_str("WHISPER_MODEL_SIZE", "base"),
        whisper_device=_env_str("WHISPER_DEVICE", "cpu"),
        whisper_compute_type=_env_str("WHISPER_COMPUTE_TYPE", "int8"),
        whisper_language=_env_str("WHISPER_LANGUAGE", "auto"),
        whisper_beam_size=_env_int("WHISPER_BEAM_SIZE", 1),
        whisper_download_root=_env_path("WHISPER_DOWNLOAD_ROOT", data_dir / "models").resolve(),
        log_level=_env_str("LOG_LEVEL", "INFO").upper(),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = build_settings()
    settings.ensure_dirs()
    return settings


def reset_settings_cache() -> None:
    """Used by tests that patch the environment."""
    get_settings.cache_clear()
