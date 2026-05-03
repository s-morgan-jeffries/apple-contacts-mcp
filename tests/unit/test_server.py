"""Unit tests for @mcp.tool() functions in server.py."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from apple_contacts_mcp.exceptions import ContactsError, ContactsTimeoutError
from apple_contacts_mcp.server import (
    check_authorization,
    get_contact,
    list_contacts,
    search_contacts,
)


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


# ---------------------------------------------------------------------------
# list_contacts
# ---------------------------------------------------------------------------


_FAKE_CONTACTS = [
    {"id": "id-0", "given_name": "Alice", "family_name": "Adams", "organization": "Acme"},
    {"id": "id-1", "given_name": "Bob", "family_name": "Brown", "organization": ""},
]


class TestListContactsValidation:
    def test_negative_offset_returns_validation_error(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            result = list_contacts(offset=-1)
        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        assert "offset" in result["error"]
        mock_connector._run_cn_authorization_status.assert_not_called()
        mock_connector._run_cn_enumerate_contacts.assert_not_called()

    def test_zero_limit_returns_validation_error(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            result = list_contacts(limit=0)
        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        assert "limit" in result["error"]
        mock_connector._run_cn_enumerate_contacts.assert_not_called()


class TestListContactsAuthFlow:
    @pytest.mark.parametrize("status", ["authorized", "limited"])
    def test_granted_status_proceeds_to_fetch(self, status: str) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = status
            mock_connector._run_cn_enumerate_contacts.return_value = _FAKE_CONTACTS
            result = list_contacts()
        assert result["success"] is True
        assert result["contacts"] == _FAKE_CONTACTS
        assert result["count"] == 2
        assert result["offset"] == 0
        assert result["limit"] == 50
        mock_connector._run_cn_enumerate_contacts.assert_called_once_with(
            offset=0, limit=50
        )
        mock_connector._run_cn_request_access.assert_not_called()

    @pytest.mark.parametrize(
        "status,remediation_substr",
        [
            ("denied", "System Settings"),
            ("restricted", "administrator"),
        ],
    )
    def test_ungranted_status_returns_auth_denied(
        self, status: str, remediation_substr: str
    ) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = status
            result = list_contacts()
        assert result["success"] is False
        assert result["error_type"] == "authorization_denied"
        assert result["status"] == status
        assert remediation_substr in result["remediation"]
        mock_connector._run_cn_enumerate_contacts.assert_not_called()
        mock_connector._run_cn_request_access.assert_not_called()

    def test_not_determined_then_granted_proceeds(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.side_effect = [
                "notDetermined",
                "authorized",
            ]
            mock_connector._run_cn_request_access.return_value = True
            mock_connector._run_cn_enumerate_contacts.return_value = []
            result = list_contacts()
        assert result["success"] is True
        mock_connector._run_cn_request_access.assert_called_once()
        mock_connector._run_cn_enumerate_contacts.assert_called_once()

    def test_not_determined_then_denied_returns_auth_denied(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.side_effect = [
                "notDetermined",
                "denied",
            ]
            mock_connector._run_cn_request_access.return_value = False
            result = list_contacts()
        assert result["success"] is False
        assert result["error_type"] == "authorization_denied"
        assert result["status"] == "denied"
        mock_connector._run_cn_enumerate_contacts.assert_not_called()

    def test_request_access_timeout_returns_pending_message(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "notDetermined"
            mock_connector._run_cn_request_access.side_effect = ContactsTimeoutError(
                "no answer"
            )
            result = list_contacts()
        assert result["success"] is False
        assert result["error_type"] == "authorization_denied"
        assert result["status"] == "notDetermined"
        assert "awaiting" in result["error"]
        mock_connector._run_cn_enumerate_contacts.assert_not_called()

    def test_authorization_status_raises_returns_unknown(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.side_effect = RuntimeError(
                "PyObjC broke"
            )
            result = list_contacts()
        assert result["success"] is False
        assert result["error_type"] == "unknown"
        assert "PyObjC broke" in result["error"]


class TestListContactsLimitClamp:
    def test_limit_above_cap_is_clamped_to_200(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_enumerate_contacts.return_value = []
            result = list_contacts(limit=500)
        assert result["limit"] == 200
        mock_connector._run_cn_enumerate_contacts.assert_called_once_with(
            offset=0, limit=200
        )

    def test_limit_below_cap_is_passed_through(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_enumerate_contacts.return_value = []
            result = list_contacts(limit=10)
        assert result["limit"] == 10
        mock_connector._run_cn_enumerate_contacts.assert_called_once_with(
            offset=0, limit=10
        )


class TestListContactsFetchFailure:
    def test_enumerate_raises_returns_unknown_error(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_enumerate_contacts.side_effect = ContactsError(
                "enumerate boom"
            )
            result = list_contacts()
        assert result["success"] is False
        assert result["error_type"] == "unknown"
        assert "enumerate boom" in result["error"]


class TestListContactsResponseShape:
    def test_success_response_has_exact_keys(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_enumerate_contacts.return_value = _FAKE_CONTACTS
            result = list_contacts()
        assert set(result.keys()) == {
            "success",
            "contacts",
            "count",
            "offset",
            "limit",
        }


# ---------------------------------------------------------------------------
# get_contact
# ---------------------------------------------------------------------------


_FAKE_CONTACT_DICT: dict[str, Any] = {
    "id": "ABCD",
    "given_name": "Alice",
    "family_name": "Adams",
    "middle_name": "",
    "name_prefix": "",
    "name_suffix": "",
    "nickname": "",
    "organization": "Acme",
    "job_title": "",
    "department": "",
    "phones": [],
    "emails": [],
    "urls": [],
    "postal_addresses": [],
    "birthday": None,
}


class TestGetContactValidation:
    @pytest.mark.parametrize("identifier", ["", "   ", "\t\n"])
    def test_blank_identifier_returns_validation_error(
        self, identifier: str
    ) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            result = get_contact(identifier)
        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        assert "identifier" in result["error"]
        mock_connector._run_cn_authorization_status.assert_not_called()
        mock_connector._run_cn_unified_contact.assert_not_called()


class TestGetContactAuthFlow:
    def test_auth_denied_passthrough_skips_fetch(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "denied"
            result = get_contact("ABCD")
        assert result["success"] is False
        assert result["error_type"] == "authorization_denied"
        mock_connector._run_cn_unified_contact.assert_not_called()


class TestGetContactFound:
    def test_found_returns_contact_dict(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_unified_contact.return_value = _FAKE_CONTACT_DICT
            result = get_contact("ABCD")
        assert result == {"success": True, "contact": _FAKE_CONTACT_DICT}
        mock_connector._run_cn_unified_contact.assert_called_once_with("ABCD")

    def test_response_keys_are_minimal_on_success(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_unified_contact.return_value = _FAKE_CONTACT_DICT
            result = get_contact("ABCD")
        assert set(result.keys()) == {"success", "contact"}


class TestGetContactNotFound:
    def test_none_from_connector_maps_to_not_found(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_unified_contact.return_value = None
            result = get_contact("ZZZZ")
        assert result["success"] is False
        assert result["error_type"] == "not_found"
        assert "ZZZZ" in result["error"]


class TestGetContactConnectorRaises:
    def test_unexpected_exception_returns_unknown_error(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_unified_contact.side_effect = ContactsError(
                "boom"
            )
            result = get_contact("ABCD")
        assert result["success"] is False
        assert result["error_type"] == "unknown"
        assert "boom" in result["error"]


# ---------------------------------------------------------------------------
# search_contacts
# ---------------------------------------------------------------------------


_FAKE_SEARCH_HITS = [
    {"id": "id-0", "given_name": "John", "family_name": "Smith", "organization": "Acme"},
    {"id": "id-1", "given_name": "Johnny", "family_name": "Walker", "organization": ""},
    {"id": "id-2", "given_name": "John", "family_name": "Doe", "organization": "Foo"},
]


class TestSearchContactsValidation:
    @pytest.mark.parametrize("query", ["", "   ", "\t", "\n  \t"])
    def test_blank_query_returns_validation_error(self, query: str) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            result = search_contacts(query)
        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        assert "query" in result["error"]
        mock_connector._run_cn_authorization_status.assert_not_called()
        mock_connector._run_cn_search_contacts.assert_not_called()


class TestSearchContactsAuthFlow:
    def test_auth_denied_passthrough_skips_search(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "denied"
            result = search_contacts("john")
        assert result["success"] is False
        assert result["error_type"] == "authorization_denied"
        mock_connector._run_cn_search_contacts.assert_not_called()


class TestSearchContactsHappyPath:
    def test_returns_results_with_query_and_limit_echoed(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_search_contacts.return_value = _FAKE_SEARCH_HITS
            result = search_contacts("john")
        assert result == {
            "success": True,
            "contacts": _FAKE_SEARCH_HITS,
            "count": 3,
            "query": "john",
            "limit": 200,
        }

    def test_response_keys_are_minimal_on_success(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_search_contacts.return_value = _FAKE_SEARCH_HITS
            result = search_contacts("john")
        assert set(result.keys()) == {
            "success",
            "contacts",
            "count",
            "query",
            "limit",
        }

    def test_no_matches_returns_empty_list(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_search_contacts.return_value = []
            result = search_contacts("zzz-no-match")
        assert result["success"] is True
        assert result["count"] == 0
        assert result["contacts"] == []

    def test_connector_called_with_query_and_cap(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_search_contacts.return_value = []
            search_contacts("alice")
        mock_connector._run_cn_search_contacts.assert_called_once_with(
            query="alice", limit=200
        )


class TestSearchContactsCapDetection:
    def test_count_equals_limit_when_cap_hit(self) -> None:
        cap_hit = [
            {"id": f"id-{i}", "given_name": "J", "family_name": "S", "organization": ""}
            for i in range(200)
        ]
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_search_contacts.return_value = cap_hit
            result = search_contacts("j")
        assert result["count"] == 200
        assert result["limit"] == 200
        assert result["count"] == result["limit"]


class TestSearchContactsConnectorRaises:
    def test_unexpected_exception_returns_unknown_error(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_search_contacts.side_effect = ContactsError("boom")
            result = search_contacts("alice")
        assert result["success"] is False
        assert result["error_type"] == "unknown"
        assert "boom" in result["error"]
