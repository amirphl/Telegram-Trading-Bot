from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

ALLOWED_IMAGE_MIME_TYPES = frozenset(
    {
        "image/jpeg",
        "image/png",
        "image/webp",
    }
)


@dataclass(frozen=True)
class ImageFileInfo:
    mime_type: str
    file_size: int
    width: int
    height: int

    @property
    def pixels(self) -> int:
        return self.width * self.height


def detect_image_mime(header: bytes) -> str | None:
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image/webp"
    return None


def _jpeg_dimensions(path: Path) -> tuple[int, int]:
    with path.open("rb") as fh:
        if fh.read(2) != b"\xff\xd8":
            raise ValueError("invalid JPEG signature")
        while True:
            byte = fh.read(1)
            while byte == b"\xff":
                byte = fh.read(1)
            if not byte:
                break
            marker = byte[0]
            if marker in (0xD8, 0xD9):
                continue
            length_raw = fh.read(2)
            if len(length_raw) != 2:
                break
            length = struct.unpack(">H", length_raw)[0]
            if length < 2:
                break
            if marker in {
                0xC0,
                0xC1,
                0xC2,
                0xC3,
                0xC5,
                0xC6,
                0xC7,
                0xC9,
                0xCA,
                0xCB,
                0xCD,
                0xCE,
                0xCF,
            }:
                payload = fh.read(length - 2)
                if len(payload) < 5:
                    break
                height, width = struct.unpack(">HH", payload[1:5])
                return width, height
            fh.seek(length - 2, 1)
    raise ValueError("JPEG dimensions not found")


def _webp_dimensions(header: bytes) -> tuple[int, int]:
    if len(header) < 30:
        raise ValueError("truncated WebP header")
    chunk = header[12:16]
    if chunk == b"VP8X":
        width = 1 + int.from_bytes(header[24:27], "little")
        height = 1 + int.from_bytes(header[27:30], "little")
        return width, height
    if chunk == b"VP8 ":
        if header[23:26] != b"\x9d\x01\x2a":
            raise ValueError("invalid lossy WebP frame")
        width = int.from_bytes(header[26:28], "little") & 0x3FFF
        height = int.from_bytes(header[28:30], "little") & 0x3FFF
        return width, height
    if chunk == b"VP8L":
        if header[20] != 0x2F:
            raise ValueError("invalid lossless WebP frame")
        bits = int.from_bytes(header[21:25], "little")
        width = 1 + (bits & 0x3FFF)
        height = 1 + ((bits >> 14) & 0x3FFF)
        return width, height
    raise ValueError("unsupported WebP encoding")


def image_dimensions(path: Path, mime_type: str) -> tuple[int, int]:
    if mime_type == "image/jpeg":
        return _jpeg_dimensions(path)
    with path.open("rb") as fh:
        header = fh.read(32)
    if mime_type == "image/png":
        if len(header) < 24:
            raise ValueError("truncated PNG header")
        return struct.unpack(">II", header[16:24])
    if mime_type == "image/webp":
        return _webp_dimensions(header)
    raise ValueError(f"unsupported image MIME type: {mime_type}")


def inspect_image_file(
    path: Path,
    *,
    max_bytes: int,
    max_pixels: int,
) -> ImageFileInfo:
    size = path.stat().st_size
    if size <= 0:
        raise ValueError("image file is empty")
    if size > max_bytes:
        raise ValueError(f"image exceeds byte limit ({size} > {max_bytes})")
    with path.open("rb") as fh:
        mime_type = detect_image_mime(fh.read(32))
    if mime_type not in ALLOWED_IMAGE_MIME_TYPES:
        raise ValueError("file content is not a supported image")
    width, height = image_dimensions(path, mime_type)
    if width <= 0 or height <= 0:
        raise ValueError("image dimensions are invalid")
    pixels = width * height
    if pixels > max_pixels:
        raise ValueError(f"image exceeds pixel limit ({pixels} > {max_pixels})")
    return ImageFileInfo(mime_type, size, width, height)


def is_path_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False
