"""Unit tests for the test-mode safety gate (issue #6).

Mirrors `apple-mail-mcp/tests/unit/test_security.py::TestCheckTestModeSafety`
adapted for the simpler contacts pattern (single gating axis: the test group).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from apple_contacts_mcp.security import (
    DESTRUCTIVE_OPERATIONS,
    OPERATION_TIERS,
    TIER_LIMITS,
    OperationLogger,
    RateLimiter,
    _get_test_group_identifiers,
    check_rate_limit,
    check_test_mode_safety,
    operation_logger,
    rate_limiter,
    require_test_mode_for,
)


class TestCheckTestModeSafety:
    def setup_method(self) -> None:
        # Resolver is process-cached; each test must start with a clean slate.
        _get_test_group_identifiers.cache_clear()

    # ------------------------------------------------------------------
    # Test mode disabled
    # ------------------------------------------------------------------

    def test_no_test_mode_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CONTACTS_TEST_MODE", raising=False)
        assert check_test_mode_safety("create_contact", group="Anything") is None
        assert check_test_mode_safety("delete_contact") is None

    def test_test_mode_explicit_false_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CONTACTS_TEST_MODE", "false")
        assert check_test_mode_safety("create_contact", group="Anything") is None

    def test_test_mode_value_is_case_insensitive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CONTACTS_TEST_MODE", "TRUE")
        monkeypatch.delenv("CONTACTS_TEST_GROUP", raising=False)
        result = check_test_mode_safety("create_contact")
        assert result is not None  # gate is engaged

    # ------------------------------------------------------------------
    # Non-destructive operations are not gated
    # ------------------------------------------------------------------

    def test_non_destructive_op_returns_none_even_in_test_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CONTACTS_TEST_MODE", "true")
        # No CONTACTS_TEST_GROUP set, but list_contacts is not destructive.
        monkeypatch.delenv("CONTACTS_TEST_GROUP", raising=False)
        assert check_test_mode_safety("list_contacts") is None
        assert check_test_mode_safety("search_contacts", group="Anything") is None

    # ------------------------------------------------------------------
    # Destructive ops without proper config are blocked
    # ------------------------------------------------------------------

    def test_destructive_op_without_test_group_env_blocked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CONTACTS_TEST_MODE", "true")
        monkeypatch.delenv("CONTACTS_TEST_GROUP", raising=False)
        result = check_test_mode_safety("create_contact", group="MCP-Test")
        assert result is not None
        assert result["error_type"] == "safety_violation"
        assert "CONTACTS_TEST_GROUP" in result["error"]

    def test_destructive_op_without_group_arg_blocked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CONTACTS_TEST_MODE", "true")
        monkeypatch.setenv("CONTACTS_TEST_GROUP", "MCP-Test")
        with patch("subprocess.run", side_effect=_subprocess_failure()):
            result = check_test_mode_safety("create_contact")
        assert result is not None
        assert result["error_type"] == "safety_violation"
        assert "create_contact" in result["error"]
        assert "MCP-Test" in result["error"]

    def test_group_mismatch_returns_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CONTACTS_TEST_MODE", "true")
        monkeypatch.setenv("CONTACTS_TEST_GROUP", "MCP-Test")
        with patch("subprocess.run", side_effect=_subprocess_failure()):
            result = check_test_mode_safety("delete_contact", group="OtherGroup")
        assert result is not None
        assert result["error_type"] == "safety_violation"
        assert "OtherGroup" in result["error"]
        assert "MCP-Test" in result["error"]

    # ------------------------------------------------------------------
    # Allowed paths
    # ------------------------------------------------------------------

    def test_group_name_matches_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CONTACTS_TEST_MODE", "true")
        monkeypatch.setenv("CONTACTS_TEST_GROUP", "MCP-Test")
        with patch("subprocess.run", side_effect=_subprocess_failure()):
            assert check_test_mode_safety("create_contact", group="MCP-Test") is None

    def test_group_id_matches_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CONTACTS_TEST_MODE", "true")
        monkeypatch.setenv("CONTACTS_TEST_GROUP", "MCP-Test")
        cn_id = "ABCD1234-AAAA-BBBB-CCCC-DEADBEEF0001:ABGroup"
        ok = subprocess.CompletedProcess(args=[], returncode=0, stdout=cn_id + "\n", stderr="")
        with patch("subprocess.run", return_value=ok):
            assert check_test_mode_safety("update_contact", group=cn_id) is None

    # ------------------------------------------------------------------
    # Resolver fallback behavior
    # ------------------------------------------------------------------

    def test_resolver_failure_falls_back_to_name_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CONTACTS_TEST_MODE", "true")
        monkeypatch.setenv("CONTACTS_TEST_GROUP", "MCP-Test")
        bad = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="Can't get group"
        )
        with patch("subprocess.run", return_value=bad):
            # Name still matches in degraded mode.
            assert check_test_mode_safety("create_contact", group="MCP-Test") is None
            # But an arbitrary id does not.
            result = check_test_mode_safety(
                "create_contact", group="some-unresolved-id"
            )
        assert result is not None
        assert result["error_type"] == "safety_violation"

    def test_resolver_timeout_falls_back_to_name_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CONTACTS_TEST_MODE", "true")
        monkeypatch.setenv("CONTACTS_TEST_GROUP", "MCP-Test")

        def boom(*_args: Any, **_kwargs: Any) -> None:
            raise subprocess.TimeoutExpired(cmd=["osascript"], timeout=5)

        with patch("subprocess.run", side_effect=boom):
            assert check_test_mode_safety("create_contact", group="MCP-Test") is None

    def test_resolver_caches_per_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CONTACTS_TEST_MODE", "true")
        monkeypatch.setenv("CONTACTS_TEST_GROUP", "MCP-Test")
        ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="ID-1\n", stderr="")
        with patch("subprocess.run", return_value=ok) as mock_run:
            check_test_mode_safety("create_contact", group="MCP-Test")
            check_test_mode_safety("delete_contact", group="MCP-Test")
            check_test_mode_safety("update_contact", group="ID-1")
        assert mock_run.call_count == 1  # cached after first lookup

    # ------------------------------------------------------------------
    # Coverage of every gated op
    # ------------------------------------------------------------------

    @pytest.mark.parametrize("op", sorted(DESTRUCTIVE_OPERATIONS))
    def test_each_destructive_op_is_gated(
        self, op: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CONTACTS_TEST_MODE", "true")
        monkeypatch.delenv("CONTACTS_TEST_GROUP", raising=False)
        result = check_test_mode_safety(op, group="anything")
        assert result is not None
        assert result["error_type"] == "safety_violation"

    # ------------------------------------------------------------------
    # Error shape
    # ------------------------------------------------------------------

    def test_safety_error_shape_is_stable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CONTACTS_TEST_MODE", "true")
        monkeypatch.delenv("CONTACTS_TEST_GROUP", raising=False)
        result = check_test_mode_safety("create_contact")
        assert result is not None
        assert set(result.keys()) == {"success", "error", "error_type"}
        assert result["success"] is False
        assert result["error_type"] == "safety_violation"
        assert isinstance(result["error"], str) and result["error"]


def _subprocess_failure() -> Any:
    """Side effect: subprocess.run raises FileNotFoundError (no osascript).

    Forces _get_test_group_identifiers to take the fallback path so tests that
    aren't specifically about resolver behavior don't depend on a happy-path
    mock.
    """

    def _boom(*_args: Any, **_kwargs: Any) -> None:
        raise FileNotFoundError("osascript not available in test env")

    return _boom


# ---------------------------------------------------------------------------
# require_test_mode_for
# ---------------------------------------------------------------------------


class TestRequireTestModeFor:
    def test_env_unset_returns_safety_violation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CONTACTS_TEST_MODE", raising=False)
        result = require_test_mode_for("delete_contact")
        assert result is not None
        assert result["error_type"] == "safety_violation"
        assert "delete_contact" in result["error"]
        assert "v0.4.0" in result["error"]

    def test_env_false_returns_safety_violation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CONTACTS_TEST_MODE", "false")
        result = require_test_mode_for("delete_contact")
        assert result is not None
        assert result["error_type"] == "safety_violation"

    def test_env_true_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CONTACTS_TEST_MODE", "true")
        assert require_test_mode_for("delete_contact") is None

    def test_env_TRUE_case_insensitive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CONTACTS_TEST_MODE", "TRUE")
        assert require_test_mode_for("delete_contact") is None


# ---------------------------------------------------------------------------
# Issue #35: rate limiter
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=False)
def _fresh_rate_limiter() -> Any:
    """Fresh RateLimiter per test to avoid singleton bleed."""
    rate_limiter.reset()
    yield
    rate_limiter.reset()


class TestRateLimiterCore:
    """Direct tests against the RateLimiter class."""

    def test_allows_up_to_max_calls_then_denies(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Use a stub clock so we can reason about the window deterministically.
        fake_now = [0.0]
        monkeypatch.setattr(
            "apple_contacts_mcp.security.time.monotonic", lambda: fake_now[0]
        )
        rl = RateLimiter()
        max_calls, _window = TIER_LIMITS["expensive_ops"]
        # All max_calls should succeed.
        for i in range(max_calls):
            assert rl.check("expensive_ops") is True, f"call {i} unexpectedly denied"
        # The next call exceeds the limit.
        assert rl.check("expensive_ops") is False

    def test_window_slides_after_window_seconds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_now = [0.0]
        monkeypatch.setattr(
            "apple_contacts_mcp.security.time.monotonic", lambda: fake_now[0]
        )
        rl = RateLimiter()
        max_calls, window = TIER_LIMITS["destructives"]
        # Fill the window.
        for _ in range(max_calls):
            assert rl.check("destructives") is True
        assert rl.check("destructives") is False
        # Advance past the window.
        fake_now[0] = window + 0.1
        assert rl.check("destructives") is True

    def test_reset_clears_all_windows(self) -> None:
        rl = RateLimiter()
        max_calls, _window = TIER_LIMITS["destructives"]
        for _ in range(max_calls):
            rl.check("destructives")
        assert rl.check("destructives") is False
        rl.reset()
        assert rl.check("destructives") is True

    def test_tiers_are_independent(self) -> None:
        rl = RateLimiter()
        # Exhaust destructives.
        for _ in range(TIER_LIMITS["destructives"][0]):
            rl.check("destructives")
        assert rl.check("destructives") is False
        # cheap_reads is unaffected.
        assert rl.check("cheap_reads") is True


class TestCheckRateLimit:
    """Tests against the module-level check_rate_limit function."""

    def test_allowed_call_returns_none(self, _fresh_rate_limiter: Any) -> None:
        assert check_rate_limit("list_contacts") is None

    def test_unmapped_operation_passes_through(
        self, _fresh_rate_limiter: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Fail-open behavior: an unrecognized op is allowed but logs a warning."""
        with caplog.at_level("WARNING", logger="apple_contacts_mcp.security"):
            assert check_rate_limit("future_unmapped_tool") is None
        assert any(
            "unmapped operation" in r.message and "future_unmapped_tool" in r.message
            for r in caplog.records
        )

    def test_exceeding_limit_returns_rate_limited_error(
        self, _fresh_rate_limiter: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_now = [0.0]
        monkeypatch.setattr(
            "apple_contacts_mcp.security.time.monotonic", lambda: fake_now[0]
        )
        # destructives is tightest; fill it.
        for _ in range(TIER_LIMITS["destructives"][0]):
            assert check_rate_limit("delete_contact") is None
        # Next call is denied with the documented shape.
        result = check_rate_limit("delete_contact")
        assert result is not None
        assert result["success"] is False
        assert result["error_type"] == "rate_limited"
        assert "delete_contact" in result["error"]
        assert "destructives" in result["error"]

    def test_params_logged_on_rate_limit_deny(
        self, _fresh_rate_limiter: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """On deny, the params dict is appended to the audit log."""
        operation_logger.operations.clear()
        fake_now = [0.0]
        monkeypatch.setattr(
            "apple_contacts_mcp.security.time.monotonic", lambda: fake_now[0]
        )
        for _ in range(TIER_LIMITS["destructives"][0]):
            assert check_rate_limit("delete_contact", params={"k": "v"}) is None
        # Allowed calls should not log.
        assert operation_logger.operations == []
        # Deny logs.
        denied = check_rate_limit("delete_contact", params={"k": "v"})
        assert denied is not None
        assert len(operation_logger.operations) == 1
        entry = operation_logger.operations[0]
        assert entry["operation"] == "delete_contact"
        assert entry["parameters"] == {"k": "v"}
        assert entry["result"] == "rate_limited"


class TestOperationTiersCoverage:
    """Every @mcp.tool() in server.py must have a tier mapping."""

    def test_all_tools_are_mapped(self) -> None:
        import re

        server_src = (
            Path(__file__).resolve().parents[2]
            / "src"
            / "apple_contacts_mcp"
            / "server.py"
        ).read_text()
        # Match `@mcp.tool()\n(async )?def tool_name(`
        tool_re = re.compile(r"@mcp\.tool\(\)\s+(?:async\s+)?def\s+(\w+)\s*\(")
        tools = set(tool_re.findall(server_src))
        unmapped = tools - set(OPERATION_TIERS.keys())
        assert not unmapped, (
            f"OPERATION_TIERS is missing entries for: {sorted(unmapped)}. "
            f"Add them to security.py before the tool ships."
        )

    def test_no_phantom_tier_mappings(self) -> None:
        """OPERATION_TIERS shouldn't list ops that don't exist."""
        import re

        server_src = (
            Path(__file__).resolve().parents[2]
            / "src"
            / "apple_contacts_mcp"
            / "server.py"
        ).read_text()
        tool_re = re.compile(r"@mcp\.tool\(\)\s+(?:async\s+)?def\s+(\w+)\s*\(")
        tools = set(tool_re.findall(server_src))
        phantom = set(OPERATION_TIERS.keys()) - tools
        assert not phantom, (
            f"OPERATION_TIERS has entries for nonexistent tools: {sorted(phantom)}"
        )

    def test_all_destructive_ops_in_tier_map(self) -> None:
        """Anything in DESTRUCTIVE_OPERATIONS (test-mode gate) should also
        have a rate-limit tier."""
        unmapped = DESTRUCTIVE_OPERATIONS - set(OPERATION_TIERS.keys())
        assert not unmapped, (
            f"DESTRUCTIVE_OPERATIONS contains ops without a rate-limit "
            f"tier mapping: {sorted(unmapped)}"
        )


# ---------------------------------------------------------------------------
# Issue #47: OperationLogger
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=False)
def _fresh_operation_logger() -> Any:
    """Clear the audit log between cases. Singleton state would otherwise
    bleed across tests."""
    operation_logger.operations.clear()
    yield
    operation_logger.operations.clear()


class TestOperationLogger:
    """Direct tests against the OperationLogger class."""

    def test_log_operation_appends_entry(
        self, _fresh_operation_logger: Any
    ) -> None:
        logger = OperationLogger()
        logger.log_operation("test_op", {"k": "v"}, "success")
        assert len(logger.operations) == 1
        entry = logger.operations[0]
        assert entry["operation"] == "test_op"
        assert entry["parameters"] == {"k": "v"}
        assert entry["result"] == "success"
        # ISO-formatted timestamp string
        assert isinstance(entry["timestamp"], str)
        assert "T" in entry["timestamp"]

    def test_default_result_is_success(
        self, _fresh_operation_logger: Any
    ) -> None:
        logger = OperationLogger()
        logger.log_operation("op", {})
        assert logger.operations[0]["result"] == "success"

    def test_get_recent_operations_returns_last_n_in_order(
        self, _fresh_operation_logger: Any
    ) -> None:
        logger = OperationLogger()
        for i in range(20):
            logger.log_operation(f"op_{i}", {})
        recent = logger.get_recent_operations(limit=5)
        assert len(recent) == 5
        assert [e["operation"] for e in recent] == [
            "op_15",
            "op_16",
            "op_17",
            "op_18",
            "op_19",
        ]

    def test_get_recent_operations_default_limit_10(
        self, _fresh_operation_logger: Any
    ) -> None:
        logger = OperationLogger()
        for i in range(15):
            logger.log_operation(f"op_{i}", {})
        assert len(logger.get_recent_operations()) == 10

    def test_singleton_is_shared(self, _fresh_operation_logger: Any) -> None:
        """`operation_logger` is a module-level instance reused everywhere."""
        operation_logger.log_operation("op_a", {"a": 1})
        operation_logger.log_operation("op_b", {"b": 2})
        assert len(operation_logger.operations) == 2


class TestRateLimitDenyIsLogged:
    """check_rate_limit auto-logs `rate_limited` on deny."""

    @pytest.fixture(autouse=True)
    def _isolate(self, monkeypatch: pytest.MonkeyPatch) -> Any:
        rate_limiter.reset()
        operation_logger.operations.clear()
        fake_now = [0.0]
        monkeypatch.setattr(
            "apple_contacts_mcp.security.time.monotonic", lambda: fake_now[0]
        )
        yield
        operation_logger.operations.clear()
        rate_limiter.reset()

    def test_allow_path_does_not_log(self) -> None:
        assert check_rate_limit("list_contacts") is None
        assert operation_logger.operations == []

    def test_deny_appends_rate_limited_entry(self) -> None:
        for _ in range(TIER_LIMITS["destructives"][0]):
            assert check_rate_limit("delete_contact") is None
        # 6th call denies
        denied = check_rate_limit("delete_contact")
        assert denied is not None
        assert len(operation_logger.operations) == 1
        entry = operation_logger.operations[0]
        assert entry["operation"] == "delete_contact"
        assert entry["result"] == "rate_limited"

    def test_unmapped_operation_does_not_log(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Unknown ops pass through (fail open) and don't log a deny."""
        with caplog.at_level("WARNING"):
            assert check_rate_limit("never_heard_of_it") is None
        assert operation_logger.operations == []


class TestSafetyViolationIsLogged:
    """check_test_mode_safety + require_test_mode_for deny paths log
    `safety_violation` via _safety_error."""

    @pytest.fixture(autouse=True)
    def _isolate(self) -> Any:
        operation_logger.operations.clear()
        _get_test_group_identifiers.cache_clear()
        yield
        operation_logger.operations.clear()

    def test_missing_test_group_logs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CONTACTS_TEST_MODE", "true")
        monkeypatch.delenv("CONTACTS_TEST_GROUP", raising=False)
        err = check_test_mode_safety("create_contact", group="X")
        assert err is not None
        assert err["error_type"] == "safety_violation"
        assert len(operation_logger.operations) == 1
        assert operation_logger.operations[0]["result"] == "safety_violation"
        assert operation_logger.operations[0]["operation"] == "create_contact"

    def test_group_mismatch_logs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CONTACTS_TEST_MODE", "true")
        monkeypatch.setenv("CONTACTS_TEST_GROUP", "MCP-Test")
        with patch(
            "apple_contacts_mcp.security._get_test_group_identifiers",
            return_value=frozenset({"MCP-Test"}),
        ):
            err = check_test_mode_safety(
                "create_contact", group="WrongGroup"
            )
        assert err is not None
        assert err["error_type"] == "safety_violation"
        assert len(operation_logger.operations) == 1
        assert operation_logger.operations[0]["result"] == "safety_violation"

    def test_require_test_mode_for_outside_test_mode_logs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CONTACTS_TEST_MODE", raising=False)
        err = require_test_mode_for("delete_contact")
        assert err is not None
        assert err["error_type"] == "safety_violation"
        assert len(operation_logger.operations) == 1
        assert operation_logger.operations[0]["operation"] == "delete_contact"
        assert operation_logger.operations[0]["result"] == "safety_violation"

    def test_allowed_safety_check_does_not_log(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CONTACTS_TEST_MODE", raising=False)
        assert check_test_mode_safety("create_contact", group="X") is None
        assert operation_logger.operations == []


class TestConfirmDestructiveCancelledIsLogged:
    """_confirm_destructive logs `cancelled` on user decline / cancel /
    explicit-no, but not on accepted-yes."""

    @pytest.fixture(autouse=True)
    def _isolate(self) -> Any:
        operation_logger.operations.clear()
        yield
        operation_logger.operations.clear()

    @pytest.mark.asyncio
    async def test_declined_logs_cancelled(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        from fastmcp.server.elicitation import DeclinedElicitation

        from apple_contacts_mcp.security import _confirm_destructive

        ctx = MagicMock()
        ctx.elicit = AsyncMock(return_value=DeclinedElicitation())
        result = await _confirm_destructive(
            ctx,
            operation="delete_contact",
            entity_kind="contact",
            identifier="abc:ABPerson",
            preview_lookup=lambda: {"id": "abc:ABPerson", "given_name": "X"},
            describe=lambda p: "X",
        )
        assert result is not None
        assert result["error_type"] == "user_declined"
        assert len(operation_logger.operations) == 1
        entry = operation_logger.operations[0]
        assert entry["operation"] == "delete_contact"
        assert entry["result"] == "cancelled"
        assert entry["parameters"]["entity_kind"] == "contact"
        assert entry["parameters"]["identifier"] == "abc:ABPerson"
        assert entry["parameters"]["action"] == "declined"

    @pytest.mark.asyncio
    async def test_cancelled_logs_cancelled(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        from fastmcp.server.elicitation import CancelledElicitation

        from apple_contacts_mcp.security import _confirm_destructive

        ctx = MagicMock()
        ctx.elicit = AsyncMock(return_value=CancelledElicitation())
        await _confirm_destructive(
            ctx,
            operation="delete_group",
            entity_kind="group",
            identifier="grp-1",
            preview_lookup=lambda: {"id": "grp-1", "name": "X"},
            describe=lambda p: "X",
        )
        assert len(operation_logger.operations) == 1
        assert operation_logger.operations[0]["result"] == "cancelled"
        assert operation_logger.operations[0]["parameters"]["action"] == "cancelled"

    @pytest.mark.asyncio
    async def test_accepted_yes_does_not_log(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        from fastmcp.server.elicitation import AcceptedElicitation

        from apple_contacts_mcp.security import _confirm_destructive

        ctx = MagicMock()
        ctx.elicit = AsyncMock(
            return_value=AcceptedElicitation(data="Yes, delete")
        )
        result = await _confirm_destructive(
            ctx,
            operation="delete_contact",
            entity_kind="contact",
            identifier="abc",
            preview_lookup=lambda: {"id": "abc"},
            describe=lambda p: "abc",
        )
        assert result is None
        assert operation_logger.operations == []

    @pytest.mark.asyncio
    async def test_elicit_unsupported_logs_safety_violation(self) -> None:
        """When the client raises on elicit, the function falls through to
        _safety_error which logs safety_violation (not cancelled)."""
        from unittest.mock import AsyncMock, MagicMock

        from apple_contacts_mcp.security import _confirm_destructive

        ctx = MagicMock()
        ctx.elicit = AsyncMock(side_effect=RuntimeError("no elicit support"))
        result = await _confirm_destructive(
            ctx,
            operation="delete_contact",
            entity_kind="contact",
            identifier="abc",
            preview_lookup=lambda: {"id": "abc"},
            describe=lambda p: "abc",
        )
        assert result is not None
        assert result["error_type"] == "safety_violation"
        assert len(operation_logger.operations) == 1
        assert operation_logger.operations[0]["result"] == "safety_violation"
