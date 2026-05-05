"""Integration tests for the TCC authorization helpers.

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


_VALID_STATUSES = {"notDetermined", "restricted", "denied", "authorized", "limited"}


def test_authorization_status_returns_known_value(
    real_connector: ContactsConnector,
) -> None:
    """The status getter returns one of the five documented strings."""
    status = real_connector._run_cn_authorization_status()
    assert status in _VALID_STATUSES


def test_request_access_when_already_authorized(
    real_connector: ContactsConnector,
) -> None:
    """Once authorized, calling requestAccess again is idempotent (returns True
    without re-prompting)."""
    status = real_connector._run_cn_authorization_status()
    if status not in ("authorized", "limited"):
        pytest.skip(
            f"This test requires authorized/limited status; got {status}."
        )
    assert real_connector._run_cn_request_access() is True
