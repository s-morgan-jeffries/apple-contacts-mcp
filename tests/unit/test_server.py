"""Unit tests for @mcp.tool() functions in server.py."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from apple_contacts_mcp.exceptions import (
    ContactsError,
    ContactsNotFoundError,
    ContactsTimeoutError,
)
from apple_contacts_mcp.server import (
    check_authorization,
    create_contact,
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


# ---------------------------------------------------------------------------
# create_contact
# ---------------------------------------------------------------------------


class TestCreateContactValidation:
    def test_all_empty_names_returns_validation_error(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            result = create_contact()
        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        mock_connector._run_cn_authorization_status.assert_not_called()
        mock_connector._run_cn_create_contact.assert_not_called()

    def test_whitespace_only_names_returns_validation_error(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            result = create_contact(given_name="   ", family_name="\t\n")
        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        mock_connector._run_cn_create_contact.assert_not_called()

    def test_email_without_at_returns_validation_error(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            result = create_contact(
                given_name="Alice",
                emails=[{"label_raw": "", "value": "no-at-sign"}],
            )
        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        assert "@" in result["error"]
        mock_connector._run_cn_create_contact.assert_not_called()

    def test_phone_with_empty_value_returns_validation_error(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            result = create_contact(
                given_name="Alice", phones=[{"label_raw": "", "value": ""}]
            )
        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        mock_connector._run_cn_create_contact.assert_not_called()

    def test_url_with_empty_value_returns_validation_error(self) -> None:
        with patch("apple_contacts_mcp.server.connector"):
            result = create_contact(
                given_name="Alice", urls=[{"label_raw": "", "value": ""}]
            )
        assert result["success"] is False
        assert result["error_type"] == "validation_error"

    def test_postal_address_all_empty_returns_validation_error(self) -> None:
        with patch("apple_contacts_mcp.server.connector"):
            result = create_contact(
                given_name="Alice",
                postal_addresses=[{"label_raw": "_$!<Home>!$_"}],
            )
        assert result["success"] is False
        assert result["error_type"] == "validation_error"

    @pytest.mark.parametrize(
        "bday",
        [
            {"month": 0, "day": 1},
            {"month": 13, "day": 1},
            {"month": 5, "day": 0},
            {"month": 5, "day": 32},
            {"year": -1, "month": 5, "day": 15},
        ],
    )
    def test_invalid_birthday_returns_validation_error(
        self, bday: dict[str, int]
    ) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            result = create_contact(given_name="Alice", birthday=bday)
        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        mock_connector._run_cn_create_contact.assert_not_called()


class TestCreateContactAuthFlow:
    def test_auth_denied_passthrough_skips_save(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "denied"
            result = create_contact(given_name="Alice")
        assert result["success"] is False
        assert result["error_type"] == "authorization_denied"
        mock_connector._run_cn_create_contact.assert_not_called()


class TestCreateContactTestModeSafety:
    def test_test_mode_without_group_arg_blocked(
        self, monkeypatch: Any
    ) -> None:
        monkeypatch.setenv("CONTACTS_TEST_MODE", "true")
        monkeypatch.setenv("CONTACTS_TEST_GROUP", "MCP-Test")
        from apple_contacts_mcp.security import _get_test_group_identifiers
        _get_test_group_identifiers.cache_clear()
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            with patch("subprocess.run", side_effect=FileNotFoundError):
                result = create_contact(given_name="Alice")
        assert result["success"] is False
        assert result["error_type"] == "safety_violation"
        mock_connector._run_cn_create_contact.assert_not_called()

    def test_test_mode_with_matching_group_proceeds(
        self, monkeypatch: Any
    ) -> None:
        monkeypatch.setenv("CONTACTS_TEST_MODE", "true")
        monkeypatch.setenv("CONTACTS_TEST_GROUP", "MCP-Test")
        from apple_contacts_mcp.security import _get_test_group_identifiers
        _get_test_group_identifiers.cache_clear()
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_create_contact.return_value = "NEW-ID"
            with patch("subprocess.run", side_effect=FileNotFoundError):
                # Name matching falls back when subprocess fails — name "MCP-Test" matches.
                result = create_contact(
                    given_name="Alice", group_identifier="MCP-Test"
                )
        assert result["success"] is True
        assert result["identifier"] == "NEW-ID"
        assert result["group_id"] == "MCP-Test"


class TestCreateContactHappyPath:
    def test_minimal_returns_identifier_no_group(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_create_contact.return_value = "NEW-ID-1"
            result = create_contact(given_name="Alice")
        assert result == {"success": True, "identifier": "NEW-ID-1"}
        mock_connector._run_cn_create_contact.assert_called_once()
        kwargs = mock_connector._run_cn_create_contact.call_args.kwargs
        assert kwargs["group_identifier"] is None
        assert kwargs["fields"]["given_name"] == "Alice"

    def test_with_group_includes_group_id_in_response(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_create_contact.return_value = "NEW-ID-2"
            result = create_contact(
                given_name="Alice", group_identifier="GROUP-XYZ"
            )
        assert result == {
            "success": True,
            "identifier": "NEW-ID-2",
            "group_id": "GROUP-XYZ",
        }

    def test_full_field_set_passes_through_to_connector(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_create_contact.return_value = "NEW"
            create_contact(
                given_name="Alice",
                family_name="Adams",
                organization="Acme",
                phones=[{"label_raw": "_$!<Mobile>!$_", "value": "+1 555-1212"}],
                emails=[{"label_raw": "", "value": "alice@example.com"}],
                urls=[{"label_raw": "", "value": "https://example.com"}],
                postal_addresses=[
                    {
                        "label_raw": "_$!<Home>!$_",
                        "city": "Cupertino",
                    }
                ],
                birthday={"year": 1990, "month": 5, "day": 15},
            )
        kwargs = mock_connector._run_cn_create_contact.call_args.kwargs
        fields = kwargs["fields"]
        assert fields["given_name"] == "Alice"
        assert fields["family_name"] == "Adams"
        assert fields["organization"] == "Acme"
        assert len(fields["phones"]) == 1
        assert fields["birthday"] == {"year": 1990, "month": 5, "day": 15}


class TestCreateContactNotFound:
    def test_group_not_found_maps_to_not_found(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_create_contact.side_effect = (
                ContactsNotFoundError("Group not found: 'BAD-GROUP'")
            )
            result = create_contact(
                given_name="Alice", group_identifier="BAD-GROUP"
            )
        assert result["success"] is False
        assert result["error_type"] == "not_found"
        assert "BAD-GROUP" in result["error"]


class TestCreateContactSaveFailure:
    def test_cn_save_error_returns_unknown(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_create_contact.side_effect = ContactsError(
                "save boom"
            )
            result = create_contact(given_name="Alice")
        assert result["success"] is False
        assert result["error_type"] == "unknown"
        assert "save boom" in result["error"]
