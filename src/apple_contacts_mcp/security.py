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
import time
from collections import deque
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
    # preview_lookup may return any framework-specific object (dict for
    # contact previews, CNGroup for group previews) so we type its return
    # as Any. `describe` consumes whatever preview_lookup returns and
    # produces a human-readable string for the prompt. Mypy 2.x is strict
    # enough to catch the previously-tolerated mismatch — using Any here
    # is the smallest fix that preserves the existing call sites.
    preview_lookup: Callable[[], Any] | Callable[[], Awaitable[Any]],
    describe: Callable[[Any], str],
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
        # mypy 2.x picks the `response_type: None` overload here instead of
        # the `list[str]` one — FastMCP's overload set isn't ordered in a
        # way mypy 2.x resolves correctly for list literals. The runtime
        # behavior is correct (FastMCP dispatches on the actual type), so
        # we silence the type-check noise rather than refactor around it.
        result: Any = await ctx.elicit(
            prompt,
            response_type=[_CONFIRM_YES, _CONFIRM_NO],  # type: ignore[arg-type]
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


# ---------------------------------------------------------------------------
# Rate limiting (#35)
#
# Sliding-window per-tier rate limiter. Built but not wired into tools yet;
# wiring is tracked under #46. The pattern mirrors apple-mail-mcp's security
# module.
# ---------------------------------------------------------------------------

# Tier limits: (max_calls, window_seconds). The window is seconds-since-now;
# calls older than now-window are evicted from the deque on every check, so
# the limit applies to the trailing window rather than fixed buckets.
TIER_LIMITS: dict[str, tuple[int, float]] = {
    "cheap_reads": (60, 60.0),
    "expensive_ops": (20, 60.0),
    "destructives": (5, 60.0),
}

# Per-tool tier assignment. Every @mcp.tool() name in server.py should
# appear here once #46 wires check_rate_limit into the tool entries.
# Adding a new tool without a tier mapping triggers a logger warning at
# check time but passes through (fail open) so a missed mapping doesn't
# brick a release.
OPERATION_TIERS: dict[str, str] = {
    "check_authorization": "cheap_reads",
    "list_contacts": "cheap_reads",
    "get_contact": "cheap_reads",
    "list_groups": "cheap_reads",
    "get_contacts_in_group": "cheap_reads",
    "list_containers": "cheap_reads",
    "read_note": "cheap_reads",
    "read_photo": "cheap_reads",
    "export_vcard": "cheap_reads",
    "search_contacts": "expensive_ops",
    "create_contact": "expensive_ops",
    "update_contact": "expensive_ops",
    "import_vcard": "expensive_ops",
    "write_note": "expensive_ops",
    "write_photo": "expensive_ops",
    "add_contact_to_group": "expensive_ops",
    "remove_contact_from_group": "expensive_ops",
    "create_group": "expensive_ops",
    "rename_group": "expensive_ops",
    "delete_contact": "destructives",
    "delete_group": "destructives",
}


class RateLimiter:
    """Sliding-window rate limiter with per-tier tracking.

    Thread-unsafe by design — FastMCP serializes tool calls per session,
    so we don't pay for locking. If we ever multiplex concurrent tool
    handlers (e.g., parallel HTTP requests) the deque mutations would
    need protection; defer until that materializes.
    """

    def __init__(self) -> None:
        self._windows: dict[str, deque[float]] = {
            tier: deque() for tier in TIER_LIMITS
        }

    def check(self, tier: str) -> bool:
        """Return True if the call is allowed under the tier's limit.
        On True, the timestamp is recorded; on False, no state changes."""
        now = time.monotonic()
        max_calls, window = TIER_LIMITS[tier]
        q = self._windows[tier]
        # Evict timestamps that have aged out of the window.
        while q and q[0] <= now - window:
            q.popleft()
        if len(q) >= max_calls:
            return False
        q.append(now)
        return True

    def reset(self) -> None:
        """Clear every tier's window. For test isolation between cases."""
        for q in self._windows.values():
            q.clear()


# Module singleton. Tests call rate_limiter.reset() between cases.
rate_limiter = RateLimiter()


def check_rate_limit(
    operation: str, params: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """Returns None if the call is allowed; a rate_limited error dict if not.

    ``params`` is accepted for parity with apple-mail's signature; eventually
    fed to the audit logger (#47) on deny. Currently unused.

    Unknown operations pass through with a logger warning — the rate limiter
    fails open if a tool ships without a tier mapping, so a missed
    OPERATION_TIERS entry won't brick the server. Release-gate parity
    checks should catch the missing mapping at PR time once #46 wires
    this in.
    """
    _ = params  # accepted for forward-compat with #47
    tier = OPERATION_TIERS.get(operation)
    if tier is None:
        logger.warning(
            "rate-limit check on unmapped operation %r; allowing through "
            "(add an entry to OPERATION_TIERS in security.py)",
            operation,
        )
        return None
    if rate_limiter.check(tier):
        return None
    max_calls, window = TIER_LIMITS[tier]
    return {
        "success": False,
        "error": (
            f"Rate limit exceeded for {operation}: {max_calls} calls per "
            f"{int(window)}s for {tier!r} operations."
        ),
        "error_type": "rate_limited",
    }
