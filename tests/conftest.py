"""Pytest configuration and fixtures.

The session fixtures `real_connector`, `test_group`, `tmp_contact`, and
`integration_env` live here (not in `tests/integration/conftest.py`) so
both the integration suite and `tests/benchmarks/` can request them
without pytest 9's "pytest_plugins outside top-level conftest" warning
or double-registration when collection walks subdirectories.

The fixtures only fire when a test references them, so unit tests stay
fast and PyObjC-free.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

import pytest

from apple_contacts_mcp.contacts_connector import ContactsConnector

logger = logging.getLogger(__name__)
TEST_GROUP_NAME = "MCP-Test"


def pytest_addoption(parser: pytest.Parser) -> None:
    """Add custom command line options."""
    parser.addoption(
        "--run-integration",
        action="store_true",
        default=False,
        help="Run integration tests (requires Apple Contacts setup)",
    )
    parser.addoption(
        "--run-benchmark",
        action="store_true",
        default=False,
        help="Run benchmark tests (requires Apple Contacts setup; produces timings)",
    )
    parser.addoption(
        "--capture-baseline",
        action="store_true",
        default=False,
        help=(
            "When running benchmarks, write observed timings to baseline.json "
            "instead of comparing against it. Use after intentional perf changes."
        ),
    )


def pytest_configure(config: pytest.Config) -> None:
    """Configure pytest."""
    config.addinivalue_line(
        "markers", "integration: mark test as integration test (requires --run-integration)"
    )
    config.addinivalue_line("markers", "e2e: mark test as end-to-end test (full MCP stack)")
    config.addinivalue_line("markers", "benchmark: mark test as performance benchmark")
    config.addinivalue_line("markers", "slow: mark test as slow-running")


# ---------------------------------------------------------------------------
# Real-CN session fixtures (shared by tests/integration/ and tests/benchmarks/)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def integration_env() -> Iterator[None]:
    """Force CONTACTS_TEST_MODE + CONTACTS_TEST_GROUP for the session."""
    mp = pytest.MonkeyPatch()
    mp.setenv("CONTACTS_TEST_MODE", "true")
    mp.setenv("CONTACTS_TEST_GROUP", TEST_GROUP_NAME)
    from apple_contacts_mcp.security import _get_test_group_identifiers

    _get_test_group_identifiers.cache_clear()
    try:
        yield
    finally:
        mp.undo()


@pytest.fixture(scope="session")
def real_connector() -> ContactsConnector:
    """Real connector. Skips the session if TCC is unavailable.

    Timeout is generous (30s) because the first osascript→Contacts.app call
    pays a cold-start cost: launching Contacts.app, the Automation TCC check,
    and the initial scripting bridge handshake can take 10–20s. Subsequent
    calls return in <200ms.
    """
    c = ContactsConnector(timeout=30.0)
    status = c._run_cn_authorization_status()
    if status in ("denied", "restricted"):
        pytest.skip(
            f"Contacts permission status={status}; grant in System "
            "Settings → Privacy & Security → Contacts and re-run."
        )
    if status == "notDetermined":
        try:
            granted = c._run_cn_request_access()
        except Exception as exc:
            pytest.skip(f"requestAccess failed: {exc}")
        if not granted:
            pytest.skip("User did not grant Contacts access.")
    try:
        c._run_applescript(
            'tell application "Contacts" to count of every person'
        )
    except Exception as exc:
        logger.warning("Contacts.app warm-up failed: %s", exc)
    return c


@pytest.fixture(scope="session")
def test_group(
    real_connector: ContactsConnector, integration_env: None
) -> Iterator[str]:
    """Find or create the MCP-Test group; yield its identifier; cleanup."""
    group_id = _find_or_create_test_group(real_connector)
    try:
        yield group_id
    finally:
        _cleanup_test_group(real_connector, group_id)


@pytest.fixture
def tmp_contact(
    real_connector: ContactsConnector, test_group: str
) -> Iterator[str]:
    """Create a contact in the test group; yield its identifier; delete on
    teardown (best-effort, doesn't mask test failures)."""
    fields = {"given_name": "Integration", "family_name": "Fixture"}
    identifier = real_connector._run_cn_create_contact(
        fields=fields, group_identifier=test_group
    )
    try:
        yield identifier
    finally:
        try:
            real_connector._run_cn_delete_contact(identifier)
        except Exception as exc:
            logger.warning("tmp_contact teardown failed for %s: %s", identifier, exc)


# ---------------------------------------------------------------------------
# Raw CN helpers used by the fixtures (kept module-private)
# ---------------------------------------------------------------------------


def _find_or_create_test_group(connector: ContactsConnector) -> str:
    """Return the CN identifier of the MCP-Test group, creating it if missing."""
    from Contacts import CNMutableGroup, CNSaveRequest

    store = connector._get_store()
    existing = _find_group_by_name(connector, TEST_GROUP_NAME)
    if existing is not None:
        identifier = str(existing.identifier())
        logger.info(
            "Reusing existing %r group (id=%s). Members will be deleted at "
            "session teardown.",
            TEST_GROUP_NAME,
            identifier,
        )
        return identifier

    new_group = CNMutableGroup.alloc().init()
    new_group.setName_(TEST_GROUP_NAME)
    save_req = CNSaveRequest.alloc().init()
    save_req.addGroup_toContainerWithIdentifier_(new_group, None)
    ok, err = store.executeSaveRequest_error_(save_req, None)
    if not ok:
        raise RuntimeError(f"Failed to create test group: {err}")
    return str(new_group.identifier())


def _find_group_by_name(connector: ContactsConnector, name: str) -> Any | None:
    """Return the first CNGroup whose .name() matches; None if no match."""
    store = connector._get_store()
    groups, err = store.groupsMatchingPredicate_error_(None, None)
    if groups is None:
        raise RuntimeError(f"groupsMatchingPredicate failed: {err}")
    for g in groups:
        if str(g.name()) == name:
            return g
    return None


def _cleanup_test_group(connector: ContactsConnector, group_id: str) -> None:
    """Delete every contact in the group, then the group itself.

    Best-effort. Logs failures, never raises — the session is wrapping
    up and we don't want to mask real test failures.
    """
    from Contacts import CNContact, CNContactIdentifierKey, CNSaveRequest

    try:
        store = connector._get_store()
        pred = CNContact.predicateForContactsInGroupWithIdentifier_(group_id)
        contacts, err = store.unifiedContactsMatchingPredicate_keysToFetch_error_(
            pred, [CNContactIdentifierKey], None
        )
        if contacts is None:
            logger.warning(
                "Could not enumerate %r members for cleanup: %s",
                TEST_GROUP_NAME,
                err,
            )
            contacts = []

        save_req = CNSaveRequest.alloc().init()
        for c in contacts:
            save_req.deleteContact_(c.mutableCopy())

        try:
            group = connector._run_cn_fetch_group(group_id)
        except Exception as exc:
            logger.warning("fetch_group during cleanup failed: %s", exc)
            group = None
        if group is not None:
            save_req.deleteGroup_(group.mutableCopy())

        ok, err = store.executeSaveRequest_error_(save_req, None)
        if not ok:
            logger.warning(
                "Cleanup save failed for %r (group_id=%s): %s",
                TEST_GROUP_NAME,
                group_id,
                err,
            )
    except Exception as exc:
        logger.warning("Cleanup of %r raised: %s", TEST_GROUP_NAME, exc)
