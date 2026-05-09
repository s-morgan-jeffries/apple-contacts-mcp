"""Shared utilities for Apple Contacts MCP."""

from __future__ import annotations


def escape_applescript_string(s: str) -> str:
    """Escape ``s`` for safe interpolation inside an AppleScript ``"..."`` literal.

    Backslash-first ordering matters — escaping ``"`` before ``\\`` would
    double-escape the inserted backslashes. Embedded newlines pass through
    unchanged (AppleScript string literals accept them). Caller is responsible
    for placing the result inside double-quoted (not single-quoted) AppleScript
    string literals.
    """
    return s.replace("\\", "\\\\").replace('"', '\\"')
