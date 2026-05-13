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
from collections.abc import Awaitable, Callable
from functools import lru_cache
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastmcp.server.context import Context

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Test-mode safety gate
# ---------------------------------------------------------------------------

DESTRUCTIVE_OPERATIONS: frozenset[str] = frozenset(
    {
        "create_contact",
        "update_contact",
        "delete_contact",
        "write_note",
        "add_contact_to_group",
        "remove_contact_from_group",
        "import_vcard",
        "create_group",
        "rename_group",
        "delete_group",
        "write_photo",
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


def require_test_mode_for(operation: str) -> dict[str, Any] | None:
    """Refuses an operation when CONTACTS_TEST_MODE is not 'true'.

    Use for destructive ops that lack a confirmation UX — they are
    only safe to expose in test mode until v0.4.0 ships the
    confirmation flow (#24).

    Returns None when test mode is enabled; otherwise a structured
    safety_violation error dict.
    """
    if not _is_test_mode_enabled():
        return _safety_error(
            operation,
            f"{operation} is only available with CONTACTS_TEST_MODE=true. "
            f"The full destructive UX (with confirmation prompts) ships "
            f"in v0.4.0 (#36).",
        )
    return None


def _safety_error(operation: str, message: str) -> dict[str, Any]:
    """Build the standard safety-violation error dict."""
    logger.warning("Safety violation in %s: %s", operation, message)
    return {
        "success": False,
        "error": message,
        "error_type": "safety_violation",
    }


# ---------------------------------------------------------------------------
# Destructive-op confirmation UX (FastMCP elicitation)
# ---------------------------------------------------------------------------

# The two yes/no choices presented to the user. The exact strings are part
# of the protocol — `_confirm_destructive` matches on `_CONFIRM_YES` to
# decide whether to proceed.
_CONFIRM_YES = "Yes, delete"
_CONFIRM_NO = "No, cancel"


async def _confirm_destructive(
    ctx: Context,
    *,
    operation: str,
    entity_kind: str,
    identifier: str,
    preview_lookup: Callable[[], dict[str, Any] | None]
    | Callable[[], Awaitable[dict[str, Any] | None]],
    describe: Callable[[dict[str, Any]], str],
) -> dict[str, Any] | None:
    """Confirm a destructive op via FastMCP elicitation. Returns None when
    the user confirmed; an error dict otherwise.

    Flow:
      1. Pre-fetch the target entity via ``preview_lookup`` so the prompt
         shows a human-readable preview. Lookup returning ``None`` short-
         circuits to a ``not_found`` response without prompting.
      2. Call ``ctx.elicit()`` with a two-option choice. The accepted result
         must be ``_CONFIRM_YES`` to proceed; anything else (including
         declined / cancelled) returns ``user_declined``.
      3. If the client doesn't support elicitation, ``ctx.elicit()`` raises.
         Catch broadly and return ``safety_violation`` pointing at test mode
         as the bypass — same posture as the v0.1.0+ test-mode gate.
    """
    from fastmcp.server.elicitation import (
        AcceptedElicitation,
        CancelledElicitation,
        DeclinedElicitation,
    )

    try:
        preview = preview_lookup()
    except Exception as exc:
        logger.error("%s confirmation preview failed: %s", operation, exc)
        return {
            "success": False,
            "error": f"{operation} preview failed: {exc}",
            "error_type": "unknown",
        }
    if preview is None:
        return {
            "success": False,
            "error": f"{entity_kind.capitalize()} not found: {identifier!r}",
            "error_type": "not_found",
        }

    description = describe(preview)
    prompt = (
        f"Delete {entity_kind} {description!r} ({identifier})? "
        f"This cannot be undone."
    )

    try:
        result = await ctx.elicit(
            prompt,
            response_type=[_CONFIRM_YES, _CONFIRM_NO],
            response_title=f"Confirm {operation}",
        )
    except Exception as exc:
        logger.warning(
            "%s elicitation failed (client may not support elicit): %s",
            operation,
            exc,
        )
        return _safety_error(
            operation,
            f"{operation} requires user confirmation, but the client doesn't "
            f"support interactive prompts. Set CONTACTS_TEST_MODE=true with "
            f"CONTACTS_TEST_GROUP and supply group_identifier to bypass for "
            f"test-harness use.",
        )

    if isinstance(result, AcceptedElicitation) and result.data == _CONFIRM_YES:
        return None

    if isinstance(result, (DeclinedElicitation, CancelledElicitation)):
        action = "declined" if isinstance(result, DeclinedElicitation) else "cancelled"
    else:
        # Accepted but with "No, cancel" (or any other value)
        action = "cancelled"

    logger.info(
        "%s %s by user (identifier=%s, preview=%s)",
        operation,
        action,
        identifier,
        description,
    )
    return {
        "success": False,
        "error": (
            f"User {action} the {operation} of {entity_kind} "
            f"{description!r} ({identifier})."
        ),
        "error_type": "user_declined",
    }
