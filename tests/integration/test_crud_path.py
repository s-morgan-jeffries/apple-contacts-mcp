"""Integration tests for the CNSaveRequest write path.

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
# _run_cn_create_contact
# ---------------------------------------------------------------------------


def test_create_minimal_returns_identifier(
    real_connector: ContactsConnector, test_group: str
) -> None:
    """A bare-minimum contact returns a non-empty CN identifier."""
    identifier = real_connector._run_cn_create_contact(
        fields={"given_name": "MinimalCreate"},
        group_identifier=test_group,
    )
    try:
        assert isinstance(identifier, str)
        assert identifier
    finally:
        real_connector._run_cn_delete_contact(identifier)


def test_create_with_group_attaches_membership(
    real_connector: ContactsConnector, test_group: str
) -> None:
    """A contact created with group_identifier shows up in the group's members."""
    from Contacts import CNContact, CNContactIdentifierKey

    identifier = real_connector._run_cn_create_contact(
        fields={"given_name": "GroupedCreate"},
        group_identifier=test_group,
    )
    try:
        store = real_connector._get_store()
        pred = CNContact.predicateForContactsInGroupWithIdentifier_(test_group)
        members, _err = store.unifiedContactsMatchingPredicate_keysToFetch_error_(
            pred, [CNContactIdentifierKey], None
        )
        ids = {str(m.identifier()) for m in (members or [])}
        assert identifier in ids
    finally:
        real_connector._run_cn_delete_contact(identifier)


def test_create_with_full_p1_fields_round_trips(
    real_connector: ContactsConnector, test_group: str
) -> None:
    """Every P1 field type written via create reads back via fetch."""
    fields = {
        "given_name": "Full",
        "family_name": "Roundtrip",
        "middle_name": "M",
        "name_prefix": "Dr.",
        "name_suffix": "Jr.",
        "nickname": "Roundy",
        "organization": "Acme",
        "job_title": "Engineer",
        "department": "R&D",
        "phones": [
            {"label_raw": "_$!<Mobile>!$_", "value": "+1 555-1212"}
        ],
        "emails": [
            {"label_raw": "_$!<Home>!$_", "value": "round@example.com"}
        ],
        "urls": [{"label_raw": "_$!<HomePage>!$_", "value": "https://example.com"}],
        "postal_addresses": [
            {
                "label_raw": "_$!<Home>!$_",
                "street": "1 Loop",
                "city": "Cupertino",
                "state": "CA",
                "postal_code": "95014",
                "country": "USA",
                "iso_country_code": "us",
            }
        ],
        "birthday": {"year": 1990, "month": 5, "day": 15},
    }
    identifier = real_connector._run_cn_create_contact(
        fields=fields, group_identifier=test_group
    )
    try:
        result = real_connector._run_cn_unified_contact(identifier)
        assert result is not None
        assert result["given_name"] == "Full"
        assert result["family_name"] == "Roundtrip"
        assert result["organization"] == "Acme"
        assert result["job_title"] == "Engineer"
        assert len(result["phones"]) == 1
        assert result["phones"][0]["value"] == "+1 555-1212"
        assert len(result["emails"]) == 1
        assert result["emails"][0]["value"] == "round@example.com"
        assert len(result["urls"]) == 1
        assert len(result["postal_addresses"]) == 1
        assert result["postal_addresses"][0]["city"] == "Cupertino"
        assert result["birthday"] == {"year": 1990, "month": 5, "day": 15}
    finally:
        real_connector._run_cn_delete_contact(identifier)


# ---------------------------------------------------------------------------
# _run_cn_update_contact
# ---------------------------------------------------------------------------


def test_update_partial_only_changes_supplied_field(
    real_connector: ContactsConnector, tmp_contact: str
) -> None:
    """Updating just given_name leaves family_name intact."""
    real_connector._run_cn_update_contact(
        identifier=tmp_contact, fields={"given_name": "Updated"}
    )
    result = real_connector._run_cn_unified_contact(tmp_contact)
    assert result is not None
    assert result["given_name"] == "Updated"
    assert result["family_name"] == "Fixture"  # untouched


