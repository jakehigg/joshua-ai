"""The streaming multipart reader: the parts, the limits, and the bad bodies."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from joshua_gateway.viewer_multipart import (
    FIELD_MAX,
    HEADER_MAX,
    SPOOL_MAX,
    BodyTooLarge,
    MultipartError,
    Part,
    boundary_of,
    read_form,
)

BOUNDARY = "----chatBoundary"


def body(*parts: bytes, boundary: str = BOUNDARY, end: bool = True) -> bytes:
    """Build a multipart body out of already framed parts."""
    out = b""
    for part in parts:
        out += f"--{boundary}\r\n".encode() + part + b"\r\n"
    return out + (f"--{boundary}--\r\n".encode() if end else b"")


def field(name: str, value: bytes) -> bytes:
    return f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode() + value


def file_part(name: str, filename: str, kind: str, data: bytes) -> bytes:
    head = (
        f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
        f"Content-Type: {kind}\r\n\r\n"
    ).encode()
    return head + data


async def chunks(data: bytes, size: int = 7) -> AsyncIterator[bytes]:
    """Give ``data`` in small chunks, so every frame is split across them."""
    for start in range(0, len(data), size):
        yield data[start : start + size]


def content_type(boundary: str = BOUNDARY) -> str:
    return f"multipart/form-data; boundary={boundary}"


async def read(data: bytes, limit: int = 1 << 20, size: int = 7) -> list[Part]:
    return await read_form(chunks(data, size), content_type(), limit)


async def test_the_fields_and_the_file_come_out_of_a_split_body() -> None:
    raw = b"\x89PNG\r\n--not-the-boundary\r\n" * 10
    data = body(
        field("text", "what is on this? é".encode()),
        field("spoken", b"brief"),
        file_part("file", "photo.png", "image/png", raw),
    )
    text, spoken, upload = await read(data)
    assert (text.name, text.text(), text.filename) == ("text", "what is on this? é", "")
    assert spoken.text() == "brief"
    assert (upload.name, upload.filename, upload.content_type) == ("file", "photo.png", "image/png")
    assert upload.size == len(raw)
    assert upload.open().read() == raw
    for part in (text, spoken, upload):
        part.close()


async def test_a_file_over_the_spool_goes_to_a_temporary_file() -> None:
    raw = bytes(range(256)) * ((SPOOL_MAX // 256) + 8)
    (upload,) = await read(body(file_part("file", "scan.pdf", "application/pdf", raw)), size=4096)
    assert upload.size == len(raw) > SPOOL_MAX
    assert upload.open().read() == raw
    upload.close()


async def test_a_whole_body_in_one_chunk_and_a_preamble_and_an_epilogue() -> None:
    data = b"ignored preamble\r\n" + body(field("text", b"hi")) + b"ignored epilogue\r\n"
    (text,) = await read(data, size=len(data))
    assert text.text() == "hi"
    text.close()


async def test_an_empty_file_part_keeps_its_name_and_size_zero() -> None:
    (upload,) = await read(body(file_part("file", "empty.pdf", "application/pdf", b"")))
    assert (upload.filename, upload.size) == ("empty.pdf", 0)
    upload.close()


async def test_a_body_over_the_limit_stops_the_read() -> None:
    data = body(file_part("file", "photo.jpg", "image/jpeg", b"x" * 2048))
    with pytest.raises(BodyTooLarge):
        await read(data, limit=512)


async def test_a_field_over_its_own_limit_is_refused() -> None:
    data = body(field("text", b"x" * (FIELD_MAX + 1)))
    with pytest.raises(MultipartError):
        await read(data, size=4096)


@pytest.mark.parametrize(
    "data",
    [
        body(field("text", b"hi"), end=False),  # cut short
        body(b"Content-Disposition: form-data\r\n\r\nno name"),
        body(field("text", b"hi"), boundary="other"),  # another boundary
        b"--" + BOUNDARY.encode() + b"x\r\n",  # a malformed boundary line
        b"",
    ],
)
async def test_a_body_that_is_not_well_formed_is_refused(data: bytes) -> None:
    with pytest.raises(MultipartError):
        await read(data)


async def test_part_headers_that_never_end_are_refused() -> None:
    data = f"--{BOUNDARY}\r\n".encode() + b"X-Long: " + b"y" * (HEADER_MAX + 1)
    with pytest.raises(MultipartError):
        await read(data, size=4096)


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        (content_type(), BOUNDARY.encode()),
        ('multipart/form-data; boundary="quoted-one"', b"quoted-one"),
        ("application/json", None),
        ("multipart/form-data", None),
        ("", None),
    ],
)
async def test_boundary_of_reads_the_content_type(kind: str, expected: bytes | None) -> None:
    assert boundary_of(kind) == expected


async def test_a_content_type_with_no_boundary_is_refused() -> None:
    with pytest.raises(MultipartError):
        await read_form(chunks(b""), "application/json", 1 << 20)
