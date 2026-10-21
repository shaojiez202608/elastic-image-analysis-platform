import pytest

from imaging.synthetic import BAD_CASES, bad_file, good_image
from imaging.validation import ImageRejected, Limits, validate_image

LIMITS = Limits(max_bytes=2_000_000, max_pixels=20_000_000, min_side=32, max_side=8192, normalize_max_side=256)


def test_good_jpeg_is_normalised():
    v = validate_image(good_image(5, (640, 480)), "image/jpeg", LIMITS)
    assert (v.width, v.height, v.detected_type) == (640, 480, "image/jpeg")
    assert v.normalized.startswith(b"\xff\xd8\xff")
    from io import BytesIO

    from PIL import Image
    assert max(Image.open(BytesIO(v.normalized)).size) == 256


def test_png_accepted_and_reencoded_as_jpeg():
    v = validate_image(good_image(6, fmt="PNG"), "image/png", LIMITS)
    assert v.detected_type == "image/png" and v.normalized_type == "image/jpeg"


@pytest.mark.parametrize("case", sorted(BAD_CASES))
def test_bad_files_rejected_with_reason(case):
    data, ctype, reason = bad_file(case)
    with pytest.raises(ImageRejected) as e:
        validate_image(data, ctype, LIMITS)
    assert e.value.reason == reason


def test_too_large():
    data = good_image(7)
    with pytest.raises(ImageRejected) as e:
        validate_image(data, "image/jpeg", Limits(len(data) - 1, 10**7, 32, 8192, 256))
    assert e.value.reason == "too_large"


def test_dimension_upper_bound():
    with pytest.raises(ImageRejected) as e:
        validate_image(good_image(8, (500, 100)), "image/jpeg", Limits(10**7, 10**7, 32, 400, 256))
    assert e.value.reason == "dimensions_out_of_range"


def test_pixel_limit_below_bomb_threshold_is_enforced():
    with pytest.raises(ImageRejected) as e:
        validate_image(good_image(9, (400, 400)), "image/jpeg", Limits(10**7, 100_000, 32, 8192, 256))
    assert e.value.reason == "decompression_bomb"


def test_trailing_garbage_is_dropped_by_reencoding():
    data = good_image(10) + b"<script>payload</script>"
    v = validate_image(data, "image/jpeg", LIMITS)
    assert b"payload" not in v.normalized


def test_unknown_reason_is_a_programming_error():
    with pytest.raises(ValueError):
        ImageRejected("nope")
