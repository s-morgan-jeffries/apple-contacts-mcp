"""Unit tests for the test-mode safety gate (issue #6).

Mirrors `apple-mail-mcp/tests/unit/test_security.py::TestCheckTestModeSafety`
adapted for the simpler contacts pattern (single gating axis: the test group).
"""

from __future__ import annotations

import subprocess
from typing import Any
from unittest.mock import patch

import pytest

from apple_contacts_mcp.security import (
    DESTRUCTIVE_OPERATIONS,
    _get_test_group_identifiers,
    check_test_mode_safety,
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
