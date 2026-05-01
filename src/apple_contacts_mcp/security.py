"""Security primitives: input sanitization, test-mode safety gate.

Reference: `apple-mail-mcp/src/apple_mail_mcp/security.py`. The contacts gate
is simpler — only one axis (the test group) — and never imports PyObjC; it
resolves the test group's identifier via `osascript` so it can run anywhere
the rest of the package can.
"""

from __future__ import annotations

import logging
import os
import subprocess
from functools import lru_cache
from typing import Any

logger = logging.getLogger(__name__)


def sanitize_input(value: str) -> str:
    """Strip control characters and normalize whitespace from user input.

    Bootstrap stub: returns the input unchanged. Real implementation will
    follow the playbook's two-step pattern (sanitize_input then escape).
    """
    return value


# ---------------------------------------------------------------------------
# Test-mode safety gate
# ---------------------------------------------------------------------------

DESTRUCTIVE_OPERATIONS: frozenset[str] = frozenset(
    {
        "create_contact",
        "update_contact",
        "delete_contact",
    }
)
"""Operations gated by `check_test_mode_safety`.

Every new destructive op (group CRUD, group membership, etc.) MUST be added
here as part of its feature PR. Ops not in this set are fail-open — the gate
returns None for them.
"""


def check_test_mode_safety(
    operation: str, group: str | None = None
) -> dict[str, Any] | None:
    """Enforce test-mode safety. Returns None if allowed, error dict if blocked.

    In test mode (`CONTACTS_TEST_MODE=true`), destructive operations must
    target a group whose name or CN identifier matches `CONTACTS_TEST_GROUP`.

    Args:
        operation: The operation name (e.g., "create_contact"). Must match
            an entry in `DESTRUCTIVE_OPERATIONS` to be gated.
        group: The group the caller asserts they are operating against
            (name or CN identifier). Required for destructive ops in test
            mode.

    Returns:
        None if the operation is allowed; otherwise a dict shaped
        `{"success": False, "error": <message>, "error_type": "safety_violation"}`.
    """
    if not _is_test_mode_enabled():
        return None

    if operation not in DESTRUCTIVE_OPERATIONS:
        return None

    test_group = _get_test_group()
    if test_group is None:
        return _safety_error(
            operation, "CONTACTS_TEST_MODE is set but CONTACTS_TEST_GROUP is not"
        )

    if group is None:
        return _safety_error(
            operation,
            f"Test mode: {operation} requires explicit target group "
            f"matching CONTACTS_TEST_GROUP={test_group!r}",
        )

    if group not in _get_test_group_identifiers(test_group):
        return _safety_error(
            operation,
            f"Test mode: group {group!r} does not match "
            f"CONTACTS_TEST_GROUP={test_group!r}",
        )

    return None


def _is_test_mode_enabled() -> bool:
    return os.environ.get("CONTACTS_TEST_MODE", "").lower() == "true"


def _get_test_group() -> str | None:
    return os.environ.get("CONTACTS_TEST_GROUP")


@lru_cache(maxsize=4)
def _get_test_group_identifiers(test_group_name: str) -> frozenset[str]:
    """Return {name, CN identifier} for the configured test group.

    Resolves via `osascript` — the gate stays independent of the connector
    and free of PyObjC. Cached per process; tests must call
    `_get_test_group_identifiers.cache_clear()` between cases.

    On lookup failure (group missing, osascript timeout, permission denied),
    falls back to name-only matching with a warning. Degraded mode still
    enforces the test-group boundary by name.
    """
    identifiers: set[str] = {test_group_name}
    try:
        result = subprocess.run(
            [
                "/usr/bin/osascript",
                "-e",
                f'tell application "Contacts" to return id of group "{test_group_name}"',
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.warning(
            "Test-mode safety gate: failed to resolve id for group %r (%s); "
            "falling back to name-only matching",
            test_group_name,
            exc,
        )
        return frozenset(identifiers)

    if result.returncode == 0:
        cn_id = result.stdout.strip()
        if cn_id:
            identifiers.add(cn_id)
    else:
        logger.warning(
            "Test-mode safety gate: failed to resolve id for group %r (exit %d): "
            "%s; falling back to name-only matching",
            test_group_name,
            result.returncode,
            (result.stderr or "").strip(),
        )
    return frozenset(identifiers)


def _safety_error(operation: str, message: str) -> dict[str, Any]:
    """Build the standard safety-violation error dict."""
    logger.warning("Safety violation in %s: %s", operation, message)
    return {
        "success": False,
        "error": message,
        "error_type": "safety_violation",
    }
