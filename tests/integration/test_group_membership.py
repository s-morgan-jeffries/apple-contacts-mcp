"""Integration tests for group-membership writes.

The add path uses ``CNSaveRequest.addMember:toGroup:`` and works as
documented. The remove path uses **AppleScript** (`remove p from g`)
because Apple's ``CNSaveRequest.removeMember:fromGroup:`` silently
no-ops despite reporting ``ok=True`` — empirically discovered during
issue #18, locked in by ``test_add_then_remove_cycle`` here.

Two probe tests document Apple's behavior empirically (idempotency on
duplicate add / remove-when-not-member). They don't assert a specific
outcome — they just capture and log what happens, so future readers can
see the observed behavior in CI logs.

Skipped by default; opt in with ``--run-integration``.
"""

from __future__ import annotations

import logging
import uuid

import pytest

from apple_contacts_mcp.contacts_connector import ContactsConnector
from apple_contacts_mcp.exceptions import (
    ContactsAppleScriptError,
    ContactsError,
    ContactsNotFoundError,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        "not config.getoption('--run-integration')",
        reason="Integration tests opt-in via --run-integration",
    ),
]

logger = logging.getLogger(__name__)


def test_add_then_remove_cycle(
    real_connector: ContactsConnector,
    test_group: str,
) -> None:
    """Full membership-write round-trip: remove → confirm gone → re-add →
    confirm present. Uses a contact created OUTSIDE the test group so we
    can exercise the add path cleanly (the standard ``tmp_contact``
    fixture creates inside MCP-Test, which we want to keep clean for this
    test)."""
    unique_token = f"MembershipCycle{uuid.uuid4().hex[:8]}"
    contact_id = real_connector._run_cn_create_contact(
        fields={"given_name": unique_token, "family_name": "Member"},
        group_identifier=None,  # NOT in MCP-Test
    )
    try:
        # Pre-condition: the contact is NOT in the test group.
        members = real_connector._run_cn_contacts_in_group(
            test_group, limit=200
        )
        assert contact_id not in {m["id"] for m in members}

        # Add → present.
        real_connector._run_cn_add_contact_to_group(contact_id, test_group)
        members = real_connector._run_cn_contacts_in_group(
            test_group, limit=200
        )
        assert contact_id in {m["id"] for m in members}, (
            "add_contact_to_group should make the contact a member"
        )

        # Remove → gone.
        real_connector._run_applescript_remove_contact_from_group(
            contact_id, test_group
        )
        members = real_connector._run_cn_contacts_in_group(
            test_group, limit=200
        )
        assert contact_id not in {m["id"] for m in members}, (
            "remove_contact_from_group should remove the membership edge"
        )
    finally:
        real_connector._run_cn_delete_contact(contact_id)


def test_not_found_when_contact_missing(
    real_connector: ContactsConnector,
    test_group: str,
) -> None:
    fabricated_contact = f"{uuid.uuid4()}:ABPerson"
    with pytest.raises(ContactsNotFoundError) as exc_info:
        real_connector._run_cn_add_contact_to_group(
            fabricated_contact, test_group
        )
    assert "Contact not found" in str(exc_info.value)


def test_not_found_when_group_missing(
    real_connector: ContactsConnector,
    test_group: str,
    tmp_contact: str,
) -> None:
    fabricated_group = f"{uuid.uuid4()}:ABGroup"
    with pytest.raises(ContactsNotFoundError) as exc_info:
        real_connector._run_cn_add_contact_to_group(
            tmp_contact, fabricated_group
        )
    assert "Group not found" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Empirical probes — document Apple's behavior, don't assert a specific shape
# ---------------------------------------------------------------------------


