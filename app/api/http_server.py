"""HTTP server built on the Python standard library.

Why stdlib rather than FastAPI: this app must start in a fresh Codespace with
zero Python packages beyond Pillow, and it must never fail because a wheel could
not be fetched. The routing table below is small and explicit, and the service
layer it calls is framework-agnostic, so moving to ASGI later is a mechanical
change confined to this one file.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urlparse

from ..errors import AppError, NotFoundError, PayloadTooLargeError, ValidationError
from ..settings import Settings
from ..storage import iter_file
from . import multipart
from .service import AppService

log = logging.getLogger(__name__)

_MAX_JSON_BYTES = 2 * 1024 * 1024
_RANGE = re.compile(r"bytes=(\d*)-(\d*)")

Route = tuple[str, re.Pattern[str], str]

_ROUTES: list[Route] = [
    ("GET", re.compile(r"^/api/health$"), "health"),
    ("GET", re.compile(r"^/api/config$"), "config"),
    ("GET", re.compile(r"^/api/fonts$"), "list_fonts"),
    ("POST", re.compile(r"^/api/fonts$"), "upload_font"),
    ("DELETE", re.compile(r"^/api/fonts/(?P<font_id>[A-Za-z0-9_.-]+)$"), "delete_font"),
    ("GET", re.compile(r"^/api/presets$"), "list_presets"),
    ("POST", re.compile(r"^/api/presets$"), "save_preset"),
    ("DELETE", re.compile(r"^/api/presets/(?P<preset_id>[A-Za-z0-9_-]+)$"), "delete_preset"),
    ("GET", re.compile(r"^/api/sources$"), "list_sources"),
    ("POST", re.compile(r"^/api/sources$"), "create_source"),
    ("GET", re.compile(r"^/api/sources/(?P<source_id>[A-Za-z0-9_-]+)$"), "get_source"),
    ("DELETE", re.compile(r"^/api/sources/(?P<source_id>[A-Za-z0-9_-]+)$"), "delete_source"),
    ("GET", re.compile(r"^/api/jobs$"), "list_jobs"),
    ("POST", re.compile(r"^/api/jobs$"), "create_job"),
    ("GET", re.compile(r"^/api/jobs/(?P<job_id>[A-Za-z0-9_-]+)$"), "get_job"),
    ("POST", re.compile(r"^/api/jobs/(?P<job_id>[A-Za-z0-9_-]+)/cancel$"), "cancel_job"),
    ("GET", re.compile(r"^/api/jobs/(?P<job_id>[A-Za-z0-9_-]+)/download$"), "download"),
    ("GET", re.compile(r"^/api/jobs/(?P<job_id>[A-Za-z0-9_-]+)/preview$"), "preview"),
    ("POST", re.compile(r"^/api/maintenance/purge$"), "purge"),
]


class Handler(BaseHTTPRequestHandler):
    server_version = "Clipforge"
    protocol_version = "HTTP/1.1"

    service: AppService
    settings: Settings

    # -- plumbing --------------------------------------------------------

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        log.info("%s - %s", self.address_string(), fmt % args)

    def _cors(self) -> None:
        origin = self.settings.cors_allow_origin
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")

    def _send_json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _send_error_payload(self, error: AppError) -> None:
        # The user sees a readable sentence; the technical detail stays in the log.
        if error.detail:
            log.warning("%s: %s", error.code, error.detail)
        self._send_json(error.to_dict(), status=error.http_status)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > _MAX_JSON_BYTES:
            raise PayloadTooLargeError("That request was too large.")
        try:
            return json.loads(self.rfile.read(length).decode("utf-8")) or {}
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValidationError("That request could not be read as JSON.") from exc

    # -- dispatch --------------------------------------------------------

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        path = urlparse(self.path).path
        try:
            for route_method, pattern, handler_name in _ROUTES:
                if route_method != method:
                    continue
                match = pattern.match(path)
                if match:
                    handler: Callable = getattr(self, f"api_{handler_name}")
                    handler(**match.groupdict())
                    return
            if method == "GET":
                self._serve_static(path)
                return
            raise NotFoundError("That endpoint does not exist.")
        except AppError as exc:
            self._send_error_payload(exc)
        except BrokenPipeError:  # client navigated away mid-download
            pass
        except Exception:
            log.exception("Unhandled error for %s %s", method, path)
            self._send_json(
                {
                    "error": {
                        "code": "internal_error",
                        "message": "Something went wrong on the server. Check the log for details.",
                    }
                },
                status=500,
            )

    # -- endpoints -------------------------------------------------------

    def api_health(self) -> None:
        self._send_json(self.service.health())

    def api_config(self) -> None:
        self._send_json(self.service.config())

    def api_list_fonts(self) -> None:
        self._send_json({"fonts": self.service.list_fonts()})

    def api_upload_font(self) -> None:
        for part in self._iter_upload():
            if part.is_file:
                record = self.service.upload_font(part.stream, part.filename or "font.ttf")
                self._send_json({"font": record, "fonts": self.service.list_fonts()}, 201)
                return
        raise ValidationError("No font file was included in that upload.")

    def api_delete_font(self, font_id: str) -> None:
        self.service.delete_font(font_id)
        self._send_json({"deleted": font_id, "fonts": self.service.list_fonts()})

    def api_list_presets(self) -> None:
        self._send_json({"presets": self.service.list_presets()})

    def api_save_preset(self) -> None:
        self._send_json({"preset": self.service.save_preset(self._read_json())}, 201)

    def api_delete_preset(self, preset_id: str) -> None:
        self.service.delete_preset(preset_id)
        self._send_json({"deleted": preset_id, "presets": self.service.list_presets()})

    def api_list_sources(self) -> None:
        self._send_json({"sources": self.service.list_sources()})

    def api_create_source(self) -> None:
        declared = int(self.headers.get("Content-Length") or 0)
        if declared > self.settings.max_upload_bytes + 1_000_000:
            raise PayloadTooLargeError(
                "That video is larger than the configured upload limit. "
                "Raise MAX_UPLOAD_BYTES in .env if you need bigger files."
            )
        for part in self._iter_upload():
            if part.is_file:
                result = self.service.create_source(part.stream, part.filename or "video.mp4")
                self._send_json(result, 201)
                return
        raise ValidationError("No video file was included in that upload.")

    def api_get_source(self, source_id: str) -> None:
        self._send_json({"source": self.service.get_source(source_id)})

    def api_delete_source(self, source_id: str) -> None:
        self.service.delete_source(source_id)
        self._send_json({"deleted": source_id})

    def api_list_jobs(self) -> None:
        self._send_json({"jobs": self.service.list_jobs()})

    def api_create_job(self) -> None:
        self._send_json({"job": self.service.create_job(self._read_json())}, 202)

    def api_get_job(self, job_id: str) -> None:
        self._send_json({"job": self.service.get_job(job_id)})

    def api_cancel_job(self, job_id: str) -> None:
        self._send_json({"job": self.service.cancel_job(job_id)})

    def api_download(self, job_id: str) -> None:
        self._send_file(self.service.job_output(job_id), download_name=f"{job_id}.mp4")

    def api_preview(self, job_id: str) -> None:
        self._send_file(self.service.job_output(job_id), download_name=None)

    def api_purge(self) -> None:
        self._send_json(self.service.purge())

    # -- upload helper ---------------------------------------------------

    def _iter_upload(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            raise ValidationError("That upload was empty.")
        return multipart.iter_parts(
            self.rfile,
            content_type=self.headers.get("Content-Type") or "",
            content_length=length,
        )

    # -- file responses (with Range support so <video> can seek) ---------

    def _send_file(self, path: Path, *, download_name: str | None) -> None:
        size = path.stat().st_size
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        start, end = 0, size - 1
        status = 200

        range_header = self.headers.get("Range")
        if range_header:
            match = _RANGE.match(range_header.strip())
            if match:
                raw_start, raw_end = match.groups()
                if raw_start:
                    start = int(raw_start)
                    end = int(raw_end) if raw_end else size - 1
                elif raw_end:  # suffix range: last N bytes
                    start = max(0, size - int(raw_end))
                end = min(end, size - 1)
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self._cors()
                    self.end_headers()
                    return
                status = 206

        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if download_name:
            self.send_header("Content-Disposition", f'attachment; filename="{download_name}"')
        self._cors()
        self.end_headers()
        for chunk in iter_file(path, start=start, length=length):
            self.wfile.write(chunk)

    # -- static frontend --------------------------------------------------

    def _serve_static(self, path: str) -> None:
        root = self.settings.web_dir.resolve()
        relative = unquote(path).lstrip("/") or "index.html"
        candidate = (root / relative).resolve()

        # Path traversal guard: the resolved file must stay inside the web root.
        if root != candidate and root not in candidate.parents:
            raise NotFoundError("That page does not exist.")
        if candidate.is_dir():
            candidate = candidate / "index.html"
        if not candidate.is_file():
            candidate = root / "index.html"
        if not candidate.is_file():
            raise NotFoundError("The web interface is not installed.")

        body = candidate.read_bytes()
        content_type = mimetypes.guess_type(candidate.name)[0] or "text/plain"
        self.send_response(200)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)


def create_server(settings: Settings, service: AppService | None = None) -> ThreadingHTTPServer:
    resolved = service or AppService(settings)

    handler = type(
        "BoundHandler",
        (Handler,),
        {"service": resolved, "settings": settings},
    )
    server = ThreadingHTTPServer((settings.host, settings.port), handler)
    server.daemon_threads = True
    server.app_service = resolved  # type: ignore[attr-defined]
    return server


def serve(settings: Settings) -> None:
    mimetypes.add_type("application/javascript", ".js")
    server = create_server(settings)
    service: AppService = server.app_service  # type: ignore[attr-defined]

    # Periodic cleanup of expired jobs, uploads and outputs.
    stop = threading.Event()

    def janitor() -> None:
        while not stop.wait(900):
            try:
                service.purge()
            except Exception:  # pragma: no cover - housekeeping must never crash
                log.exception("Cleanup pass failed")

    threading.Thread(target=janitor, name="janitor", daemon=True).start()

    log.info("Clipforge listening on http://%s:%s", settings.host, settings.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down")
    finally:
        stop.set()
        service.shutdown()
        server.server_close()
