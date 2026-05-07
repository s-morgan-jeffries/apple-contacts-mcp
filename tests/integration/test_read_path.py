"""Integration tests for read-path connector helpers.

Skipped by default; opt in with ``--run-integration``.
"""

from __future__ import annotations

import uuid

import pytest

from apple_contacts_mcp.contacts_connector import ContactsConnector

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        "not config.getoption('--run-integration')",
        reason="Integration tests opt-in via --run-integration",
    ),
]


# ---------------------------------------------------------------------------
# _run_cn_enumerate_contacts
# ---------------------------------------------------------------------------


def test_enumerate_returns_dicts_with_expected_shape(
    real_connector: ContactsConnector, test_group: str
) -> None:
    """The first page is dicts with the documented 4 keys, all str values."""
    results = real_connector._run_cn_enumerate_contacts(offset=0, limit=10)
    assert isinstance(results, list)
    for entry in results:
        assert set(entry.keys()) == {
            "id",
            "given_name",
            "family_name",
            "organization",
        }
        for v in entry.values():
            assert isinstance(v, str)


def test_enumerate_offset_advances(
    real_connector: ContactsConnector, test_group: str
) -> None:
    """Calling enumerate with offset=N+M returns a different first entry
    than offset=0 (assuming the store has at least M+1 contacts).

    Skipped automatically if the test machine has fewer than 2 contacts.
    """
    first_page = real_connector._run_cn_enumerate_contacts(offset=0, limit=2)
    if len(first_page) < 2:
        pytest.skip("Need at least 2 contacts in the store to test offset advance.")
    offset_page = real_connector._run_cn_enumerate_contacts(offset=1, limit=1)
    assert offset_page[0]["id"] == first_page[1]["id"]


# ---------------------------------------------------------------------------
# _run_cn_unified_contact
# ---------------------------------------------------------------------------


def test_unified_contact_round_trips_basic_fields(
    real_connector: ContactsConnector, tmp_contact: str
) -> None:
    """A contact created with given_name/family_name fetches back with those
    exact values plus empty defaults for unset fields."""
    result = real_connector._run_cn_unified_contact(tmp_contact)
    assert result is not None
    assert result["id"] == tmp_contact
    assert result["given_name"] == "Integration"
    assert result["family_name"] == "Fixture"
    assert result["organization"] == ""
    assert result["phones"] == []
    assert result["emails"] == []
    assert result["postal_addresses"] == []
    assert result["birthday"] is None


def test_unified_contact_returns_none_for_missing_id(
    real_connector: ContactsConnector,
) -> None:
    """A fabricated identifier returns None (not raise)."""
    fabricated = f"fabricated-{uuid.uuid4()}"
    assert real_connector._run_cn_unified_contact(fabricated) is None


# ---------------------------------------------------------------------------
# _run_cn_search_contacts
# ---------------------------------------------------------------------------


def test_search_finds_contact_by_unique_name(
    real_connector: ContactsConnector, test_group: str
) -> None:
    """A contact created with a unique name is findable by predicate."""
    unique_token = f"IntegSearch{uuid.uuid4().hex[:8]}"
    identifier = real_connector._run_cn_create_contact(
        fields={"given_name": unique_token, "family_name": "Searchable"},
        group_identifier=test_group,
    )
    try:
        results = real_connector._run_cn_search_contacts(
            field="name", value=unique_token, limit=10
        )
        ids = {entry["id"] for entry in results}
        assert identifier in ids
    finally:
        real_connector._run_cn_delete_contact(identifier)


def test_search_no_match_returns_empty(
    real_connector: ContactsConnector,
) -> None:
    """A query guaranteed to miss returns an empty list."""
    impossible = f"NoSuchPerson-{uuid.uuid4().hex}"
    assert (
        real_connector._run_cn_search_contacts(
            field="name", value=impossible, limit=10
        )
        == []
    )


def test_search_finds_contact_by_unique_phone(
    real_connector: ContactsConnector, test_group: str
) -> None:
    """A contact created with a unique phone number is findable via Apple's
    phone predicate, which normalizes punctuation and country codes."""
    # 14-digit suffix gives an effectively unique number against any
    # real address book; the leading +1 makes Apple parse it as US E.164.
    unique_phone = f"+1555{uuid.uuid4().int % 10**10:010d}"
    identifier = real_connector._run_cn_create_contact(
        fields={
            "given_name": "PhoneSearch",
            "family_name": "Fixture",
            "phones": [{"label_raw": "_$!<Mobile>!$_", "value": unique_phone}],
        },
        group_identifier=test_group,
    )
    try:
        results = real_connector._run_cn_search_contacts(
            field="phone", value=unique_phone, limit=10
        )
        ids = {entry["id"] for entry in results}
        assert identifier in ids
    finally:
        real_connector._run_cn_delete_contact(identifier)


def test_search_finds_contact_by_unique_email(
    real_connector: ContactsConnector, test_group: str
) -> None:
    """A contact created with a unique email is findable via the email
    predicate. Uses the .invalid TLD so the address can never collide."""
    unique_email = f"integ-{uuid.uuid4().hex}@mcp-test.invalid"
    identifier = real_connector._run_cn_create_contact(
        fields={
            "given_name": "EmailSearch",
            "family_name": "Fixture",
            "emails": [{"label_raw": "_$!<Work>!$_", "value": unique_email}],
        },
        group_identifier=test_group,
    )
    try:
        results = real_connector._run_cn_search_contacts(
            field="email", value=unique_email, limit=10
        )
        ids = {entry["id"] for entry in results}
        assert identifier in ids
    finally:
        real_connector._run_cn_delete_contact(identifier)


def test_search_finds_contact_by_unique_organization(
    real_connector: ContactsConnector, test_group: str
) -> None:
    """A contact created with a unique organization name is findable via
    the custom CONTAINS[cd] NSPredicate, exercising the org-mode path that
    has no built-in Apple predicate."""
    unique_org = f"MCP-Test-{uuid.uuid4().hex[:12]}"
    identifier = real_connector._run_cn_create_contact(
        fields={
            "given_name": "OrgSearch",
            "family_name": "Fixture",
            "organization": unique_org,
        },
        group_identifier=test_group,
    )
    try:
        results = real_connector._run_cn_search_contacts(
            field="organization", value=unique_org, limit=10
        )
        ids = {entry["id"] for entry in results}
        assert identifier in ids
    finally:
        real_connector._run_cn_delete_contact(identifier)


# ---------------------------------------------------------------------------
# _run_cn_fetch_group
# ---------------------------------------------------------------------------


def test_fetch_group_returns_test_group(
    real_connector: ContactsConnector, test_group: str
) -> None:
    """Fetching the test group's identifier returns a CNGroup whose name
    matches the configured CONTACTS_TEST_GROUP."""
    group = real_connector._run_cn_fetch_group(test_group)
    assert group is not None
    assert str(group.name()) == "MCP-Test"


def test_fetch_group_returns_none_for_missing_id(
    real_connector: ContactsConnector,
) -> None:
    """Fabricated identifier returns None."""
    fabricated = f"fabricated-{uuid.uuid4()}"
    assert real_connector._run_cn_fetch_group(fabricated) is None
