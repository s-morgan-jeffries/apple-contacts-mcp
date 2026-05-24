"""Lock the shape of packaging/Info.plist (issue #34).

The plist is a contract artifact for future bundling — not consumed by the
current unbundled `uv run` launch path. These assertions catch silent drift
in the keys that bundling tools and the macOS TCC consent dialog need.
"""

from __future__ import annotations

import plistlib
import re
from pathlib import Path

import pytest

from apple_contacts_mcp import __version__

PLIST_PATH = Path(__file__).resolve().parents[2] / "packaging" / "Info.plist"


@pytest.fixture(scope="module")
def plist() -> dict:
    with PLIST_PATH.open("rb") as fp:
        return plistlib.load(fp)


def test_plist_file_exists() -> None:
    assert PLIST_PATH.is_file(), f"Info.plist missing at {PLIST_PATH}"


def test_contacts_usage_description_present_and_nonempty(plist: dict) -> None:
    """The whole point of issue #34: the TCC consent-dialog copy."""
    desc = plist.get("NSContactsUsageDescription")
    assert isinstance(desc, str) and desc.strip(), (
        "NSContactsUsageDescription must be a non-empty string"
    )


def test_bundle_identifier_is_reverse_dns(plist: dict) -> None:
    bid = plist.get("CFBundleIdentifier")
    assert isinstance(bid, str)
    assert re.match(r"^com\.[a-z0-9-]+\.[a-z0-9-]+$", bid), (
        f"CFBundleIdentifier should be reverse-DNS-shaped, got: {bid!r}"
    )


def test_runs_as_background_process(plist: dict) -> None:
    """LSUIElement=true means no Dock icon / menu bar — MCP server is headless."""
    assert plist.get("LSUIElement") is True


def test_minimum_system_version_present(plist: dict) -> None:
    min_ver = plist.get("LSMinimumSystemVersion")
    assert isinstance(min_ver, str) and re.match(r"^\d+\.\d+", min_ver), (
        f"LSMinimumSystemVersion must be a version string, got: {min_ver!r}"
    )


def test_version_strings_match_package_version(plist: dict) -> None:
    """Cross-check against `apple_contacts_mcp.__version__` — the same source
    `scripts/check_version_sync.sh` treats as authoritative via pyproject.toml.
    """
    assert plist.get("CFBundleShortVersionString") == __version__
    assert plist.get("CFBundleVersion") == __version__


def test_executable_matches_console_script(plist: dict) -> None:
    """Must match the `[project.scripts]` entry in pyproject.toml."""
    assert plist.get("CFBundleExecutable") == "apple-contacts-mcp"
