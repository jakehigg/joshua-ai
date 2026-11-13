"""A streaming reader of ``multipart/form-data``.

A turn can carry one photo or one PDF beside the text. Each part is written
to a temporary file as it arrives, and the file part is streamed on to
channels from there, so no request holds the whole body in memory. The
standard library ``email`` parser needs the whole body at once, so the
frames are parsed here instead.
"""

from __future__ import annotations

import tempfile
from collections.abc import AsyncIterator
from email.message import Message
from typing import IO

CRLF = b"\r\n"
# A part up to this size stays in memory; a larger one goes to a temporary file.
SPOOL_MAX = 64 * 1024
# A part with no file name (``text``, ``spoken``) over this is refused.
FIELD_MAX = 64 * 1024
# The headers of one part.
HEADER_MAX = 16 * 1024


class MultipartError(Exception):
    """The body is not a well-formed ``multipart/form-data`` body."""


class BodyTooLarge(Exception):
    """The body passed the byte limit."""


def boundary_of(content_type: str) -> bytes | None:
    """Return the boundary of a ``multipart/form-data`` content type, else ``None``."""
    header = Message()
    header["Content-Type"] = content_type
    if header.get_content_type() != "multipart/form-data":
        return None
    marker = header.get_param("boundary")
    if not isinstance(marker, str):
        return None
    return marker.encode("utf-8") or None


class Part:
    """One part of the body. Its bytes are in memory, or in a temporary file."""

    def __init__(self, name: str, filename: str, content_type: str) -> None:
        self.name = name
        self.filename = filename
        self.content_type = content_type
        self.size = 0
        self._file: IO[bytes] = tempfile.SpooledTemporaryFile(max_size=SPOOL_MAX)

    def write(self, data: bytes) -> None:
        self.size += len(data)
        if not self.filename and self.size > FIELD_MAX:
            raise MultipartError(f"the field {self.name} is too long")
        self._file.write(data)

    def open(self) -> IO[bytes]:
        """Return the bytes of the part as a binary stream, at its start."""
        self._file.seek(0)
        return self._file

    def text(self) -> str:
        """Return the bytes of the part as text."""
        return self.open().read().decode("utf-8", "replace")

    def close(self) -> None:
        self._file.close()


def _part_of(raw: bytes) -> Part:
    """Build the part that the headers ``raw`` describe."""
    headers = Message()
    for line in raw.decode("utf-8", "replace").split("\r\n"):
        field, separator, value = line.partition(":")
        if separator:
            headers[field.strip()] = value.strip()
    name = headers.get_param("name", header="content-disposition")
    if not isinstance(name, str) or not name:
        raise MultipartError("a part has no name")
    filename = headers.get_filename() or ""
    kind = str(headers.get("content-type") or "").split(";")[0].strip()
    return Part(name, str(filename), kind)


class MultipartReader:
    """Parses ``multipart/form-data`` as it arrives, into ``parts``.

    ``feed`` takes the body in chunks of any size; ``close`` ends it. Every
    part holds its bytes, so the caller closes each one it does not keep.
    """

    def __init__(self, boundary: bytes) -> None:
        self._delimiter = b"--" + boundary
        self._buffer = bytearray()
        self._state = "preamble"
        self._part: Part | None = None
        self.parts: list[Part] = []

    def feed(self, data: bytes) -> None:
        self._buffer += data
        while self._step():
            pass

    def close(self) -> list[Part]:
        """Return the parts. Raise ``MultipartError`` when the body is cut short."""
        if self._state != "done":
            raise MultipartError("the body ends before the last boundary")
        self._buffer.clear()
        return self.parts

    def _step(self) -> bool:
        """Do one step of the machine. Return True when another one can run now."""
        if self._state == "preamble":
            return self._preamble()
        if self._state == "boundary":
            return self._boundary()
        if self._state == "headers":
            return self._headers()
        if self._state == "body":
            return self._body()
        self._buffer.clear()  # done: the epilogue is dropped
        return False

    def _preamble(self) -> bool:
        index = self._buffer.find(self._delimiter)
        if index < 0:
            self._keep_tail(len(self._delimiter))
            return False
        del self._buffer[: index + len(self._delimiter)]
        self._state = "boundary"
        return True

    def _boundary(self) -> bool:
        """After a delimiter: ``--`` ends the body, ``\\r\\n`` starts a part."""
        if len(self._buffer) < 2:
            return False
        if self._buffer[:2] == b"--":
            self._state = "done"
            self._buffer.clear()
            return False
        if self._buffer[:2] != CRLF:
            raise MultipartError("a boundary line is malformed")
        del self._buffer[:2]
        self._state = "headers"
        return True

    def _headers(self) -> bool:
        index = self._buffer.find(CRLF + CRLF)
        if index < 0:
            if len(self._buffer) > HEADER_MAX:
                raise MultipartError("the headers of a part are too long")
            return False
        raw = bytes(self._buffer[:index])
        del self._buffer[: index + 2 * len(CRLF)]
        self._part = _part_of(raw)
        self.parts.append(self._part)
        self._state = "body"
        return True

    def _body(self) -> bool:
        needle = CRLF + self._delimiter
        index = self._buffer.find(needle)
        if index < 0:
            # Keep what a delimiter could still begin with; write the rest.
            self._flush(len(self._buffer) - (len(needle) - 1))
            return False
        self._flush(index)
        del self._buffer[: len(needle)]
        self._part = None
        self._state = "boundary"
        return True

    def _flush(self, count: int) -> None:
        if count <= 0 or self._part is None:
            return
        self._part.write(bytes(self._buffer[:count]))
        del self._buffer[:count]

    def _keep_tail(self, size: int) -> None:
        """Drop everything but the last ``size - 1`` bytes, which can still match."""
        extra = len(self._buffer) - (size - 1)
        if extra > 0:
            del self._buffer[:extra]


async def read_form(chunks: AsyncIterator[bytes], content_type: str, limit: int) -> list[Part]:
    """Read a multipart body from ``chunks`` and return its parts.

    Raises ``BodyTooLarge`` as soon as the body passes ``limit`` bytes, and
    ``MultipartError`` for a body that is not well formed. No part survives
    either error.
    """
    marker = boundary_of(content_type)
    if marker is None:
        raise MultipartError("the content type names no boundary")
    reader = MultipartReader(marker)
    total = 0
    try:
        async for chunk in chunks:
            total += len(chunk)
            if total > limit:
                raise BodyTooLarge
            reader.feed(chunk)
        return reader.close()
    except BaseException:
        for part in reader.parts:
            part.close()
        raise
