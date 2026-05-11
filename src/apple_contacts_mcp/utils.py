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


# Apple's built-in label tokens, mapped from the lowercase human form
# that ``CNLabeledValue.localizedStringForLabel:`` returns in en_US.
# Empirically probed against macOS 26.3.1 — see issue #22 and
# ``docs/research/label-translation-decision.md``.
_HUMAN_LABEL_TO_APPLE_TOKEN: dict[str, str] = {
    "mobile": "_$!<Mobile>!$_",
    "work": "_$!<Work>!$_",
    "home": "_$!<Home>!$_",
    "other": "_$!<Other>!$_",
    "iphone": "_$!<iPhone>!$_",
    "main": "_$!<Main>!$_",
    "home fax": "_$!<HomeFAX>!$_",
    "work fax": "_$!<WorkFAX>!$_",
    "other fax": "_$!<OtherFAX>!$_",
    "pager": "_$!<Pager>!$_",
    "school": "_$!<School>!$_",
    "homepage": "_$!<HomePage>!$_",
}


_HEIC_FTYP_BRANDS: frozenset[bytes] = frozenset(
    {b"heic", b"heix", b"heif", b"hevc", b"hevx", b"mif1", b"msf1"}
)


def detect_image_format(data: bytes) -> str:
    """Identify an image format from its leading magic bytes.

    Returns one of ``"jpeg"``, ``"png"``, ``"gif"``, ``"heic"``, or
    ``"unknown"``. Pure function; no PyObjC dependency. Robust against
    short / empty input — never raises.

    ``"heic"`` covers the wider HEIF-family ISOBMFF brands Apple emits
    (heic, heix, heif, hevc, hevx, mif1, msf1) — they're all "HEIF-flavored
    bytes" from a caller's perspective.
    """
    if len(data) >= 3 and data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if len(data) >= 8 and data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if len(data) >= 4 and data[:4] == b"GIF8":
        return "gif"
    if len(data) >= 12 and data[4:8] == b"ftyp" and data[8:12] in _HEIC_FTYP_BRANDS:
        return "heic"
    return "unknown"


def label_to_apple_token(label: str) -> str:
    """Translate a label input to the form Contacts.framework expects.

    Three cases, all returning the right thing for
    ``CNLabeledValue.labeledValueWithLabel:value:``:

    - **Human form** (case-insensitive: ``"mobile"``, ``"Home Fax"``,
      ``"iPhone"``): translated to Apple's raw token (e.g.,
      ``_$!<Mobile>!$_``).
    - **Apple token** (``_$!<Mobile>!$_``): not in the table; passed
      through unchanged. Apple stores it as the built-in label.
    - **Custom string** (``"Spotify"``, ``"Personal"``): not in the
      table, not a built-in token; passed through unchanged. Apple
      stores it as a custom label.

    Lookup is case-insensitive on the human form (``"MOBILE"`` →
    ``_$!<Mobile>!$_``); the input is also stripped of leading/trailing
    whitespace before lookup. The empty string returns ``""`` (no label).

    Note: Apple's ``localizedStringForLabel:`` returns locale-dependent
    strings (``"mobile"`` in en, ``"mobil"`` in de). This helper accepts
    only English forms. Non-English human forms are treated as custom
    labels — predictable, but worth documenting in tool docstrings.
    """
    if not label:
        return label
    return _HUMAN_LABEL_TO_APPLE_TOKEN.get(label.strip().lower(), label)
