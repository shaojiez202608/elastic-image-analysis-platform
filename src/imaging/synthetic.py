"""Synthetic images and deliberately bad files (no real images anywhere in the project).

Good images: random geometric shapes on a gradient, deterministic per seed.
Bad files (each must be rejected with the listed reason, and must never block the queue):
    truncated_jpeg     -> corrupt
    text_as_jpeg       -> unsupported_type
    png_declared_jpeg  -> type_mismatch
    bomb_png           -> decompression_bomb (tiny file, huge declared dimensions)
    tiny_png           -> dimensions_out_of_range
    empty              -> empty
"""

from __future__ import annotations

import io
import random
import struct
import zlib

from PIL import Image, ImageDraw

BAD_CASES = {
    "truncated_jpeg": ("image/jpeg", "corrupt"),
    "text_as_jpeg": ("image/jpeg", "unsupported_type"),
    "png_declared_jpeg": ("image/jpeg", "type_mismatch"),
    "bomb_png": ("image/png", "decompression_bomb"),
    "tiny_png": ("image/png", "dimensions_out_of_range"),
    "empty": ("image/jpeg", "empty"),
}


def good_image(seed: int, size: tuple[int, int] = (320, 240), fmt: str = "JPEG") -> bytes:
    rnd = random.Random(seed)  # noqa: S311 - synthetic test images, not security  # nosec B311
    w, h = size
    img = Image.new("RGB", size)
    draw = ImageDraw.Draw(img)
    c1 = [rnd.randrange(256) for _ in range(3)]
    c2 = [rnd.randrange(256) for _ in range(3)]
    for y in range(h):
        t = y / max(1, h - 1)
        draw.line([(0, y), (w, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(c1, c2, strict=True)))
    for _ in range(rnd.randint(2, 6)):
        x0, y0 = rnd.randrange(w), rnd.randrange(h)
        x1, y1 = min(w, x0 + rnd.randint(1, max(1, w // 2))), min(h, y0 + rnd.randint(1, max(1, h // 2)))
        color = tuple(rnd.randrange(256) for _ in range(3))
        shape = rnd.choice(("rect", "ellipse", "line"))
        if shape == "rect":
            draw.rectangle([x0, y0, x1, y1], fill=color)
        elif shape == "ellipse":
            draw.ellipse([x0, y0, x1, y1], fill=color)
        else:
            draw.line([x0, y0, x1, y1], fill=color, width=rnd.randint(2, 8))
    out = io.BytesIO()
    img.save(out, format=fmt, **({"quality": 85} if fmt == "JPEG" else {}))
    return out.getvalue()


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def bomb_png(width: int = 60000, height: int = 60000) -> bytes:
    """Valid PNG header declaring a huge canvas, with a tiny (incomplete) compressed body."""
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    idat = zlib.compress(b"\x00" * 1024)
    return b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", ihdr) + _png_chunk(b"IDAT", idat) + _png_chunk(b"IEND", b"")


def bad_file(case: str) -> tuple[bytes, str, str]:
    """Returns (data, declared content type, expected reject reason)."""
    ctype, reason = BAD_CASES[case]
    if case == "truncated_jpeg":
        data = good_image(1)[:400]
    elif case == "text_as_jpeg":
        data = b"this is not an image\n" * 20
    elif case == "png_declared_jpeg":
        data = good_image(2, fmt="PNG")
    elif case == "bomb_png":
        data = bomb_png()
    elif case == "tiny_png":
        data = good_image(3, size=(8, 8), fmt="PNG")
    else:
        data = b""
    return data, ctype, reason