def test_update_with_empty_string_clears_field(
    real_connector: ContactsConnector, tmp_contact: str
) -> None:
    """Passing family_name='' explicitly clears it (presence semantics)."""
    real_connector._run_cn_update_contact(
        identifier=tmp_contact, fields={"family_name": ""}
    )
    result = real_connector._run_cn_unified_contact(tmp_contact)
    assert result is not None
    assert result["family_name"] == ""


def test_update_replaces_phones(
    real_connector: ContactsConnector, test_group: str
) -> None:
    """Updating with a new phones list replaces (not appends)."""
    identifier = real_connector._run_cn_create_contact(
        fields={
            "given_name": "PhoneReplace",
            "phones": [{"label_raw": "_$!<Home>!$_", "value": "+1 555-1111"}],
        },
        group_identifier=test_group,
    )
    try:
        real_connector._run_cn_update_contact(
            identifier=identifier,
            fields={
                "phones": [
                    {"label_raw": "_$!<Mobile>!$_", "value": "+1 555-2222"}
                ]
            },
        )
        result = real_connector._run_cn_unified_contact(identifier)
        assert result is not None
        assert len(result["phones"]) == 1
        assert result["phones"][0]["value"] == "+1 555-2222"
    finally:
        real_connector._run_cn_delete_contact(identifier)


def test_update_raises_not_found_for_missing_identifier(
    real_connector: ContactsConnector,
) -> None:
    fabricated = f"fabricated-{uuid.uuid4()}"
    with pytest.raises(ContactsNotFoundError):
        real_connector._run_cn_update_contact(
            identifier=fabricated, fields={"given_name": "X"}
        )


# ---------------------------------------------------------------------------
# _run_cn_delete_contact
# ---------------------------------------------------------------------------


def test_delete_removes_contact(
    real_connector: ContactsConnector, test_group: str
) -> None:
    """After delete, the contact is gone from the store."""
    identifier = real_connector._run_cn_create_contact(
        fields={"given_name": "DeleteMe"},
        group_identifier=test_group,
    )
    real_connector._run_cn_delete_contact(identifier)
    assert real_connector._run_cn_unified_contact(identifier) is None


def test_delete_raises_not_found_for_missing_identifier(
    real_connector: ContactsConnector,
) -> None:
    fabricated = f"fabricated-{uuid.uuid4()}"
    with pytest.raises(ContactsNotFoundError):
        real_connector._run_cn_delete_contact(fabricated)


# ---------------------------------------------------------------------------
# Full CRUD smoke
# ---------------------------------------------------------------------------


def test_full_crud_cycle(
    real_connector: ContactsConnector, test_group: str
) -> None:
    """create → fetch → update → fetch → delete → fetch returns None."""
    identifier = real_connector._run_cn_create_contact(
        fields={"given_name": "Crud", "family_name": "Cycle"},
        group_identifier=test_group,
    )
    try:
        # Fetch after create.
        first = real_connector._run_cn_unified_contact(identifier)
        assert first is not None
        assert first["given_name"] == "Crud"

        # Update.
        real_connector._run_cn_update_contact(
            identifier=identifier,
            fields={
                "family_name": "Updated",
                "organization": "AcmeCRUD",
                "phones": [
                    {"label_raw": "_$!<Mobile>!$_", "value": "+1 555-9999"}
                ],
            },
        )

        # Fetch after update.
        second = real_connector._run_cn_unified_contact(identifier)
        assert second is not None
        assert second["given_name"] == "Crud"  # not touched
        assert second["family_name"] == "Updated"
        assert second["organization"] == "AcmeCRUD"
        assert second["phones"][0]["value"] == "+1 555-9999"

        # Delete.
        real_connector._run_cn_delete_contact(identifier)
    except Exception:
        # If something went wrong mid-cycle, attempt cleanup so the next run
        # doesn't trip over a leaked contact (best-effort).
        try:
            real_connector._run_cn_delete_contact(identifier)
        except Exception:
            pass
        raise

    # Fetch after delete.
    assert real_connector._run_cn_unified_contact(identifier) is None
