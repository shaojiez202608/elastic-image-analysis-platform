"""Untrusted-image validation and normalisation.

Uploaded files are untrusted. Before anything is sent to the model the worker:
1. checks the size against the configured limit (the object is read with a hard byte cap);
2. detects the type from magic bytes (JPEG / PNG only) and compares it with the declared type;
3. opens the image with Pillow under a pixel limit (decompression-bomb protection), verifies it,
   checks the dimensions, and
4. re-encodes it as a fresh RGB JPEG no larger than `normalize_max_side`, so the model container only
   ever receives an image produced by this code (metadata, ICC profiles and trailing data are dropped).

Every failure maps to a fixed reason code; no file content or parser message is returned or logged.
"""

from __future__ import annotations

import io
import warnings
from dataclasses import dataclass

from PIL import Image, UnidentifiedImageError

JPEG = "image/jpeg"
PNG = "image/png"
SUPPORTED_TYPES = (JPEG, PNG)
MAGIC = {JPEG: (b"\xff\xd8\xff",), PNG: (b"\x89PNG\r\n\x1a\n",)}
PIL_FORMATS = {"JPEG": JPEG, "PNG": PNG}

REASONS = (
    "empty",
    "too_large",
    "unsupported_type",
    "type_mismatch",
    "corrupt",
    "decompression_bomb",
    "dimensions_out_of_range",
)


class ImageRejected(Exception):  # noqa: N818 - domain outcome, not a programming error
    """Permanent failure: the file is not processable. `reason` is one of REASONS."""

    def __init__(self, reason: str):
        if reason not in REASONS:
            raise ValueError(f"unknown reject reason {reason!r}")
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Limits:
    max_bytes: int
    max_pixels: int
    min_side: int
    max_side: int
    normalize_max_side: int

    def __post_init__(self) -> None:
        if not (0 < self.min_side <= self.max_side) or self.max_bytes <= 0 or self.max_pixels <= 0:
            raise ValueError("invalid image limits")
        if self.normalize_max_side <= 0:
            raise ValueError("normalize_max_side must be positive")


@dataclass(frozen=True)
class ValidatedImage:
    detected_type: str
    width: int
    height: int
    size_bytes: int
    normalized: bytes  # JPEG bytes sent to the model
    normalized_type: str = JPEG


def detect_type(data: bytes) -> str | None:
    for ctype, magics in MAGIC.items():
        if any(data.startswith(m) for m in magics):
            return ctype
    return None


def validate_image(data: bytes, declared_type: str | None, limits: Limits) -> ValidatedImage:
    if not data:
        raise ImageRejected("empty")
    if len(data) > limits.max_bytes:
        raise ImageRejected("too_large")
    detected = detect_type(data)
    if detected is None:
        raise ImageRejected("unsupported_type")
    if declared_type and declared_type.split(";")[0].strip().lower() != detected:
        raise ImageRejected("type_mismatch")
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        old_limit = Image.MAX_IMAGE_PIXELS
        Image.MAX_IMAGE_PIXELS = limits.max_pixels
        try:
            return _decode(data, detected, limits)
        except (Image.DecompressionBombError, Image.DecompressionBombWarning):
            raise ImageRejected("decompression_bomb") from None
        finally:
            Image.MAX_IMAGE_PIXELS = old_limit


def _decode(data: bytes, detected: str, limits: Limits) -> ValidatedImage:
    try:
        with Image.open(io.BytesIO(data)) as probe:
            if PIL_FORMATS.get(probe.format or "") != detected:
                raise ImageRejected("type_mismatch")
            width, height = probe.size
            if width * height > limits.max_pixels:
                raise ImageRejected("decompression_bomb")
            if min(width, height) < limits.min_side or max(width, height) > limits.max_side:
                raise ImageRejected("dimensions_out_of_range")
            probe.verify()
        # verify() leaves the image unusable; decode again for the real pixel data.
        with Image.open(io.BytesIO(data)) as img:
            img.load()
            rgb = img.convert("RGB")
    except ImageRejected:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, EOFError):
        raise ImageRejected("corrupt") from None
    rgb.thumbnail((limits.normalize_max_side, limits.normalize_max_side))
    out = io.BytesIO()
    rgb.save(out, format="JPEG", quality=90)
    return ValidatedImage(detected, width, height, len(data), out.getvalue())
