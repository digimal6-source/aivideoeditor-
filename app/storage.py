"""Storage abstraction.

The rest of the application never joins paths by hand and never trusts a
client-supplied filename. Everything goes through :class:`LocalStorage`, which
implements the small :class:`StorageBackend` protocol. Swapping in an S3/R2
backed implementation later means implementing that protocol - no pipeline or
API code has to change.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Iterator, Protocol

from .errors import NotFoundError, PayloadTooLargeError, UnsupportedMediaError, ValidationError
from .settings import Settings

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{5,63}$")
_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._ -]")
_CHUNK = 1024 * 1024


def new_id(prefix: str = "") -> str:
    token = secrets.token_hex(8)
    return f"{prefix}{token}" if prefix else token


def validate_id(value: str, *, kind: str = "id") -> str:
    """Reject anything that could escape a directory (``..``, ``/``, NUL, ...)."""
    if not isinstance(value, str) or not _ID_RE.match(value):
        raise ValidationError(f"Invalid {kind}.")
    return value


def sanitize_filename(name: str, *, default: str = "upload") -> str:
    """Make a client-supplied filename safe to store and echo back."""
    if not isinstance(name, str):
        return default
    # Strip any directory component from Windows and POSIX style paths.
    name = name.replace("\\", "/").split("/")[-1]
    name = unicodedata.normalize("NFKD", name)
    name = name.replace("\x00", "")
    name = _UNSAFE_CHARS.sub("_", name).strip(". ")
    name = re.sub(r"_{2,}", "_", name)
    if not name:
        return default
    return name[:120]


def file_extension(name: str) -> str:
    return Path(sanitize_filename(name)).suffix.lower()


@dataclass
class SourceAsset:
    """An uploaded long-form video plus its probed metadata."""

    id: str
    original_name: str
    extension: str
    size_bytes: int
    created_at: float = field(default_factory=time.time)
    media: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "originalName": self.original_name,
            "extension": self.extension,
            "sizeBytes": self.size_bytes,
            "createdAt": self.created_at,
            "media": self.media,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SourceAsset":
        return cls(
            id=data["id"],
            original_name=data.get("originalName", "video"),
            extension=data.get("extension", ".mp4"),
            size_bytes=int(data.get("sizeBytes", 0)),
            created_at=float(data.get("createdAt", time.time())),
            media=data.get("media") or {},
        )


class StorageBackend(Protocol):
    def save_source(self, stream: BinaryIO, filename: str, declared_size: int | None) -> SourceAsset: ...
    def adopt_source(self, path: Path, filename: str) -> SourceAsset: ...
    def source_path(self, source_id: str) -> Path: ...
    def load_source(self, source_id: str) -> SourceAsset: ...
    def job_workspace(self, job_id: str) -> Path: ...
    def output_path(self, job_id: str) -> Path: ...
    def cleanup_job(self, job_id: str, *, keep_output: bool) -> None: ...


class LocalStorage:
    """Disk-backed storage rooted at the configured directories."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        settings.ensure_dirs()

    # -- sources ---------------------------------------------------------

    def _check_extension(self, safe_name: str) -> str:
        ext = Path(safe_name).suffix.lower()
        if ext not in self.settings.allowed_video_extensions:
            raise UnsupportedMediaError(
                f"'{ext or safe_name}' is not a supported video format. "
                f"Allowed: {', '.join(sorted(self.settings.allowed_video_extensions))}."
            )
        return ext

    def save_source(
        self, stream: BinaryIO, filename: str, declared_size: int | None = None
    ) -> SourceAsset:
        """Stream an upload to disk in 1 MiB chunks (never buffered in RAM).

        Used by the one-shot multipart endpoint, where the bytes arrive on the
        socket and there is nothing on disk yet. The chunked upload path uses
        :meth:`adopt_source` instead, which avoids copying the file.
        """
        safe_name = sanitize_filename(filename, default="video.mp4")
        ext = self._check_extension(safe_name)
        limit = self.settings.max_upload_bytes
        if declared_size is not None and declared_size > limit:
            raise PayloadTooLargeError(
                f"That file is {declared_size / 1e9:.2f} GB, which exceeds the "
                f"{limit / 1e9:.2f} GB upload limit."
            )

        source_id = new_id("src_").replace("_", "-")
        target = self.settings.upload_dir / f"{source_id}{ext}"
        written = 0
        try:
            with open(target, "wb") as handle:
                while True:
                    chunk = stream.read(_CHUNK)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > limit:
                        raise PayloadTooLargeError(
                            f"Upload exceeds the {limit / 1e9:.2f} GB limit."
                        )
                    handle.write(chunk)
        except BaseException:
            target.unlink(missing_ok=True)
            raise

        if written == 0:
            target.unlink(missing_ok=True)
            raise ValidationError("The uploaded file was empty.")

        asset = SourceAsset(
            id=source_id, original_name=safe_name, extension=ext, size_bytes=written
        )
        self.write_source(asset)
        return asset

    def adopt_source(self, path: Path, filename: str) -> SourceAsset:
        """Take ownership of a file that is already on disk, by moving it.

        The chunked upload assembles the video inside the upload directory, so
        reading it back out and writing it to a second file would copy every
        byte for no reason - minutes of pointless disk I/O on a large video,
        all of it after the progress bar has already hit 100%. A rename is
        effectively instantaneous.

        ``os.replace`` requires both paths to be on one filesystem, which is
        true by construction here; the fallback covers an operator pointing the
        directories at different mounts.
        """
        safe_name = sanitize_filename(filename, default="video.mp4")
        ext = self._check_extension(safe_name)

        size = path.stat().st_size
        if size == 0:
            raise ValidationError("The uploaded file was empty.")
        limit = self.settings.max_upload_bytes
        if size > limit:
            raise PayloadTooLargeError(
                f"That file is {size / 1e9:.2f} GB, which exceeds the "
                f"{limit / 1e9:.2f} GB upload limit."
            )

        source_id = new_id("src_").replace("_", "-")
        target = self.settings.upload_dir / f"{source_id}{ext}"
        try:
            os.replace(path, target)
        except OSError:
            shutil.move(str(path), str(target))

        asset = SourceAsset(
            id=source_id, original_name=safe_name, extension=ext, size_bytes=size
        )
        self.write_source(asset)
        return asset

    def _meta_path(self, source_id: str) -> Path:
        return self.settings.upload_dir / f"{source_id}.json"

    def write_source(self, asset: SourceAsset) -> None:
        _atomic_write_json(self._meta_path(asset.id), asset.to_dict())

    def load_source(self, source_id: str) -> SourceAsset:
        validate_id(source_id, kind="source id")
        meta = self._meta_path(source_id)
        if not meta.is_file():
            raise NotFoundError("That uploaded video is no longer available. Please upload it again.")
        try:
            data = json.loads(meta.read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise NotFoundError("That upload record is unreadable. Please upload the video again.") from exc
        asset = SourceAsset.from_dict(data)
        if not self.source_path(source_id).is_file():
            raise NotFoundError("That uploaded video is no longer on disk. Please upload it again.")
        return asset

    def source_path(self, source_id: str) -> Path:
        validate_id(source_id, kind="source id")
        matches = sorted(
            p for p in self.settings.upload_dir.glob(f"{source_id}.*") if p.suffix.lower() != ".json"
        )
        if not matches:
            raise NotFoundError("That uploaded video is no longer available. Please upload it again.")
        return matches[0]

    def delete_source(self, source_id: str) -> None:
        validate_id(source_id, kind="source id")
        for path in self.settings.upload_dir.glob(f"{source_id}.*"):
            path.unlink(missing_ok=True)

    def list_sources(self) -> list[SourceAsset]:
        out: list[SourceAsset] = []
        for meta in self.settings.upload_dir.glob("*.json"):
            try:
                out.append(SourceAsset.from_dict(json.loads(meta.read_text("utf-8"))))
            except (OSError, json.JSONDecodeError, KeyError):
                continue
        return sorted(out, key=lambda a: a.created_at, reverse=True)

    # -- jobs ------------------------------------------------------------

    def job_workspace(self, job_id: str) -> Path:
        validate_id(job_id, kind="job id")
        workspace = self.settings.jobs_dir / job_id
        workspace.mkdir(parents=True, exist_ok=True)
        return workspace

    def output_path(self, job_id: str) -> Path:
        validate_id(job_id, kind="job id")
        return self.settings.output_dir / f"{job_id}.mp4"

    def cleanup_job(self, job_id: str, *, keep_output: bool = True) -> None:
        """Remove the per-job scratch directory. The MP4 lives elsewhere."""
        validate_id(job_id, kind="job id")
        shutil.rmtree(self.settings.jobs_dir / job_id, ignore_errors=True)
        if not keep_output:
            self.output_path(job_id).unlink(missing_ok=True)

    # -- retention -------------------------------------------------------

    def purge_expired(self, *, now: float | None = None) -> list[str]:
        """Delete outputs, scratch dirs and uploads older than the retention window."""
        now = time.time() if now is None else now
        cutoff = now - self.settings.job_retention_hours * 3600
        removed: list[str] = []
        for base in (self.settings.jobs_dir, self.settings.output_dir, self.settings.upload_dir):
            if not base.is_dir():
                continue
            for entry in base.iterdir():
                try:
                    if entry.stat().st_mtime >= cutoff:
                        continue
                    if entry.is_dir():
                        shutil.rmtree(entry, ignore_errors=True)
                    else:
                        entry.unlink(missing_ok=True)
                    removed.append(str(entry))
                except OSError:
                    continue
        return removed

    def free_bytes(self) -> int:
        usage = shutil.disk_usage(self.settings.data_dir)
        return usage.free


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), "utf-8")
    os.replace(tmp, path)


def iter_file(path: Path, *, start: int = 0, length: int | None = None, chunk: int = _CHUNK) -> Iterator[bytes]:
    """Stream a file (optionally a byte range) without loading it into memory."""
    remaining = length
    with open(path, "rb") as handle:
        handle.seek(start)
        while remaining is None or remaining > 0:
            size = chunk if remaining is None else min(chunk, remaining)
            data = handle.read(size)
            if not data:
                return
            if remaining is not None:
                remaining -= len(data)
            yield data
