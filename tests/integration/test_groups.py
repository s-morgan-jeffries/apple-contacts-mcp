"""Integration tests for `_run_cn_list_groups` and `_run_cn_contacts_in_group`.

CN-only ops, but the smoke tests guard against macOS API drift (Apple's
`groupsMatchingPredicate` and `predicateForContactsInGroupWithIdentifier_`
semantics aren't fully documented).

Skipped by default; opt in with ``--run-integration``.
"""

from __future__ import annotations

import pytest

from apple_contacts_mcp.contacts_connector import ContactsConnector

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        "not config.getoption('--run-integration')",
        reason="Integration tests opt-in via --run-integration",
    ),
]


def test_list_groups_includes_test_group(
    real_connector: ContactsConnector, test_group: str
) -> None:
    """The MCP-Test fixture group must show up in the enumeration."""
    groups = real_connector._run_cn_list_groups()
    by_id = {g["id"]: g for g in groups}
    assert test_group in by_id, (
        f"MCP-Test group {test_group!r} not in {sorted(by_id.keys())}"
    )

    entry = by_id[test_group]
    assert isinstance(entry["id"], str) and entry["id"]
    assert isinstance(entry["name"], str) and entry["name"]
    assert isinstance(entry["container_id"], str) and entry["container_id"], (
        f"container_id should be non-empty: {entry!r}"
    )


def test_list_groups_entries_have_only_three_keys(
    real_connector: ContactsConnector, test_group: str
) -> None:
    """Lock the response shape; if Apple adds a new key on a future macOS,
    the connector should still produce only the documented three."""
    for g in real_connector._run_cn_list_groups():
        assert set(g.keys()) == {"id", "name", "container_id"}


def test_contacts_in_group_finds_tmp_contact(
    real_connector: ContactsConnector,
    test_group: str,
    tmp_contact: str,
) -> None:
    """A tmp_contact created in MCP-Test must be findable via the membership
    predicate, with the documented 4-field shape."""
    members = real_connector._run_cn_contacts_in_group(
        test_group, limit=200
    )
    ids = {m["id"] for m in members}
    assert tmp_contact in ids, (
        f"tmp_contact {tmp_contact!r} not in members {sorted(ids)}"
    )

    [member] = [m for m in members if m["id"] == tmp_contact]
    assert set(member.keys()) == {
        "id",
        "given_name",
        "family_name",
        "organization",
    }


def test_contacts_in_group_returns_empty_for_unknown_id(
    real_connector: ContactsConnector,
) -> None:
    """Apple's predicate returns [] (not error) for unknown group_ids; the
    connector surfaces that directly. The server-tool layer is what
    translates this into `not_found` via a separate `_run_cn_fetch_group`
    pre-flight."""
    fabricated = "fabricated-group-uuid-12345:ABGroup"
    assert (
        real_connector._run_cn_contacts_in_group(fabricated, limit=10) == []
    )
