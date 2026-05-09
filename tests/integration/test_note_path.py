"""Integration tests for the AppleScript note read/write path.

These tests are **mandatory** for any AppleScript-touching code per the
``integration-testing`` skill's hard rule. They exercise:

- The identifier-format invariant (CN id must be passed verbatim, not
  stripped of the ``:ABPerson`` suffix — discovered empirically during #19).
- The escape helper against real osascript with quotes / backslashes /
  newlines / Unicode.
- The ``save`` command's persistence to disk.
- The not-found mapping for fabricated identifiers.

Skipped by default; opt in with ``--run-integration``.
"""

from __future__ import annotations

import uuid

import pytest

from apple_contacts_mcp.contacts_connector import ContactsConnector
from apple_contacts_mcp.exceptions import ContactsNotFoundError

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        "not config.getoption('--run-integration')",
        reason="Integration tests opt-in via --run-integration",
    ),
]


# ---------------------------------------------------------------------------
# Identifier-format probe
# ---------------------------------------------------------------------------


def test_applescript_id_matches_cn_identifier_with_abperson_suffix(
    real_connector: ContactsConnector, tmp_contact: str
) -> None:
    """Locks in the empirical finding: AppleScript's `id of person` returns
    the full `<UUID>:ABPerson` form, identical to CN's `unifiedContact.id`.
    Stripping the suffix breaks `whose id is "..."` lookups.

    If this test fails on a future macOS, the not-found mapping in
    `_is_not_found_error` and the docstring claims need updating.
    """
    script = (
        'tell application "Contacts"\n'
        f'  set p to first person whose id is "{tmp_contact}"\n'
        "  return id of p\n"
        "end tell"
    )
    result = real_connector._run_applescript(script)
    assert result == tmp_contact
    assert result.endswith(":ABPerson")


# ---------------------------------------------------------------------------
# read_note + write_note round-trip
# ---------------------------------------------------------------------------


def test_fresh_contact_has_empty_note(
    real_connector: ContactsConnector, tmp_contact: str
) -> None:
    assert real_connector._run_applescript_read_note(tmp_contact) == ""


def test_read_write_read_round_trip(
    real_connector: ContactsConnector, tmp_contact: str
) -> None:
    """Write a note, read it back; overwrite, read again."""
    real_connector._run_applescript_write_note(tmp_contact, "hello")
    assert real_connector._run_applescript_read_note(tmp_contact) == "hello"

    real_connector._run_applescript_write_note(tmp_contact, "replaced")
    assert real_connector._run_applescript_read_note(tmp_contact) == "replaced"


def test_empty_string_clears_existing_note(
    real_connector: ContactsConnector, tmp_contact: str
) -> None:
    """write_note(id, '') is the documented way to clear a note."""
    real_connector._run_applescript_write_note(tmp_contact, "non-empty")
    assert real_connector._run_applescript_read_note(tmp_contact) == "non-empty"
    real_connector._run_applescript_write_note(tmp_contact, "")
    assert real_connector._run_applescript_read_note(tmp_contact) == ""


def test_unicode_quotes_and_backslashes_round_trip(
    real_connector: ContactsConnector, tmp_contact: str
) -> None:
    """The load-bearing test for `escape_applescript_string`. If escaping is
    wrong, these characters either break the script (osascript syntax error)
    or come back mangled."""
    payload = (
        'Line 1\n'
        'Line 2 — café 🌮 with "quotes"\n'
        'and \\backslash and a "literal \\\\ pair"'
    )
    real_connector._run_applescript_write_note(tmp_contact, payload)
    assert real_connector._run_applescript_read_note(tmp_contact) == payload


def test_not_found_for_fabricated_identifier(
    real_connector: ContactsConnector,
) -> None:
    fabricated = f"{uuid.uuid4()}:ABPerson"
    with pytest.raises(ContactsNotFoundError):
        real_connector._run_applescript_read_note(fabricated)
    with pytest.raises(ContactsNotFoundError):
        real_connector._run_applescript_write_note(fabricated, "x")


# ---------------------------------------------------------------------------
# Persistence probe
# ---------------------------------------------------------------------------


def test_save_command_persists_across_subprocesses(
    real_connector: ContactsConnector, tmp_contact: str
) -> None:
    """The `save` command in `_run_applescript_write_note` is load-bearing —
    without it, edits sit in Contacts.app's in-memory state and don't persist.
    Verify by writing in one osascript invocation and reading in a fresh one.
    """
    real_connector._run_applescript_write_note(tmp_contact, "persistent value")
    # Fresh osascript subprocess — would not see in-memory-only edits if `save`
    # were missing.
    raw_script = (
        'tell application "Contacts"\n'
        f'  set p to first person whose id is "{tmp_contact}"\n'
        "  return note of p\n"
        "end tell"
    )
    assert real_connector._run_applescript(raw_script) == "persistent value"
