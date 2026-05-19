"""Durability regression test for the undocumented `CNContact` runtime
selectors `creationDate` and `modificationDate` (issue #33).

These fields are accessible only via runtime selectors — they are not in
the public `CNContactKey` constants. The skill docs (SKILL.md §2) and
gap analysis Q2 flag them as a v0.4.0 follow-up that needs a check.

If either test fails on a macOS upgrade, the AppleScript fallback
(`creation date of person`, `modification date of person`) needs to be
wired before downstream features that depend on these dates ship.
"""

from __future__ import annotations

import datetime

import pytest

from apple_contacts_mcp.contacts_connector import ContactsConnector

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        "not config.getoption('--run-integration')",
        reason="Integration tests opt-in via --run-integration",
    ),
]


def _fetch_raw_cn_contact(connector: ContactsConnector, identifier: str):  # type: ignore[no-untyped-def]
    """Fetch a contact directly from the store with the undocumented
    date selectors in keysToFetch. CN accepts NSStrings as key paths."""
    store = connector._get_store()
    contact, err = store.unifiedContactWithIdentifier_keysToFetch_error_(
        identifier, ["creationDate", "modificationDate"], None
    )
    assert err is None, f"CN fetch failed: {err}"
    assert contact is not None, f"contact not found: {identifier!r}"
    return contact


def test_creationDate_selector_returns_datetime(
    real_connector: ContactsConnector, tmp_contact: str
) -> None:
    """`creationDate` selector is bridged and returns a usable NSDate.

    If this fails: macOS framework regression — wire the AppleScript
    fallback (`creation date of person`) per SKILL.md §2 / gap-analysis Q2.
    """
    cn_contact = _fetch_raw_cn_contact(real_connector, tmp_contact)
    try:
        raw = cn_contact.creationDate()
    except AttributeError as exc:
        pytest.fail(
            f"macOS regression: `creationDate` selector no longer "
            f"accessible on CNContact ({exc}). Wire the AppleScript "
            f"fallback (`creation date of person`) per SKILL.md §2 / "
            f"gap-analysis Q2."
        )
    assert raw is not None, (
        "creationDate selector returned None on a freshly-created "
        "contact — the field is silently dropped or never populated. "
        "Investigate before assuming the framework is healthy."
    )
    assert isinstance(raw, datetime.datetime), (
        f"creationDate returned unexpected type {type(raw)!r}; "
        f"expected datetime (PyObjC NSDate bridge)."
    )


def test_modificationDate_selector_returns_datetime(
    real_connector: ContactsConnector, tmp_contact: str
) -> None:
    """`modificationDate` selector is bridged and returns a usable NSDate.

    If this fails: macOS framework regression — wire the AppleScript
    fallback (`modification date of person`) per SKILL.md §2.
    """
    cn_contact = _fetch_raw_cn_contact(real_connector, tmp_contact)
    try:
        raw = cn_contact.modificationDate()
    except AttributeError as exc:
        pytest.fail(
            f"macOS regression: `modificationDate` selector no longer "
            f"accessible on CNContact ({exc}). Wire the AppleScript "
            f"fallback (`modification date of person`) per SKILL.md §2."
        )
    assert raw is not None, (
        "modificationDate selector returned None — investigate."
    )
    assert isinstance(raw, datetime.datetime), (
        f"modificationDate returned unexpected type {type(raw)!r}; "
        f"expected datetime."
    )
