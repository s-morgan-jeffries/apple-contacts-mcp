"""Integration tests for `_run_cn_export_vcard` and `_run_cn_import_vcard`.

These exercise `CNContactVCardSerialization` against a real Contacts.app:
- Round-trip an exported vCard back into a new contact and verify fields.
- Year-full + year-less BDAY parsing using the empirically-captured
  fixtures from `docs/research/vcard-version-decision.md` Appendix A.
- Group-on-import.
- Malformed-input rejection (parse failure surfaces as `ContactsError`).
- Multi-contact vCard.

Skipped by default; opt in with ``--run-integration``.
"""

from __future__ import annotations

import logging
import uuid

import pytest

from apple_contacts_mcp.contacts_connector import ContactsConnector
from apple_contacts_mcp.exceptions import ContactsError, ContactsNotFoundError

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        "not config.getoption('--run-integration')",
        reason="Integration tests opt-in via --run-integration",
    ),
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Round-trip: export → import → fetch → verify
# ---------------------------------------------------------------------------


def test_export_then_import_round_trip(
    real_connector: ContactsConnector,
    test_group: str,
    tmp_contact: str,
) -> None:
    """Export the tmp_contact, import the bytes back, fetch the new contact,
    and verify the basic fields round-tripped."""
    # Add a phone + email to the tmp_contact via update so we have something
    # interesting to round-trip.
    real_connector._run_cn_update_contact(
        identifier=tmp_contact,
        fields={
            "phones": [
                {
                    "label": "_$!<Mobile>!$_",
                    "value": "+15551234567",
                }
            ],
            "emails": [
                {
                    "label": "_$!<Work>!$_",
                    "value": "round-trip@example.com",
                }
            ],
        },
    )

    vcard_text = real_connector._run_cn_export_vcard([tmp_contact])
    assert "BEGIN:VCARD" in vcard_text
    assert "VERSION:3.0" in vcard_text
    assert "+15551234567" in vcard_text
    assert "round-trip@example.com" in vcard_text

    new_ids = real_connector._run_cn_import_vcard(
        vcard_text, group_identifier=test_group
    )
    assert len(new_ids) == 1
    new_id = new_ids[0]
    try:
        new_contact = real_connector._run_cn_unified_contact(new_id)
        assert new_contact is not None
        assert new_contact["given_name"] == "Integration"
        assert new_contact["family_name"] == "Fixture"
        assert any(
            p["value"] == "+15551234567" for p in new_contact["phones"]
        )
        assert any(
            e["value"] == "round-trip@example.com"
            for e in new_contact["emails"]
        )
    finally:
        real_connector._run_cn_delete_contact(new_id)


# ---------------------------------------------------------------------------
# Year-full + year-less BDAY using the #23 Appendix A fixtures
# ---------------------------------------------------------------------------


_YEAR_FULL_VCARD = (
    "BEGIN:VCARD\r\n"
    "VERSION:3.0\r\n"
    "PRODID:-//Apple Inc.//macOS 26.3.1//EN\r\n"
    "N:Probe;YearFull;;;\r\n"
    "FN:YearFull Probe\r\n"
    "TEL;type=CELL;type=VOICE;type=pref:+15550101111\r\n"
    "BDAY:1980-05-15\r\n"
    "END:VCARD\r\n"
)


_YEAR_LESS_VCARD = (
    "BEGIN:VCARD\r\n"
    "VERSION:3.0\r\n"
    "PRODID:-//Apple Inc.//macOS 26.3.1//EN\r\n"
    "N:Probe;YearLess;;;\r\n"
    "FN:YearLess Probe\r\n"
    "TEL;type=CELL;type=VOICE;type=pref:+15550101222\r\n"
    "BDAY;X-APPLE-OMIT-YEAR=1604:1604-05-15\r\n"
    "END:VCARD\r\n"
)


def test_year_full_birthday_round_trips(
    real_connector: ContactsConnector,
    test_group: str,
) -> None:
    new_ids = real_connector._run_cn_import_vcard(
        _YEAR_FULL_VCARD, group_identifier=test_group
    )
    assert len(new_ids) == 1
    try:
        c = real_connector._run_cn_unified_contact(new_ids[0])
        assert c is not None
        assert c["birthday"] == {"year": 1980, "month": 5, "day": 15}
    finally:
        real_connector._run_cn_delete_contact(new_ids[0])


