"""Unit tests for `apple_contacts_mcp.utils`."""

from __future__ import annotations

import pytest

from apple_contacts_mcp.utils import escape_applescript_string


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
