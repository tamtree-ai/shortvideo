"""Image validation — every check that saves a run before MiniMax makes it.

The dimension readers are tested against headers built to a known size rather
than against checked-in files, so an offset read one byte wrong fails here
instead of passing against the one sample that happened to be in the repo.
"""

from __future__ import annotations

import base64

import pytest
from tamtree_plugin_sdk import NodeConfigurationError

from tamtree_shortvideo.images import (
    MAX_IMAGE_BYTES,
    SUPPORTED_FORMATS,
    data_uri,
    dimensions,
    image_format,
    validate_image,
)
from tests.image_fixtures import (
    heic_bytes,
    jpeg_bytes,
    png_bytes,
    webp_vp8_bytes,
    webp_vp8l_bytes,
    webp_vp8x_bytes,
)

# -- format detection --------------------------------------------------------


@pytest.mark.parametrize(
    ("builder", "expected"),
    [
        (png_bytes, "png"),
        (jpeg_bytes, "jpeg"),
        (webp_vp8x_bytes, "webp"),
        (webp_vp8l_bytes, "webp"),
        (webp_vp8_bytes, "webp"),
        (heic_bytes, "heic"),
    ],
)
def test_the_format_is_read_from_the_bytes(builder: object, expected: str) -> None:
    assert image_format(builder()) == expected  # type: ignore[operator]


def test_the_generic_heif_brands_are_recognised_as_heif() -> None:
    assert image_format(heic_bytes(brand=b"mif1")) == "heif"
    assert image_format(heic_bytes(brand=b"msf1")) == "heif"


def test_something_that_is_not_an_image_is_not_guessed_at() -> None:
    assert image_format(b"%PDF-1.7\n") is None
    assert image_format(b"") is None


def test_every_detected_format_has_a_media_type() -> None:
    """The detector and the data-URI table are the same vocabulary; a format
    the first could return and the second could not name would be a KeyError
    on a real user's image."""
    for builder in (png_bytes, jpeg_bytes, webp_vp8x_bytes, heic_bytes):
        fmt = image_format(builder())
        assert fmt in SUPPORTED_FORMATS


# -- dimensions --------------------------------------------------------------


@pytest.mark.parametrize(
    "builder", [png_bytes, jpeg_bytes, webp_vp8x_bytes, webp_vp8l_bytes, webp_vp8_bytes]
)
@pytest.mark.parametrize(("width", "height"), [(1080, 1920), (256, 256), (5760, 2304)])
def test_dimensions_are_read_exactly(builder: object, width: int, height: int) -> None:
    data = builder(width=width, height=height)  # type: ignore[operator]
    fmt = image_format(data)
    assert fmt is not None

    assert dimensions(data, fmt) == (width, height)


def test_a_jpeg_size_is_found_behind_however_many_segments_precede_it() -> None:
    """A real photo carries EXIF and quantisation tables before its SOF. A
    parser reading a fixed offset would pass on a minimal file and fail on
    every camera JPEG."""
    for padding in (0, 1, 5, 12):
        data = jpeg_bytes(width=720, height=1280, padding_segments=padding)
        assert dimensions(data, "jpeg") == (720, 1280), padding


def test_heic_dimensions_are_not_read_and_that_is_deliberate() -> None:
    """Walking ISO-BMFF boxes is a real parser for a format nobody feeds a
    vertical short from. It passes format and size checks; MiniMax judges the
    rest, which costs a run but never a generation."""
    assert dimensions(heic_bytes(), "heic") is None


def test_a_truncated_header_returns_no_size_rather_than_a_wrong_one() -> None:
    assert dimensions(png_bytes()[:20], "png") is None
    assert dimensions(b"\xff\xd8\xff", "jpeg") is None
    assert dimensions(b"RIFF\x00\x00\x00\x00WEBP", "webp") is None


# -- validation --------------------------------------------------------------


def test_a_good_frame_passes_and_names_its_format() -> None:
    assert validate_image(png_bytes(width=1080, height=1920), label="first frame") == "png"


def test_an_empty_attachment_is_refused() -> None:
    with pytest.raises(NodeConfigurationError, match="is empty"):
        validate_image(b"", label="first frame")


def test_a_file_that_is_not_an_image_says_the_type_was_read_from_the_bytes() -> None:
    """The common case is a `.png` that is really something else, and the
    message has to explain why the declared type did not save it."""
    with pytest.raises(NodeConfigurationError) as caught:
        validate_image(b"%PDF-1.7\n" + b"\x00" * 100, label="reference image 2")

    message = str(caught.value)
    assert "reference image 2" in message
    assert "read from the bytes" in message


def test_an_oversized_file_is_refused_with_the_escape_hatch_named() -> None:
    oversized = png_bytes() + b"\x00" * MAX_IMAGE_BYTES

    with pytest.raises(NodeConfigurationError) as caught:
        validate_image(oversized, label="first frame")

    assert "https URL" in str(caught.value)


@pytest.mark.parametrize(("width", "height"), [(255, 500), (500, 255), (5761, 3000), (3000, 5761)])
def test_dimensions_outside_the_accepted_range_are_refused(width: int, height: int) -> None:
    with pytest.raises(NodeConfigurationError, match="pixels on each side"):
        validate_image(png_bytes(width=width, height=height), label="first frame")


@pytest.mark.parametrize(("width", "height"), [(300, 1000), (1000, 300)])
def test_an_extreme_aspect_ratio_is_refused(width: int, height: int) -> None:
    with pytest.raises(NodeConfigurationError, match="aspect ratio"):
        validate_image(png_bytes(width=width, height=height), label="first frame")


def test_a_vertical_short_frame_is_inside_the_ratio_limits() -> None:
    """0.56 for 9:16 — the shape this whole plugin exists to produce had
    better not be refused by its own validation."""
    assert validate_image(png_bytes(width=1080, height=1920), label="first frame") == "png"


def test_heic_passes_without_a_dimension_check() -> None:
    assert validate_image(heic_bytes(), label="first frame") == "heic"


# -- the data URI ------------------------------------------------------------


def test_the_data_uri_carries_a_lowercase_media_type_and_the_real_bytes() -> None:
    """MiniMax requires the format in lower case, and the payload has to
    survive the round trip — a truncated or re-encoded image is a rejection
    that looks like a bad picture."""
    data = png_bytes(width=512, height=512)

    uri = data_uri(data, "png")

    prefix, encoded = uri.split(",", 1)
    assert prefix == "data:image/png;base64"
    assert base64.b64decode(encoded) == data


def test_jpg_and_jpeg_are_the_one_media_type() -> None:
    """MiniMax lists both spellings as accepted formats; `image/jpeg` is the
    only spelling of the media type."""
    assert SUPPORTED_FORMATS["jpeg"] == "image/jpeg"
    assert "jpg" not in SUPPORTED_FORMATS