def test_year_less_birthday_round_trips_via_apple_omit_year(
    real_connector: ContactsConnector,
    test_group: str,
) -> None:
    """Apple recognizes its own X-APPLE-OMIT-YEAR=1604 marker on import and
    strips the placeholder year. The resulting contact's birthday should
    expose only month/day.
    """
    new_ids = real_connector._run_cn_import_vcard(
        _YEAR_LESS_VCARD, group_identifier=test_group
    )
    assert len(new_ids) == 1
    try:
        c = real_connector._run_cn_unified_contact(new_ids[0])
        assert c is not None
        assert c["birthday"] == {"month": 5, "day": 15}, (
            f"year-less BDAY should round-trip to {{month,day}} via "
            f"Apple's X-APPLE-OMIT-YEAR recognition, got: {c['birthday']!r}"
        )
    finally:
        real_connector._run_cn_delete_contact(new_ids[0])


# ---------------------------------------------------------------------------
# Group-on-import + multi-contact + error paths
# ---------------------------------------------------------------------------


def test_import_with_group_adds_membership(
    real_connector: ContactsConnector,
    test_group: str,
) -> None:
    vcard = (
        "BEGIN:VCARD\r\n"
        "VERSION:3.0\r\n"
        f"N:Member;Group{uuid.uuid4().hex[:6]};;;\r\n"
        "FN:Group Member\r\n"
        "END:VCARD\r\n"
    )
    new_ids = real_connector._run_cn_import_vcard(
        vcard, group_identifier=test_group
    )
    assert len(new_ids) == 1
    try:
        members = real_connector._run_cn_contacts_in_group(
            test_group, limit=200
        )
        assert new_ids[0] in {m["id"] for m in members}
    finally:
        real_connector._run_cn_delete_contact(new_ids[0])


def test_import_multi_contact_returns_two_identifiers(
    real_connector: ContactsConnector,
    test_group: str,
) -> None:
    vcard = (
        "BEGIN:VCARD\r\n"
        "VERSION:3.0\r\n"
        f"N:Multi;A{uuid.uuid4().hex[:6]};;;\r\n"
        "FN:Multi A\r\n"
        "END:VCARD\r\n"
        "BEGIN:VCARD\r\n"
        "VERSION:3.0\r\n"
        f"N:Multi;B{uuid.uuid4().hex[:6]};;;\r\n"
        "FN:Multi B\r\n"
        "END:VCARD\r\n"
    )
    new_ids = real_connector._run_cn_import_vcard(
        vcard, group_identifier=test_group
    )
    assert len(new_ids) == 2
    try:
        for nid in new_ids:
            c = real_connector._run_cn_unified_contact(nid)
            assert c is not None
            # vCard 3.0 N: format is `Family;Given;...` — fixture puts
            # "Multi" in family-name and "A<hex>"/"B<hex>" in given-name.
            assert c["family_name"] == "Multi"
    finally:
        for nid in new_ids:
            try:
                real_connector._run_cn_delete_contact(nid)
            except Exception as exc:  # pragma: no cover (cleanup best-effort)
                logger.warning("multi-contact cleanup failed for %s: %s", nid, exc)


def test_import_malformed_input_raises_contacts_error(
    real_connector: ContactsConnector,
) -> None:
    """Apple's parser rejects garbage input. The connector translates to
    ContactsError with the 'vCard parse failed' prefix (the server tool
    layer maps that prefix to validation_error)."""
    with pytest.raises(ContactsError) as exc_info:
        real_connector._run_cn_import_vcard(
            "this is not a vcard at all",
            group_identifier=None,
        )
    assert (
        "vCard parse failed" in str(exc_info.value)
        or "No vCards found" in str(exc_info.value)
    )


def test_import_with_unknown_group_raises_not_found(
    real_connector: ContactsConnector,
) -> None:
    fabricated = f"{uuid.uuid4()}:ABGroup"
    with pytest.raises(ContactsNotFoundError) as exc_info:
        real_connector._run_cn_import_vcard(
            "BEGIN:VCARD\r\nVERSION:3.0\r\nFN:X\r\nEND:VCARD\r\n",
            group_identifier=fabricated,
        )
    assert "Group not found" in str(exc_info.value)


def test_export_unknown_identifier_raises_not_found(
    real_connector: ContactsConnector,
) -> None:
    fabricated = f"{uuid.uuid4()}:ABPerson"
    with pytest.raises(ContactsNotFoundError) as exc_info:
        real_connector._run_cn_export_vcard([fabricated])
    assert fabricated in str(exc_info.value)
