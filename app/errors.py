"""Typed application errors.

Every error carries a machine-readable ``code``, an HTTP status and a
user-safe ``message``. Technical details live in ``detail`` and are only ever
written to the server log, never returned to the browser.
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    code = "app_error"
    http_status = 400

    def __init__(self, message: str, *, detail: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail

    def to_dict(self) -> dict:
        return {"error": self.code, "message": self.message}


class ValidationError(AppError):
    code = "validation_error"
    http_status = 400


class NotFoundError(AppError):
    code = "not_found"
    http_status = 404


class UnsupportedMediaError(AppError):
    code = "unsupported_media"
    http_status = 415


class PayloadTooLargeError(AppError):
    code = "payload_too_large"
    http_status = 413


class DependencyError(AppError):
    """A required external tool (ffmpeg, a model, a font) is unavailable."""

    code = "dependency_unavailable"
    http_status = 500


class ProcessingError(AppError):
    code = "processing_failed"
    http_status = 500
