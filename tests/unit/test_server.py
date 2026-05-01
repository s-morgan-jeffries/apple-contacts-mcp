"""Unit tests for @mcp.tool() functions in server.py."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from apple_contacts_mcp.server import check_authorization


class TestCheckAuthorization:
    @pytest.mark.parametrize("status", ["authorized", "limited"])
    def test_granted_status_returns_no_remediation(self, status: str) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = status
            result = check_authorization()
        assert result == {"success": True, "status": status}

    @pytest.mark.parametrize(
        "status,remediation_substr",
        [
            ("notDetermined", "list_contacts"),
            ("denied", "System Settings"),
            ("restricted", "administrator"),
        ],
    )
    def test_ungranted_status_includes_remediation(
        self, status: str, remediation_substr: str
    ) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = status
            result = check_authorization()
        assert result["success"] is True
        assert result["status"] == status
        assert remediation_substr in result["remediation"]

    def test_connector_failure_returns_unknown_error(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.side_effect = RuntimeError(
                "PyObjC import broke"
            )
            result: dict[str, Any] = check_authorization()
        assert result["success"] is False
        assert result["error_type"] == "unknown"
        assert "PyObjC import broke" in result["error"]

    def test_response_keys_are_minimal_on_granted(self) -> None:
        """Granted responses should not carry remediation noise."""
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            result = check_authorization()
        assert "remediation" not in result
        assert set(result.keys()) == {"success", "status"}
