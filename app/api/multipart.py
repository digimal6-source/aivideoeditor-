"""Streaming multipart/form-data parser.

Written deliberately rather than pulled from a framework because the whole point
is that a multi-gigabyte upload must never be buffered in memory. File parts are
exposed as read-only streams that are piped straight to disk by the storage
layer.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from typing import Iterator

from ..errors import PayloadTooLargeError, ValidationError

_CHUNK = 64 * 1024
_DISPOSITION = re.compile(r'(\w+)="([^"]*)"')


def parse_boundary(content_type: str) -> bytes:
    if "multipart/form-data" not in (content_type or "").lower():
        raise ValidationError("That request must be sent as a file upload.")
    for token in content_type.split(";"):
        key, _, value = token.strip().partition("=")
        if key.strip().lower() == "boundary":
            boundary = value.strip().strip('"')
            if boundary:
                return boundary.encode("latin-1")
    raise ValidationError("The upload was malformed (no multipart boundary).")


class _Source:
    """A pushback-buffered view over the raw request body."""

    def __init__(self, rfile, remaining: int) -> None:
        self.rfile = rfile
        self.remaining = max(0, remaining)
        self.buf = bytearray()

    def fill(self, target: int) -> None:
        while len(self.buf) < target and self.remaining > 0:
            chunk = self.rfile.read(min(_CHUNK, self.remaining))
            if not chunk:
                self.remaining = 0
                break
            self.remaining -= len(chunk)
            self.buf += chunk

    def readline(self) -> bytes:
        while b"\n" not in self.buf and self.remaining > 0:
            self.fill(len(self.buf) + _CHUNK)
        index = self.buf.find(b"\n")
        if index == -1:
            line = bytes(self.buf)
            self.buf.clear()
            return line
        line = bytes(self.buf[: index + 1])
        del self.buf[: index + 1]
        return line

    @property
    def exhausted(self) -> bool:
        return self.remaining <= 0 and not self.buf


class PartStream(io.RawIOBase):
    """Read-only stream over a single multipart part body."""

    def __init__(self, source: _Source, delimiter: bytes) -> None:
        self._source = source
        self._delimiter = delimiter
        self._done = False

    def readable(self) -> bool:  # pragma: no cover - trivial
        return True

    def read(self, size: int = -1) -> bytes:  # type: ignore[override]
        """Read from the part body.

        A negative ``size`` follows the ``RawIOBase`` contract and reads the
        whole remaining part, not just the next internal chunk.
        """
        if size is None or size < 0:
            chunks: list[bytes] = []
            while not self._done:
                chunk = self._read_chunk(_CHUNK)
                if chunk:
                    chunks.append(chunk)
                elif self._source.remaining <= 0:
                    break
            return b"".join(chunks)
        return self._read_chunk(size)

    def _read_chunk(self, size: int) -> bytes:
        if self._done:
            return b""
        want = max(0, size)
        if want == 0:
            return b""
        source = self._source
        source.fill(want + len(self._delimiter) + 2)
        index = source.buf.find(self._delimiter)

        if index == -1:
            # Hold back enough bytes that a delimiter split across two reads is
            # still detectable on the next call.
            keep = len(self._delimiter) - 1
            available = max(0, len(source.buf) - keep)
            if available == 0:
                if source.remaining <= 0:
                    self._done = True
                    data = bytes(source.buf)
                    source.buf.clear()
                    return data
                return b""
            take = min(want, available)
        else:
            take = min(want, index)
            if take == 0:
                self._done = True
                del source.buf[: len(self._delimiter)]
                return b""

        data = bytes(source.buf[:take])
        del source.buf[:take]
        return data

    def drain(self) -> None:
        while self.read(_CHUNK):
            pass

    @property
    def finished(self) -> bool:
        return self._done


@dataclass
class Part:
    name: str
    filename: str | None
    content_type: str
    stream: PartStream

    @property
    def is_file(self) -> bool:
        return self.filename is not None

    def text(self, limit: int = 1_000_000) -> str:
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = self.stream.read(_CHUNK)
            if not chunk:
                if self.stream.finished:
                    break
                continue
            total += len(chunk)
            if total > limit:
                raise PayloadTooLargeError("A form field in that request was too large.")
            chunks.append(chunk)
        return b"".join(chunks).decode("utf-8", errors="replace")


def iter_parts(rfile, *, content_type: str, content_length: int) -> Iterator[Part]:
    """Yield each multipart part in order.

    Each part's stream must be fully consumed before advancing the iterator.
    """
    boundary = parse_boundary(content_type)
    opening = b"--" + boundary
    delimiter = b"\r\n--" + boundary
    source = _Source(rfile, content_length)

    # Skip the preamble up to the first boundary line.
    while True:
        line = source.readline()
        if not line:
            return
        if line.rstrip(b"\r\n") == opening:
            break
        if line.rstrip(b"\r\n") == opening + b"--":
            return

    while True:
        name = ""
        filename: str | None = None
        part_type = "application/octet-stream"

        while True:
            line = source.readline()
            if not line or line in (b"\r\n", b"\n"):
                break
            header = line.decode("utf-8", errors="replace").strip()
            lowered = header.lower()
            if lowered.startswith("content-disposition:"):
                for key, value in _DISPOSITION.findall(header):
                    if key == "name":
                        name = value
                    elif key == "filename":
                        filename = value
            elif lowered.startswith("content-type:"):
                part_type = header.split(":", 1)[1].strip()

        stream = PartStream(source, delimiter)
        yield Part(name=name, filename=filename, content_type=part_type, stream=stream)
        stream.drain()

        trailer = source.readline().rstrip(b"\r\n")
        if trailer == b"--" or source.exhausted:
            return
