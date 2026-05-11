"""Unit tests for `apple_contacts_mcp.utils`."""

from __future__ import annotations

import pytest

from apple_contacts_mcp.utils import (
    detect_image_format,
    escape_applescript_string,
    label_to_apple_token,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", ""),
        ("plain text", "plain text"),
        ('with "quotes"', 'with \\"quotes\\"'),
        (r"with \backslash", r"with \\backslash"),
        (r"both \ and \"", r"both \\ and \\\""),
        ("multi\nline\ntext", "multi\nline\ntext"),
        ("café 🌮 unicode", "café 🌮 unicode"),
    ],
)
def test_escape_applescript_string_table(raw: str, expected: str) -> None:
    assert escape_applescript_string(raw) == expected


def test_backslash_escaped_before_quote() -> None:
    """The order matters: a literal backslash followed by a quote should escape
    to ``\\\\\\"``, not ``\\\\\\\\\"`` (which would happen if " were escaped
    first and the inserted backslash was then itself escaped)."""
    assert escape_applescript_string('\\"') == '\\\\\\"'


def test_round_trip_via_subprocess_safe_form() -> None:
    """Sanity check: the escaped form, wrapped in `"..."`, contains no
    unescaped `"` and the only backslashes that appear are escape sequences."""
    raw = 'He said "hi" with a \\ slash'
    escaped = escape_applescript_string(raw)
    quoted = f'"{escaped}"'
    # Strip the outer quotes; remaining " must all be preceded by a backslash.
    inner = quoted[1:-1]
    i = 0
    while i < len(inner):
        if inner[i] == '"':
            assert i > 0 and inner[i - 1] == "\\", (
                f"unescaped quote at {i} in {inner!r}"
            )
        i += 1


# ---------------------------------------------------------------------------
# label_to_apple_token
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("human_form", "expected_token"),
    [
        ("mobile", "_$!<Mobile>!$_"),
        ("work", "_$!<Work>!$_"),
        ("home", "_$!<Home>!$_"),
        ("other", "_$!<Other>!$_"),
        ("iphone", "_$!<iPhone>!$_"),
        ("main", "_$!<Main>!$_"),
        ("home fax", "_$!<HomeFAX>!$_"),
        ("work fax", "_$!<WorkFAX>!$_"),
        ("other fax", "_$!<OtherFAX>!$_"),
        ("pager", "_$!<Pager>!$_"),
        ("school", "_$!<School>!$_"),
        ("homepage", "_$!<HomePage>!$_"),
    ],
)
def test_label_translates_human_form_to_apple_token(
    human_form: str, expected_token: str
) -> None:
    assert label_to_apple_token(human_form) == expected_token


@pytest.mark.parametrize(
    ("variant", "expected_token"),
    [
        ("MOBILE", "_$!<Mobile>!$_"),
        ("Mobile", "_$!<Mobile>!$_"),
        ("  mobile  ", "_$!<Mobile>!$_"),
        ("HOME FAX", "_$!<HomeFAX>!$_"),
        ("Home Fax", "_$!<HomeFAX>!$_"),
        ("iPhone", "_$!<iPhone>!$_"),  # exact case
        ("IPHONE", "_$!<iPhone>!$_"),  # all-caps
    ],
)
def test_label_lookup_is_case_and_whitespace_insensitive(
    variant: str, expected_token: str
) -> None:
    assert label_to_apple_token(variant) == expected_token


@pytest.mark.parametrize(
    "token",
    [
        "_$!<Mobile>!$_",
        "_$!<Work>!$_",
        "_$!<HomePage>!$_",
        "_$!<HomeFAX>!$_",
    ],
)
def test_label_apple_token_passes_through_unchanged(token: str) -> None:
    """Already-token inputs round-trip — Apple stores them as built-ins."""
    assert label_to_apple_token(token) == token


@pytest.mark.parametrize(
    "custom",
    [
        "Spotify",
        "WhatsApp",
        "Personal",  # not a real Apple token despite looking like one elsewhere
        "_$!<Personal>!$_",  # fake token; Apple emits this verbatim
        "_$!<AppleWatch>!$_",  # fake token; not in the real catalog
        "my custom label",
    ],
)
def test_label_custom_strings_pass_through(custom: str) -> None:
    """Anything not in the human-form table is passed through unchanged.
    Apple's CN accepts custom labels; this preserves user intent."""
    assert label_to_apple_token(custom) == custom


def test_label_empty_string_passes_through() -> None:
    assert label_to_apple_token("") == ""


def test_label_whitespace_only_passes_through() -> None:
    """Whitespace-only is not a valid human form; treat as custom (don't crash)."""
    assert label_to_apple_token("   ") == "   "


# ---------------------------------------------------------------------------
# detect_image_format
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("magic", "expected"),
    [
        # JPEG: FF D8 FF followed by anything (JFIF, EXIF, etc. all share the prefix)
        (b"\xff\xd8\xff\xe0\x00\x10JFIF\x00", "jpeg"),
        (b"\xff\xd8\xff\xe1\x00\x10Exif\x00", "jpeg"),
        # PNG: full 8-byte signature
        (b"\x89PNG\r\n\x1a\n" + b"\x00" * 16, "png"),
        # GIF: 87a or 89a variants both start "GIF8"
        (b"GIF87a" + b"\x00" * 10, "gif"),
        (b"GIF89a" + b"\x00" * 10, "gif"),
    ],
)
def test_detect_image_format_known_formats(magic: bytes, expected: str) -> None:
    assert detect_image_format(magic) == expected


@pytest.mark.parametrize(
    "brand", [b"heic", b"heix", b"heif", b"hevc", b"hevx", b"mif1", b"msf1"]
)
def test_detect_image_format_heic_brands(brand: bytes) -> None:
    """Apple emits several HEIF-family ftyp brands; all should detect as 'heic'."""
    # 4-byte size prefix + 'ftyp' + brand + rest
    data = b"\x00\x00\x00\x18ftyp" + brand + b"\x00" * 32
    assert detect_image_format(data) == "heic"


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"\x00",
        b"\x00\x00",
        b"hello world",
        b"\xff\xd8",  # truncated JPEG header (1 byte short)
        b"\x89PN",  # truncated PNG header
        b"\x00\x00\x00\x18ftypwhat" + b"\x00" * 16,  # ftyp with unknown brand
        b"randombytesthatlooklikenothing",
    ],
)
def test_detect_image_format_unknown_or_short_input(data: bytes) -> None:
    assert detect_image_format(data) == "unknown"


def test_detect_image_format_does_not_raise_on_short_input() -> None:
    """The detector must be robust against any-length input.
    Every byte length from 0..12 should return 'unknown' without raising."""
    for n in range(13):
        # Use bytes that don't match any magic
        assert detect_image_format(b"\x01" * n) == "unknown"
