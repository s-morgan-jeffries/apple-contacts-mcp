"""Unit tests for `apple_contacts_mcp.utils`."""

from __future__ import annotations

import pytest

from apple_contacts_mcp.utils import (
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