def test_add_when_already_member_probe(
    real_connector: ContactsConnector,
    test_group: str,
    tmp_contact: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``tmp_contact`` is already in MCP-Test (created by the fixture).
    Calling add again — does Apple no-op or raise?

    This test always passes; it only documents Apple's behavior. Read the
    ``observed_behavior`` log line in CI output to see what happened.
    """
    caplog.set_level(logging.INFO, logger="apple_contacts_mcp")
    try:
        real_connector._run_cn_add_contact_to_group(tmp_contact, test_group)
        observed = "no-op (silent success)"
    except ContactsError as exc:
        observed = f"raised ContactsError: {exc}"
    except ContactsAppleScriptError as exc:  # pragma: no cover (unlikely)
        observed = f"raised ContactsAppleScriptError: {exc}"
    logger.info("observed_behavior_duplicate_add: %s", observed)


def test_remove_when_not_member_probe(
    real_connector: ContactsConnector,
    test_group: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Create a contact NOT in the test group; call remove. Does Apple
    no-op or raise? Logs the observed behavior; doesn't assert a shape."""
    caplog.set_level(logging.INFO, logger="apple_contacts_mcp")
    contact_id = real_connector._run_cn_create_contact(
        fields={
            "given_name": f"NotMember{uuid.uuid4().hex[:8]}",
            "family_name": "Probe",
        },
        group_identifier=None,
    )
    try:
        real_connector._run_applescript_remove_contact_from_group(
            contact_id, test_group
        )
        observed = "no-op (silent success)"
    except ContactsError as exc:
        observed = f"raised ContactsError: {exc}"
    finally:
        real_connector._run_cn_delete_contact(contact_id)
    logger.info("observed_behavior_remove_when_not_member: %s", observed)


def test_cross_container_probe(
    real_connector: ContactsConnector,
    test_group: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """If the test machine has ≥2 containers, try to add a contact created
    in the non-default container to the MCP-Test group (which lives in
    the default container). Otherwise skip.

    This documents Apple's actual error wording so a v0.2.x follow-up can
    decide whether to translate to a typed error_type. Doesn't assert a
    specific shape — just that *something* fails (or, surprisingly, that
    Apple silently accepts cross-container memberships).
    """
    store = real_connector._get_store()
    containers, err = store.containersMatchingPredicate_error_(None, None)
    if containers is None:
        pytest.skip(f"Cannot enumerate containers: {err}")
    if len(containers) < 2:
        pytest.skip(
            f"Need ≥2 containers to probe cross-container error; "
            f"got {len(containers)}"
        )

    # Pick the non-default container (the test group lives in the default).
    default_id = str(containers[0].identifier())
    other = next(
        (c for c in containers if str(c.identifier()) != default_id), None
    )
    if other is None:
        pytest.skip("No non-default container available")

    other_id = str(other.identifier())
    caplog.set_level(logging.INFO, logger="apple_contacts_mcp")

    # Create a contact directly in the non-default container.
    from Contacts import CNMutableContact, CNSaveRequest

    mutable = CNMutableContact.alloc().init()
    mutable.setGivenName_(f"CrossContainer{uuid.uuid4().hex[:8]}")
    mutable.setFamilyName_("Probe")
    save_req = CNSaveRequest.alloc().init()
    save_req.addContact_toContainerWithIdentifier_(mutable, other_id)
    ok, save_err = store.executeSaveRequest_error_(save_req, None)
    if not ok:
        pytest.skip(f"Could not create contact in non-default container: {save_err}")
    other_contact_id = str(mutable.identifier())

    try:
        try:
            real_connector._run_cn_add_contact_to_group(
                other_contact_id, test_group
            )
            observed = "silent success (cross-container memberships allowed)"
        except ContactsError as exc:
            observed = f"raised ContactsError: {exc}"
    finally:
        # Best-effort cleanup
        try:
            real_connector._run_cn_delete_contact(other_contact_id)
        except Exception as exc:  # pragma: no cover (defensive)
            logger.warning(
                "Cross-container probe cleanup failed for %s: %s",
                other_contact_id,
                exc,
            )

    logger.info("observed_behavior_cross_container: %s", observed)
