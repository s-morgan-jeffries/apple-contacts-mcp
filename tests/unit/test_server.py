"""Unit tests for @mcp.tool() functions in server.py."""

from __future__ import annotations

import base64
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apple_contacts_mcp.exceptions import (
    ContactsError,
    ContactsNotFoundError,
    ContactsTimeoutError,
)
from apple_contacts_mcp.security import _get_test_group_identifiers, rate_limiter
from apple_contacts_mcp.server import (
    _verify_authorization_still_granted,
    add_contact_to_group,
    check_authorization,
    create_contact,
    create_group,
    delete_contact,
    delete_group,
    export_vcard,
    get_contact,
    get_contacts_in_group,
    import_vcard,
    list_contacts,
    list_containers,
    list_groups,
    read_note,
    read_photo,
    remove_contact_from_group,
    rename_group,
    search_contacts,
    update_contact,
    write_note,
    write_photo,
)


@pytest.fixture(autouse=True)
def _reset_rate_limiter() -> None:
    """Tools wire `check_rate_limit` (issue #46); the module-level
    `rate_limiter` accumulates across tests. Reset between cases so
    one tier's budget doesn't leak into the next test."""
    rate_limiter.reset()


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
            # Three calls: entry check (notDetermined), post-request re-check,
            # post-call verification (empty result triggers the #37 verifier).
            mock_connector._run_cn_authorization_status.side_effect = [
                "notDetermined",
                "authorized",
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
        mock_connector._run_cn_unified_contact.assert_called_once_with(
            "ABCD", include_niche=False
        )

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


_SEARCH_FIELDS = ("name", "phone", "email", "organization")
_SEARCH_VALUES = {
    "name": "alice",
    "phone": "+15551234567",
    "email": "alice@example.com",
    "organization": "acme",
}


class TestSearchContactsValidation:
    def test_no_fields_set_returns_validation_error(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            result = search_contacts()
        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        assert "name" in result["error"]
        assert "phone" in result["error"]
        assert "email" in result["error"]
        assert "organization" in result["error"]
        mock_connector._run_cn_authorization_status.assert_not_called()
        mock_connector._run_cn_search_contacts.assert_not_called()

    @pytest.mark.parametrize("blank", ["", "   ", "\t", "\n  \t"])
    def test_only_whitespace_treated_as_unset(self, blank: str) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            result = search_contacts(name=blank)
        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        mock_connector._run_cn_search_contacts.assert_not_called()

    @pytest.mark.parametrize(
        "pair",
        [
            ("name", "phone"),
            ("name", "email"),
            ("name", "organization"),
            ("phone", "email"),
            ("phone", "organization"),
            ("email", "organization"),
        ],
    )
    def test_two_fields_set_returns_validation_error(
        self, pair: tuple[str, str]
    ) -> None:
        kwargs = {f: _SEARCH_VALUES[f] for f in pair}
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            result = search_contacts(**kwargs)
        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        for f in pair:
            assert f in result["error"]
        mock_connector._run_cn_search_contacts.assert_not_called()

    def test_all_four_fields_set_returns_validation_error(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            result = search_contacts(**_SEARCH_VALUES)
        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        mock_connector._run_cn_search_contacts.assert_not_called()

    def test_whitespace_on_others_does_not_count_as_set(self) -> None:
        """name='alice' alongside phone='   ' is a single-field call."""
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = (
                "authorized"
            )
            mock_connector._run_cn_search_contacts.return_value = []
            result = search_contacts(
                name="alice", phone="   ", email="\t", organization=""
            )
        assert result["success"] is True
        mock_connector._run_cn_search_contacts.assert_called_once_with(
            field="name", value="alice", limit=200
        )


class TestSearchContactsAuthFlow:
    @pytest.mark.parametrize("field", _SEARCH_FIELDS)
    def test_auth_denied_passthrough_skips_search(self, field: str) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "denied"
            result = search_contacts(**{field: _SEARCH_VALUES[field]})
        assert result["success"] is False
        assert result["error_type"] == "authorization_denied"
        mock_connector._run_cn_search_contacts.assert_not_called()


class TestSearchContactsHappyPath:
    @pytest.mark.parametrize("field", _SEARCH_FIELDS)
    def test_returns_results_with_search_field_and_value_echoed(
        self, field: str
    ) -> None:
        value = _SEARCH_VALUES[field]
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = (
                "authorized"
            )
            mock_connector._run_cn_search_contacts.return_value = (
                _FAKE_SEARCH_HITS
            )
            result = search_contacts(**{field: value})
        assert result == {
            "success": True,
            "contacts": _FAKE_SEARCH_HITS,
            "count": 3,
            "search_field": field,
            "search_value": value,
            "limit": 200,
        }

    def test_response_keys_are_minimal_on_success(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = (
                "authorized"
            )
            mock_connector._run_cn_search_contacts.return_value = (
                _FAKE_SEARCH_HITS
            )
            result = search_contacts(name="john")
        assert set(result.keys()) == {
            "success",
            "contacts",
            "count",
            "search_field",
            "search_value",
            "limit",
        }

    def test_no_matches_returns_empty_list(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = (
                "authorized"
            )
            mock_connector._run_cn_search_contacts.return_value = []
            result = search_contacts(name="zzz-no-match")
        assert result["success"] is True
        assert result["count"] == 0
        assert result["contacts"] == []

    @pytest.mark.parametrize("field", _SEARCH_FIELDS)
    def test_connector_called_with_field_value_and_cap(self, field: str) -> None:
        value = _SEARCH_VALUES[field]
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = (
                "authorized"
            )
            mock_connector._run_cn_search_contacts.return_value = []
            search_contacts(**{field: value})
        mock_connector._run_cn_search_contacts.assert_called_once_with(
            field=field, value=value, limit=200
        )

    def test_search_value_is_stripped(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = (
                "authorized"
            )
            mock_connector._run_cn_search_contacts.return_value = []
            result = search_contacts(name="  alice  ")
        assert result["search_value"] == "alice"
        mock_connector._run_cn_search_contacts.assert_called_once_with(
            field="name", value="alice", limit=200
        )


class TestSearchContactsCapDetection:
    def test_count_equals_limit_when_cap_hit(self) -> None:
        cap_hit = [
            {"id": f"id-{i}", "given_name": "J", "family_name": "S", "organization": ""}
            for i in range(200)
        ]
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = (
                "authorized"
            )
            mock_connector._run_cn_search_contacts.return_value = cap_hit
            result = search_contacts(name="j")
        assert result["count"] == 200
        assert result["limit"] == 200
        assert result["count"] == result["limit"]


class TestSearchContactsConnectorRaises:
    def test_unexpected_exception_returns_unknown_error(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = (
                "authorized"
            )
            mock_connector._run_cn_search_contacts.side_effect = ContactsError(
                "boom"
            )
            result = search_contacts(name="alice")
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
                emails=[{"label": "", "value": "no-at-sign"}],
            )
        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        assert "@" in result["error"]
        mock_connector._run_cn_create_contact.assert_not_called()

    def test_phone_with_empty_value_returns_validation_error(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            result = create_contact(
                given_name="Alice", phones=[{"label": "", "value": ""}]
            )
        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        mock_connector._run_cn_create_contact.assert_not_called()

    def test_url_with_empty_value_returns_validation_error(self) -> None:
        with patch("apple_contacts_mcp.server.connector"):
            result = create_contact(
                given_name="Alice", urls=[{"label": "", "value": ""}]
            )
        assert result["success"] is False
        assert result["error_type"] == "validation_error"

    def test_postal_address_all_empty_returns_validation_error(self) -> None:
        with patch("apple_contacts_mcp.server.connector"):
            result = create_contact(
                given_name="Alice",
                postal_addresses=[{"label": "_$!<Home>!$_"}],
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

    def test_dates_entry_with_no_components_returns_validation_error(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            result = create_contact(
                given_name="Alice", dates=[{"label": "anniversary"}]
            )
        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        assert "year/month/day" in result["error"]
        mock_connector._run_cn_create_contact.assert_not_called()

    @pytest.mark.parametrize(
        "date_entry",
        [
            {"month": 0, "day": 1},
            {"month": 13, "day": 1},
            {"month": 5, "day": 0},
            {"month": 5, "day": 32},
            {"year": -1, "month": 5, "day": 15},
        ],
    )
    def test_dates_invalid_components_returns_validation_error(
        self, date_entry: dict[str, int]
    ) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            result = create_contact(given_name="Alice", dates=[date_entry])
        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        mock_connector._run_cn_create_contact.assert_not_called()

    def test_social_profile_without_username_or_url_returns_validation_error(
        self,
    ) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            result = create_contact(
                given_name="Alice",
                social_profiles=[{"label": "twitter", "service": "Twitter"}],
            )
        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        assert "username/url" in result["error"]
        mock_connector._run_cn_create_contact.assert_not_called()

    def test_relation_without_name_returns_validation_error(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            result = create_contact(
                given_name="Alice",
                relations=[{"label": "spouse"}],
            )
        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        mock_connector._run_cn_create_contact.assert_not_called()

    def test_instant_message_without_username_returns_validation_error(
        self,
    ) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            result = create_contact(
                given_name="Alice",
                instant_messages=[{"label": "slack", "service": "Slack"}],
            )
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
    def test_minimal_returns_identifier_with_null_group_and_container(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_create_contact.return_value = "NEW-ID-1"
            result = create_contact(given_name="Alice")
        assert result == {
            "success": True,
            "identifier": "NEW-ID-1",
            "group_id": None,
            "container_id": None,
        }
        mock_connector._run_cn_create_contact.assert_called_once()
        kwargs = mock_connector._run_cn_create_contact.call_args.kwargs
        assert kwargs["group_identifier"] is None
        assert kwargs["container_identifier"] is None
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
            "container_id": None,
        }

    def test_with_container_includes_container_id_in_response(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_create_contact.return_value = "NEW-ID-3"
            result = create_contact(
                given_name="Alice",
                container_identifier="CONTAINER-ABC:ABAccount",
            )
        assert result == {
            "success": True,
            "identifier": "NEW-ID-3",
            "group_id": None,
            "container_id": "CONTAINER-ABC:ABAccount",
        }
        kwargs = mock_connector._run_cn_create_contact.call_args.kwargs
        assert kwargs["container_identifier"] == "CONTAINER-ABC:ABAccount"

    def test_with_group_and_container_echoes_both(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_create_contact.return_value = "NEW-ID-4"
            result = create_contact(
                given_name="Alice",
                group_identifier="GROUP-XYZ",
                container_identifier="CONTAINER-ABC:ABAccount",
            )
        assert result == {
            "success": True,
            "identifier": "NEW-ID-4",
            "group_id": "GROUP-XYZ",
            "container_id": "CONTAINER-ABC:ABAccount",
        }

    def test_response_keys_are_minimal(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_create_contact.return_value = "NEW"
            result = create_contact(given_name="Alice")
        assert set(result.keys()) == {
            "success",
            "identifier",
            "group_id",
            "container_id",
        }

    def test_niche_fields_pass_through_to_connector(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_create_contact.return_value = "NEW-NICHE"
            result = create_contact(
                given_name="Alice",
                dates=[{"label": "anniversary", "year": 2010, "month": 6, "day": 1}],
                social_profiles=[
                    {"label": "twitter", "username": "alice", "service": "Twitter"}
                ],
                relations=[{"label": "spouse", "name": "Bob"}],
                instant_messages=[
                    {"label": "slack", "username": "alice", "service": "Slack"}
                ],
            )
        assert result["success"] is True
        kwargs = mock_connector._run_cn_create_contact.call_args.kwargs
        fields = kwargs["fields"]
        assert fields["dates"][0]["year"] == 2010
        assert fields["social_profiles"][0]["username"] == "alice"
        assert fields["relations"][0]["name"] == "Bob"
        assert fields["instant_messages"][0]["service"] == "Slack"

    def test_full_field_set_passes_through_to_connector(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_create_contact.return_value = "NEW"
            create_contact(
                given_name="Alice",
                family_name="Adams",
                organization="Acme",
                phones=[{"label": "_$!<Mobile>!$_", "value": "+1 555-1212"}],
                emails=[{"label": "", "value": "alice@example.com"}],
                urls=[{"label": "", "value": "https://example.com"}],
                postal_addresses=[
                    {
                        "label": "_$!<Home>!$_",
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


# ---------------------------------------------------------------------------
# update_contact
# ---------------------------------------------------------------------------


class TestUpdateContactValidation:
    def test_empty_identifier_returns_validation_error(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            result = update_contact(identifier="", given_name="X")
        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        mock_connector._run_cn_authorization_status.assert_not_called()

    def test_no_fields_supplied_returns_validation_error(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            result = update_contact(identifier="ABCD")
        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        assert "at least one" in result["error"].lower()
        mock_connector._run_cn_update_contact.assert_not_called()

    def test_email_without_at_returns_validation_error(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            result = update_contact(
                identifier="ABCD",
                emails=[{"label": "", "value": "no-at-sign"}],
            )
        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        mock_connector._run_cn_update_contact.assert_not_called()

    @pytest.mark.parametrize(
        "bday",
        [
            {"month": 13, "day": 1},
            {"month": 5, "day": 32},
            {"year": -1, "month": 5, "day": 15},
        ],
    )
    def test_invalid_birthday_returns_validation_error(
        self, bday: dict[str, int]
    ) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            result = update_contact(identifier="ABCD", birthday=bday)
        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        mock_connector._run_cn_update_contact.assert_not_called()


class TestUpdateContactAuthFlow:
    def test_auth_denied_passthrough_skips_update(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "denied"
            result = update_contact(identifier="ABCD", given_name="Alice")
        assert result["success"] is False
        assert result["error_type"] == "authorization_denied"
        mock_connector._run_cn_update_contact.assert_not_called()


class TestUpdateContactTestModeSafety:
    def test_test_mode_without_group_arg_blocked(
        self, monkeypatch: Any
    ) -> None:
        monkeypatch.setenv("CONTACTS_TEST_MODE", "true")
        monkeypatch.setenv("CONTACTS_TEST_GROUP", "MCP-Test")
        _get_test_group_identifiers.cache_clear()
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            with patch("subprocess.run", side_effect=FileNotFoundError):
                result = update_contact(identifier="ABCD", given_name="Alice")
        assert result["success"] is False
        assert result["error_type"] == "safety_violation"
        mock_connector._run_cn_update_contact.assert_not_called()

    def test_test_mode_with_matching_group_proceeds(
        self, monkeypatch: Any
    ) -> None:
        monkeypatch.setenv("CONTACTS_TEST_MODE", "true")
        monkeypatch.setenv("CONTACTS_TEST_GROUP", "MCP-Test")
        _get_test_group_identifiers.cache_clear()
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_update_contact.return_value = "ABCD"
            with patch("subprocess.run", side_effect=FileNotFoundError):
                result = update_contact(
                    identifier="ABCD",
                    given_name="Alice",
                    group_identifier="MCP-Test",
                )
        assert result == {"success": True, "identifier": "ABCD"}


class TestUpdateContactHappyPath:
    def test_single_field_passes_only_that_key_to_connector(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_update_contact.return_value = "ABCD"
            result = update_contact(identifier="ABCD", given_name="Alice")
        assert result == {"success": True, "identifier": "ABCD"}
        kwargs = mock_connector._run_cn_update_contact.call_args.kwargs
        assert kwargs["identifier"] == "ABCD"
        assert kwargs["fields"] == {"given_name": "Alice"}

    def test_empty_string_clears_field_via_presence(self) -> None:
        """given_name='' must reach the connector (presence semantics)."""
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_update_contact.return_value = "ABCD"
            update_contact(identifier="ABCD", given_name="")
        kwargs = mock_connector._run_cn_update_contact.call_args.kwargs
        assert kwargs["fields"] == {"given_name": ""}

    def test_phones_empty_list_reaches_connector(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_update_contact.return_value = "ABCD"
            update_contact(identifier="ABCD", phones=[])
        kwargs = mock_connector._run_cn_update_contact.call_args.kwargs
        assert kwargs["fields"] == {"phones": []}

    def test_response_keys_are_minimal(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_update_contact.return_value = "ABCD"
            result = update_contact(identifier="ABCD", given_name="A")
        assert set(result.keys()) == {"success", "identifier"}


class TestUpdateContactErrors:
    def test_not_found_from_connector(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_update_contact.side_effect = (
                ContactsNotFoundError("Contact not found: 'BAD'")
            )
            result = update_contact(identifier="BAD", given_name="X")
        assert result["success"] is False
        assert result["error_type"] == "not_found"
        assert "BAD" in result["error"]

    def test_save_failure_returns_unknown(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_update_contact.side_effect = ContactsError(
                "save boom"
            )
            result = update_contact(identifier="ABCD", given_name="X")
        assert result["success"] is False
        assert result["error_type"] == "unknown"
        assert "save boom" in result["error"]


# ---------------------------------------------------------------------------
# delete_contact
# ---------------------------------------------------------------------------


def _make_ctx_accept(value: str = "Yes, delete") -> MagicMock:
    """Build a fake Context whose elicit() returns AcceptedElicitation(value)."""
    from fastmcp.server.elicitation import AcceptedElicitation

    ctx = MagicMock(name="Context(accept)")
    ctx.elicit = AsyncMock(return_value=AcceptedElicitation(data=value))
    return ctx


def _make_ctx_declined() -> MagicMock:
    from fastmcp.server.elicitation import DeclinedElicitation

    ctx = MagicMock(name="Context(declined)")
    ctx.elicit = AsyncMock(return_value=DeclinedElicitation())
    return ctx


def _make_ctx_cancelled() -> MagicMock:
    from fastmcp.server.elicitation import CancelledElicitation

    ctx = MagicMock(name="Context(cancelled)")
    ctx.elicit = AsyncMock(return_value=CancelledElicitation())
    return ctx


def _make_ctx_unsupported() -> MagicMock:
    """Mock ctx whose elicit raises — simulates a client without elicit support."""
    ctx = MagicMock(name="Context(unsupported)")
    ctx.elicit = AsyncMock(
        side_effect=RuntimeError("client does not support elicitation")
    )
    return ctx


def _fake_contact_for_preview(
    given: str = "Alice", family: str = "Adams"
) -> dict[str, Any]:
    """Minimal contact dict shaped like _run_cn_unified_contact's return."""
    return {
        "id": "ABCD",
        "given_name": given,
        "family_name": family,
        "organization": "",
    }


class TestDeleteContactValidation:
    async def test_empty_identifier_returns_validation_error(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            result = await delete_contact(MagicMock(), identifier="")
        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        mock_connector._run_cn_authorization_status.assert_not_called()


class TestDeleteContactTestModePath:
    """In test mode, the existing group-scope safety gate applies and
    no elicitation happens."""

    async def test_no_group_arg_returns_safety_violation(
        self, monkeypatch: Any
    ) -> None:
        monkeypatch.setenv("CONTACTS_TEST_MODE", "true")
        monkeypatch.setenv("CONTACTS_TEST_GROUP", "MCP-Test")
        _get_test_group_identifiers.cache_clear()
        ctx = MagicMock()
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            with patch("subprocess.run", side_effect=FileNotFoundError):
                result = await delete_contact(ctx, identifier="ABCD")
        assert result["success"] is False
        assert result["error_type"] == "safety_violation"
        mock_connector._run_cn_delete_contact.assert_not_called()
        ctx.elicit.assert_not_called()

    async def test_matching_group_proceeds_without_elicit(
        self, monkeypatch: Any
    ) -> None:
        monkeypatch.setenv("CONTACTS_TEST_MODE", "true")
        monkeypatch.setenv("CONTACTS_TEST_GROUP", "MCP-Test")
        _get_test_group_identifiers.cache_clear()
        ctx = MagicMock()
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_delete_contact.return_value = "ABCD"
            with patch("subprocess.run", side_effect=FileNotFoundError):
                result = await delete_contact(
                    ctx, identifier="ABCD", group_identifier="MCP-Test"
                )
        assert result == {"success": True, "identifier": "ABCD"}
        ctx.elicit.assert_not_called()


class TestDeleteContactConfirmation:
    """Outside test mode, the user must confirm via elicitation."""

    async def test_accept_yes_proceeds_with_delete(self, monkeypatch: Any) -> None:
        monkeypatch.delenv("CONTACTS_TEST_MODE", raising=False)
        ctx = _make_ctx_accept("Yes, delete")
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_unified_contact.return_value = (
                _fake_contact_for_preview()
            )
            mock_connector._run_cn_delete_contact.return_value = "ABCD"
            result = await delete_contact(ctx, identifier="ABCD")
        assert result == {"success": True, "identifier": "ABCD"}
        ctx.elicit.assert_awaited_once()
        mock_connector._run_cn_delete_contact.assert_called_once_with(
            identifier="ABCD"
        )

    async def test_accept_no_returns_user_declined(
        self, monkeypatch: Any
    ) -> None:
        monkeypatch.delenv("CONTACTS_TEST_MODE", raising=False)
        ctx = _make_ctx_accept("No, cancel")
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_unified_contact.return_value = (
                _fake_contact_for_preview()
            )
            result = await delete_contact(ctx, identifier="ABCD")
        assert result["success"] is False
        assert result["error_type"] == "user_declined"
        mock_connector._run_cn_delete_contact.assert_not_called()

    async def test_declined_returns_user_declined(self, monkeypatch: Any) -> None:
        monkeypatch.delenv("CONTACTS_TEST_MODE", raising=False)
        ctx = _make_ctx_declined()
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_unified_contact.return_value = (
                _fake_contact_for_preview()
            )
            result = await delete_contact(ctx, identifier="ABCD")
        assert result["success"] is False
        assert result["error_type"] == "user_declined"
        mock_connector._run_cn_delete_contact.assert_not_called()

    async def test_cancelled_returns_user_declined(
        self, monkeypatch: Any
    ) -> None:
        monkeypatch.delenv("CONTACTS_TEST_MODE", raising=False)
        ctx = _make_ctx_cancelled()
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_unified_contact.return_value = (
                _fake_contact_for_preview()
            )
            result = await delete_contact(ctx, identifier="ABCD")
        assert result["success"] is False
        assert result["error_type"] == "user_declined"
        mock_connector._run_cn_delete_contact.assert_not_called()

    async def test_elicit_unsupported_returns_safety_violation(
        self, monkeypatch: Any
    ) -> None:
        monkeypatch.delenv("CONTACTS_TEST_MODE", raising=False)
        ctx = _make_ctx_unsupported()
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_unified_contact.return_value = (
                _fake_contact_for_preview()
            )
            result = await delete_contact(ctx, identifier="ABCD")
        assert result["success"] is False
        assert result["error_type"] == "safety_violation"
        assert "CONTACTS_TEST_MODE" in result["error"]
        mock_connector._run_cn_delete_contact.assert_not_called()

    async def test_missing_contact_returns_not_found_without_prompting(
        self, monkeypatch: Any
    ) -> None:
        """Pre-fetch returns None → not_found, elicit never called."""
        monkeypatch.delenv("CONTACTS_TEST_MODE", raising=False)
        ctx = _make_ctx_accept()
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_unified_contact.return_value = None
            result = await delete_contact(ctx, identifier="MISSING")
        assert result["success"] is False
        assert result["error_type"] == "not_found"
        ctx.elicit.assert_not_called()
        mock_connector._run_cn_delete_contact.assert_not_called()


class TestDeleteContactErrors:
    async def test_not_found_from_connector(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("CONTACTS_TEST_MODE", "true")
        monkeypatch.setenv("CONTACTS_TEST_GROUP", "MCP-Test")
        _get_test_group_identifiers.cache_clear()
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_delete_contact.side_effect = (
                ContactsNotFoundError("Contact not found: 'BAD'")
            )
            with patch("subprocess.run", side_effect=FileNotFoundError):
                result = await delete_contact(
                    MagicMock(), identifier="BAD", group_identifier="MCP-Test"
                )
        assert result["success"] is False
        assert result["error_type"] == "not_found"

    async def test_save_failure_returns_unknown(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("CONTACTS_TEST_MODE", "true")
        monkeypatch.setenv("CONTACTS_TEST_GROUP", "MCP-Test")
        _get_test_group_identifiers.cache_clear()
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_delete_contact.side_effect = ContactsError(
                "delete boom"
            )
            with patch("subprocess.run", side_effect=FileNotFoundError):
                result = await delete_contact(
                    MagicMock(), identifier="ABCD", group_identifier="MCP-Test"
                )
        assert result["success"] is False
        assert result["error_type"] == "unknown"
        assert "delete boom" in result["error"]


# ---------------------------------------------------------------------------
# read_note
# ---------------------------------------------------------------------------


class TestReadNoteValidation:
    @pytest.mark.parametrize("identifier", ["", "   ", "\t"])
    def test_blank_identifier_returns_validation_error(
        self, identifier: str
    ) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            result = read_note(identifier)
        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        mock_connector._run_applescript_read_note.assert_not_called()


class TestReadNoteAuthFlow:
    def test_auth_denied_passthrough_skips_read(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "denied"
            result = read_note("ABCD")
        assert result["success"] is False
        assert result["error_type"] == "authorization_denied"
        mock_connector._run_applescript_read_note.assert_not_called()


class TestReadNoteHappyPath:
    def test_returns_note_text_with_identifier_echoed(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_applescript_read_note.return_value = "hello world"
            result = read_note("ABCD-1234:ABPerson")
        assert result == {
            "success": True,
            "identifier": "ABCD-1234:ABPerson",
            "note": "hello world",
        }
        mock_connector._run_applescript_read_note.assert_called_once_with(
            "ABCD-1234:ABPerson"
        )

    def test_empty_note_round_trips(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_applescript_read_note.return_value = ""
            result = read_note("ABCD")
        assert result == {"success": True, "identifier": "ABCD", "note": ""}

    def test_response_keys_are_minimal(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_applescript_read_note.return_value = "x"
            result = read_note("ABCD")
        assert set(result.keys()) == {"success", "identifier", "note"}


class TestReadNoteErrors:
    def test_not_found_from_connector(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_applescript_read_note.side_effect = (
                ContactsNotFoundError("Contact not found: 'BAD'")
            )
            result = read_note("BAD")
        assert result["success"] is False
        assert result["error_type"] == "not_found"
        assert "BAD" in result["error"]

    def test_unexpected_exception_returns_unknown(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_applescript_read_note.side_effect = ContactsError(
                "boom"
            )
            result = read_note("ABCD")
        assert result["success"] is False
        assert result["error_type"] == "unknown"
        assert "boom" in result["error"]


# ---------------------------------------------------------------------------
# write_note
# ---------------------------------------------------------------------------


class TestWriteNoteValidation:
    @pytest.mark.parametrize("identifier", ["", "   ", "\t"])
    def test_blank_identifier_returns_validation_error(
        self, identifier: str
    ) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            result = write_note(identifier, "x")
        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        mock_connector._run_applescript_write_note.assert_not_called()


class TestWriteNoteAuthFlow:
    def test_auth_denied_passthrough_skips_write(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "denied"
            result = write_note("ABCD", "x")
        assert result["success"] is False
        assert result["error_type"] == "authorization_denied"
        mock_connector._run_applescript_write_note.assert_not_called()


class TestWriteNoteTestModeSafety:
    def test_test_mode_without_group_arg_blocked(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("CONTACTS_TEST_MODE", "true")
        monkeypatch.setenv("CONTACTS_TEST_GROUP", "MCP-Test")
        _get_test_group_identifiers.cache_clear()
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            with patch("subprocess.run", side_effect=FileNotFoundError):
                result = write_note("ABCD", "x")
        assert result["success"] is False
        assert result["error_type"] == "safety_violation"
        mock_connector._run_applescript_write_note.assert_not_called()

    def test_test_mode_with_matching_group_proceeds(
        self, monkeypatch: Any
    ) -> None:
        monkeypatch.setenv("CONTACTS_TEST_MODE", "true")
        monkeypatch.setenv("CONTACTS_TEST_GROUP", "MCP-Test")
        _get_test_group_identifiers.cache_clear()
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_applescript_write_note.return_value = None
            with patch("subprocess.run", side_effect=FileNotFoundError):
                result = write_note(
                    "ABCD", "hello", group_identifier="MCP-Test"
                )
        assert result == {"success": True, "identifier": "ABCD"}


class TestWriteNoteHappyPath:
    def test_passes_identifier_and_note_to_connector(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_applescript_write_note.return_value = None
            result = write_note("ABCD", "hello world")
        assert result == {"success": True, "identifier": "ABCD"}
        mock_connector._run_applescript_write_note.assert_called_once_with(
            "ABCD", "hello world"
        )

    def test_empty_note_clears_via_connector(self) -> None:
        """note='' is the legitimate clear value — must reach the connector."""
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_applescript_write_note.return_value = None
            result = write_note("ABCD", "")
        assert result == {"success": True, "identifier": "ABCD"}
        mock_connector._run_applescript_write_note.assert_called_once_with(
            "ABCD", ""
        )

    def test_response_keys_are_minimal(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_applescript_write_note.return_value = None
            result = write_note("ABCD", "x")
        assert set(result.keys()) == {"success", "identifier"}


class TestWriteNoteErrors:
    def test_not_found_from_connector(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_applescript_write_note.side_effect = (
                ContactsNotFoundError("Contact not found: 'BAD'")
            )
            result = write_note("BAD", "x")
        assert result["success"] is False
        assert result["error_type"] == "not_found"
        assert "BAD" in result["error"]

    def test_unexpected_exception_returns_unknown(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_applescript_write_note.side_effect = (
                ContactsError("boom")
            )
            result = write_note("ABCD", "x")
        assert result["success"] is False
        assert result["error_type"] == "unknown"
        assert "boom" in result["error"]


# ---------------------------------------------------------------------------
# list_groups
# ---------------------------------------------------------------------------


_FAKE_GROUPS = [
    {"id": "G1", "name": "Family", "container_id": "C1"},
    {"id": "G2", "name": "Work", "container_id": "C1"},
]


class TestListGroupsAuthFlow:
    def test_auth_denied_passthrough_skips_list(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "denied"
            result = list_groups()
        assert result["success"] is False
        assert result["error_type"] == "authorization_denied"
        mock_connector._run_cn_list_groups.assert_not_called()


class TestListGroupsHappyPath:
    def test_returns_groups_with_count_and_limit(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_list_groups.return_value = _FAKE_GROUPS
            result = list_groups()
        assert result == {
            "success": True,
            "groups": _FAKE_GROUPS,
            "count": 2,
            "limit": 200,
        }

    def test_empty_store_returns_empty_list(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_list_groups.return_value = []
            result = list_groups()
        assert result["success"] is True
        assert result["groups"] == []
        assert result["count"] == 0

    def test_response_keys_are_minimal(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_list_groups.return_value = _FAKE_GROUPS
            result = list_groups()
        assert set(result.keys()) == {"success", "groups", "count", "limit"}


class TestListGroupsCapDetection:
    def test_count_equals_limit_when_cap_hit(self) -> None:
        cap_hit = [
            {"id": f"G{i}", "name": f"Group {i}", "container_id": "C1"}
            for i in range(250)
        ]
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_list_groups.return_value = cap_hit
            result = list_groups()
        assert result["count"] == 200
        assert result["limit"] == 200
        assert len(result["groups"]) == 200


class TestListGroupsErrors:
    def test_unexpected_exception_returns_unknown(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_list_groups.side_effect = ContactsError(
                "boom"
            )
            result = list_groups()
        assert result["success"] is False
        assert result["error_type"] == "unknown"
        assert "boom" in result["error"]


# ---------------------------------------------------------------------------
# list_containers
# ---------------------------------------------------------------------------


_FAKE_CONTAINERS = [
    {
        "id": "C-ICLOUD:ABAccount",
        "name": "iCloud",
        "type": "cardDAV",
        "is_default": True,
    },
    {
        "id": "C-GMAIL:ABAccount",
        "name": "Gmail",
        "type": "cardDAV",
        "is_default": False,
    },
]


class TestListContainersAuthFlow:
    def test_auth_denied_passthrough_skips_list(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "denied"
            result = list_containers()
        assert result["success"] is False
        assert result["error_type"] == "authorization_denied"
        mock_connector._run_cn_list_containers.assert_not_called()


class TestListContainersHappyPath:
    def test_returns_containers_with_count_and_limit(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_list_containers.return_value = _FAKE_CONTAINERS
            result = list_containers()
        assert result == {
            "success": True,
            "containers": _FAKE_CONTAINERS,
            "count": 2,
            "limit": 10,
        }

    def test_default_flag_surfaces_per_container(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_list_containers.return_value = _FAKE_CONTAINERS
            result = list_containers()
        defaults = [c for c in result["containers"] if c["is_default"]]
        assert len(defaults) == 1
        assert defaults[0]["name"] == "iCloud"

    def test_empty_store_returns_empty_list(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_list_containers.return_value = []
            result = list_containers()
        assert result["success"] is True
        assert result["containers"] == []
        assert result["count"] == 0

    def test_response_keys_are_minimal(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_list_containers.return_value = _FAKE_CONTAINERS
            result = list_containers()
        assert set(result.keys()) == {"success", "containers", "count", "limit"}


class TestListContainersCapDetection:
    def test_count_equals_limit_when_cap_hit(self) -> None:
        cap_hit = [
            {
                "id": f"C{i}:ABAccount",
                "name": f"Container {i}",
                "type": "cardDAV",
                "is_default": i == 0,
            }
            for i in range(15)
        ]
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_list_containers.return_value = cap_hit
            result = list_containers()
        assert result["count"] == 10
        assert result["limit"] == 10
        assert len(result["containers"]) == 10


class TestListContainersErrors:
    def test_unexpected_exception_returns_unknown(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_list_containers.side_effect = ContactsError(
                "boom"
            )
            result = list_containers()
        assert result["success"] is False
        assert result["error_type"] == "unknown"
        assert "boom" in result["error"]


# ---------------------------------------------------------------------------
# get_contacts_in_group
# ---------------------------------------------------------------------------


class TestGetContactsInGroupValidation:
    @pytest.mark.parametrize("identifier", ["", "   ", "\t"])
    def test_blank_identifier_returns_validation_error(
        self, identifier: str
    ) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            result = get_contacts_in_group(identifier)
        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        mock_connector._run_cn_fetch_group.assert_not_called()
        mock_connector._run_cn_contacts_in_group.assert_not_called()


class TestGetContactsInGroupAuthFlow:
    def test_auth_denied_passthrough_skips_lookup(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "denied"
            result = get_contacts_in_group("ABCD")
        assert result["success"] is False
        assert result["error_type"] == "authorization_denied"
        mock_connector._run_cn_fetch_group.assert_not_called()


class TestGetContactsInGroupNotFound:
    def test_unknown_group_returns_not_found(self) -> None:
        """Pre-flight via _run_cn_fetch_group → None ⇒ not_found."""
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_fetch_group.return_value = None
            result = get_contacts_in_group("BAD-GROUP")
        assert result["success"] is False
        assert result["error_type"] == "not_found"
        assert "BAD-GROUP" in result["error"]
        mock_connector._run_cn_contacts_in_group.assert_not_called()


class TestGetContactsInGroupHappyPath:
    def test_returns_contacts_with_group_identifier_echoed(self) -> None:
        members = [
            {"id": "id-0", "given_name": "A", "family_name": "B", "organization": "C"},
        ]
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_fetch_group.return_value = "GROUP-OBJ"
            mock_connector._run_cn_contacts_in_group.return_value = members
            result = get_contacts_in_group("MY-GROUP")
        assert result == {
            "success": True,
            "group_identifier": "MY-GROUP",
            "contacts": members,
            "count": 1,
            "limit": 200,
        }
        mock_connector._run_cn_contacts_in_group.assert_called_once_with(
            "MY-GROUP", 200
        )

    def test_empty_group_returns_empty_list(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_fetch_group.return_value = "GROUP-OBJ"
            mock_connector._run_cn_contacts_in_group.return_value = []
            result = get_contacts_in_group("EMPTY")
        assert result["success"] is True
        assert result["count"] == 0
        assert result["contacts"] == []


class TestGetContactsInGroupCapDetection:
    def test_count_equals_limit_when_cap_hit(self) -> None:
        cap_hit = [
            {"id": f"id-{i}", "given_name": "x", "family_name": "y", "organization": ""}
            for i in range(200)
        ]
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_fetch_group.return_value = "GROUP-OBJ"
            mock_connector._run_cn_contacts_in_group.return_value = cap_hit
            result = get_contacts_in_group("BIG")
        assert result["count"] == 200
        assert result["limit"] == 200


class TestGetContactsInGroupErrors:
    def test_unexpected_exception_returns_unknown(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_fetch_group.return_value = "GROUP-OBJ"
            mock_connector._run_cn_contacts_in_group.side_effect = (
                ContactsError("members-boom")
            )
            result = get_contacts_in_group("ABCD")
        assert result["success"] is False
        assert result["error_type"] == "unknown"
        assert "members-boom" in result["error"]


# ---------------------------------------------------------------------------
# add_contact_to_group
# ---------------------------------------------------------------------------


class TestAddContactToGroupValidation:
    @pytest.mark.parametrize("contact_identifier", ["", "   ", "\t"])
    def test_blank_contact_identifier_returns_validation_error(
        self, contact_identifier: str
    ) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            result = add_contact_to_group(contact_identifier, "GROUP")
        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        assert "contact_identifier" in result["error"]
        mock_connector._run_cn_add_contact_to_group.assert_not_called()

    @pytest.mark.parametrize("group_identifier", ["", "   ", "\t"])
    def test_blank_group_identifier_returns_validation_error(
        self, group_identifier: str
    ) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            result = add_contact_to_group("CONTACT", group_identifier)
        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        assert "group_identifier" in result["error"]
        mock_connector._run_cn_add_contact_to_group.assert_not_called()


class TestAddContactToGroupAuthFlow:
    def test_auth_denied_passthrough(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "denied"
            result = add_contact_to_group("CONTACT", "GROUP")
        assert result["success"] is False
        assert result["error_type"] == "authorization_denied"
        mock_connector._run_cn_add_contact_to_group.assert_not_called()


class TestAddContactToGroupTestModeSafety:
    def test_test_mode_with_wrong_group_blocked(
        self, monkeypatch: Any
    ) -> None:
        monkeypatch.setenv("CONTACTS_TEST_MODE", "true")
        monkeypatch.setenv("CONTACTS_TEST_GROUP", "MCP-Test")
        _get_test_group_identifiers.cache_clear()
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            with patch("subprocess.run", side_effect=FileNotFoundError):
                result = add_contact_to_group("CONTACT", "Some-Other-Group")
        assert result["success"] is False
        assert result["error_type"] == "safety_violation"
        mock_connector._run_cn_add_contact_to_group.assert_not_called()

    def test_test_mode_with_matching_group_proceeds(
        self, monkeypatch: Any
    ) -> None:
        monkeypatch.setenv("CONTACTS_TEST_MODE", "true")
        monkeypatch.setenv("CONTACTS_TEST_GROUP", "MCP-Test")
        _get_test_group_identifiers.cache_clear()
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_add_contact_to_group.return_value = None
            with patch("subprocess.run", side_effect=FileNotFoundError):
                result = add_contact_to_group("CONTACT", "MCP-Test")
        assert result == {
            "success": True,
            "contact_identifier": "CONTACT",
            "group_identifier": "MCP-Test",
        }


class TestAddContactToGroupHappyPath:
    def test_returns_both_identifiers(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_add_contact_to_group.return_value = None
            result = add_contact_to_group("CONTACT", "GROUP")
        assert result == {
            "success": True,
            "contact_identifier": "CONTACT",
            "group_identifier": "GROUP",
        }
        mock_connector._run_cn_add_contact_to_group.assert_called_once_with(
            "CONTACT", "GROUP"
        )

    def test_response_keys_are_minimal(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_add_contact_to_group.return_value = None
            result = add_contact_to_group("CONTACT", "GROUP")
        assert set(result.keys()) == {
            "success",
            "contact_identifier",
            "group_identifier",
        }


class TestAddContactToGroupErrors:
    def test_not_found_dispatches_for_missing_contact(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_add_contact_to_group.side_effect = (
                ContactsNotFoundError("Contact not found: 'BAD'")
            )
            result = add_contact_to_group("BAD", "GROUP")
        assert result["success"] is False
        assert result["error_type"] == "not_found"
        assert "Contact not found" in result["error"]

    def test_not_found_dispatches_for_missing_group(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_add_contact_to_group.side_effect = (
                ContactsNotFoundError("Group not found: 'BAD-GROUP'")
            )
            result = add_contact_to_group("CONTACT", "BAD-GROUP")
        assert result["success"] is False
        assert result["error_type"] == "not_found"
        assert "Group not found" in result["error"]

    def test_unexpected_exception_returns_unknown(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_add_contact_to_group.side_effect = (
                ContactsError("add-boom")
            )
            result = add_contact_to_group("CONTACT", "GROUP")
        assert result["success"] is False
        assert result["error_type"] == "unknown"
        assert "add-boom" in result["error"]


# ---------------------------------------------------------------------------
# remove_contact_from_group
# ---------------------------------------------------------------------------


class TestRemoveContactFromGroupValidation:
    @pytest.mark.parametrize("contact_identifier", ["", "   ", "\t"])
    def test_blank_contact_identifier_returns_validation_error(
        self, contact_identifier: str
    ) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            result = remove_contact_from_group(contact_identifier, "GROUP")
        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        assert "contact_identifier" in result["error"]
        mock_connector._run_applescript_remove_contact_from_group.assert_not_called()

    @pytest.mark.parametrize("group_identifier", ["", "   ", "\t"])
    def test_blank_group_identifier_returns_validation_error(
        self, group_identifier: str
    ) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            result = remove_contact_from_group("CONTACT", group_identifier)
        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        assert "group_identifier" in result["error"]
        mock_connector._run_applescript_remove_contact_from_group.assert_not_called()


class TestRemoveContactFromGroupAuthFlow:
    def test_auth_denied_passthrough(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "denied"
            result = remove_contact_from_group("CONTACT", "GROUP")
        assert result["success"] is False
        assert result["error_type"] == "authorization_denied"
        mock_connector._run_applescript_remove_contact_from_group.assert_not_called()


class TestRemoveContactFromGroupTestModeSafety:
    def test_test_mode_with_wrong_group_blocked(
        self, monkeypatch: Any
    ) -> None:
        monkeypatch.setenv("CONTACTS_TEST_MODE", "true")
        monkeypatch.setenv("CONTACTS_TEST_GROUP", "MCP-Test")
        _get_test_group_identifiers.cache_clear()
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            with patch("subprocess.run", side_effect=FileNotFoundError):
                result = remove_contact_from_group("CONTACT", "Some-Other")
        assert result["success"] is False
        assert result["error_type"] == "safety_violation"
        mock_connector._run_applescript_remove_contact_from_group.assert_not_called()


class TestRemoveContactFromGroupHappyPath:
    def test_returns_both_identifiers(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_applescript_remove_contact_from_group.return_value = None
            result = remove_contact_from_group("CONTACT", "GROUP")
        assert result == {
            "success": True,
            "contact_identifier": "CONTACT",
            "group_identifier": "GROUP",
        }
        mock_connector._run_applescript_remove_contact_from_group.assert_called_once_with(
            "CONTACT", "GROUP"
        )


class TestRemoveContactFromGroupErrors:
    def test_not_found_dispatches(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_applescript_remove_contact_from_group.side_effect = (
                ContactsNotFoundError("Group not found: 'BAD'")
            )
            result = remove_contact_from_group("CONTACT", "BAD")
        assert result["success"] is False
        assert result["error_type"] == "not_found"

    def test_unexpected_exception_returns_unknown(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_applescript_remove_contact_from_group.side_effect = (
                ContactsError("remove-boom")
            )
            result = remove_contact_from_group("CONTACT", "GROUP")
        assert result["success"] is False
        assert result["error_type"] == "unknown"
        assert "remove-boom" in result["error"]


# ---------------------------------------------------------------------------
# export_vcard
# ---------------------------------------------------------------------------


class TestExportVcardValidation:
    def test_non_list_returns_validation_error(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            result = export_vcard("not-a-list")  # type: ignore[arg-type]
        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        assert "non-empty list" in result["error"]
        mock_connector._run_cn_export_vcard.assert_not_called()

    def test_empty_list_returns_validation_error(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            result = export_vcard([])
        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        mock_connector._run_cn_export_vcard.assert_not_called()

    def test_non_string_element_returns_validation_error(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            result = export_vcard(["good", 123])  # type: ignore[list-item]
        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        assert "[1]" in result["error"]
        mock_connector._run_cn_export_vcard.assert_not_called()

    def test_blank_string_element_returns_validation_error(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            result = export_vcard(["good", "  "])
        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        mock_connector._run_cn_export_vcard.assert_not_called()


class TestExportVcardAuthFlow:
    def test_auth_denied_passthrough(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "denied"
            result = export_vcard(["id-1"])
        assert result["success"] is False
        assert result["error_type"] == "authorization_denied"
        mock_connector._run_cn_export_vcard.assert_not_called()


class TestExportVcardHappyPath:
    def test_success_response_includes_vcard_count_and_notes(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_export_vcard.return_value = "BEGIN:VCARD\n"
            result = export_vcard(["id-1", "id-2"])
        assert result["success"] is True
        assert result["vcard"] == "BEGIN:VCARD\n"
        assert result["count"] == 2
        assert isinstance(result["notes"], list)
        assert len(result["notes"]) == 2
        assert any("NOTE field" in note for note in result["notes"])
        assert any(
            "X-APPLE-OMIT-YEAR" in note or "year-less" in note.lower()
            for note in result["notes"]
        )
        mock_connector._run_cn_export_vcard.assert_called_once_with(
            ["id-1", "id-2"]
        )

    def test_response_keys_are_minimal(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_export_vcard.return_value = "x"
            result = export_vcard(["id-1"])
        assert set(result.keys()) == {"success", "vcard", "count", "notes"}


class TestExportVcardErrors:
    def test_not_found_dispatches(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_export_vcard.side_effect = (
                ContactsNotFoundError("Contact not found: 'BAD'")
            )
            result = export_vcard(["BAD"])
        assert result["success"] is False
        assert result["error_type"] == "not_found"
        assert "BAD" in result["error"]

    def test_unexpected_exception_returns_unknown(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_export_vcard.side_effect = ContactsError(
                "ser-boom"
            )
            result = export_vcard(["id-1"])
        assert result["success"] is False
        assert result["error_type"] == "unknown"
        assert "ser-boom" in result["error"]


# ---------------------------------------------------------------------------
# import_vcard
# ---------------------------------------------------------------------------


class TestImportVcardValidation:
    @pytest.mark.parametrize("vcard_text", ["", "   ", "\t\n"])
    def test_blank_text_returns_validation_error(
        self, vcard_text: str
    ) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            result = import_vcard(vcard_text)
        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        assert "vcard_text" in result["error"]
        mock_connector._run_cn_import_vcard.assert_not_called()

    def test_non_string_returns_validation_error(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            result = import_vcard(123)  # type: ignore[arg-type]
        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        mock_connector._run_cn_import_vcard.assert_not_called()


class TestImportVcardAuthFlow:
    def test_auth_denied_passthrough(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "denied"
            result = import_vcard("BEGIN:VCARD")
        assert result["success"] is False
        assert result["error_type"] == "authorization_denied"
        mock_connector._run_cn_import_vcard.assert_not_called()


class TestImportVcardTestModeSafety:
    def test_test_mode_without_group_blocked(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("CONTACTS_TEST_MODE", "true")
        monkeypatch.setenv("CONTACTS_TEST_GROUP", "MCP-Test")
        _get_test_group_identifiers.cache_clear()
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            with patch("subprocess.run", side_effect=FileNotFoundError):
                result = import_vcard("BEGIN:VCARD")
        assert result["success"] is False
        assert result["error_type"] == "safety_violation"
        mock_connector._run_cn_import_vcard.assert_not_called()

    def test_test_mode_with_matching_group_proceeds(
        self, monkeypatch: Any
    ) -> None:
        monkeypatch.setenv("CONTACTS_TEST_MODE", "true")
        monkeypatch.setenv("CONTACTS_TEST_GROUP", "MCP-Test")
        _get_test_group_identifiers.cache_clear()
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_import_vcard.return_value = ["NEW-1"]
            with patch("subprocess.run", side_effect=FileNotFoundError):
                result = import_vcard(
                    "BEGIN:VCARD", group_identifier="MCP-Test"
                )
        assert result == {
            "success": True,
            "identifiers": ["NEW-1"],
            "count": 1,
            "group_id": "MCP-Test",
        }


class TestImportVcardHappyPath:
    def test_returns_identifiers_count_and_group(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_import_vcard.return_value = [
                "NEW-1",
                "NEW-2",
            ]
            result = import_vcard("BEGIN:VCARD x2")
        assert result == {
            "success": True,
            "identifiers": ["NEW-1", "NEW-2"],
            "count": 2,
            "group_id": None,
        }
        mock_connector._run_cn_import_vcard.assert_called_once_with(
            "BEGIN:VCARD x2", None
        )

    def test_response_keys_are_minimal(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_import_vcard.return_value = ["NEW"]
            result = import_vcard("BEGIN:VCARD")
        assert set(result.keys()) == {
            "success",
            "identifiers",
            "count",
            "group_id",
        }


class TestImportVcardErrors:
    def test_parse_failure_dispatches_validation_error(self) -> None:
        """Key behavior: malformed vCard input is the caller's fault, not
        an unknown CN error. Error_type = validation_error."""
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_import_vcard.side_effect = ContactsError(
                "vCard parse failed: malformed input"
            )
            result = import_vcard("not a vcard")
        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        assert "vCard parse failed" in result["error"]

    def test_empty_parse_dispatches_validation_error(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_import_vcard.side_effect = ContactsError(
                "No vCards found in input"
            )
            result = import_vcard("BEGIN:WHATEVER")
        assert result["success"] is False
        assert result["error_type"] == "validation_error"

    def test_group_not_found_dispatches_not_found(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_import_vcard.side_effect = (
                ContactsNotFoundError("Group not found: 'BAD'")
            )
            result = import_vcard("BEGIN:VCARD", group_identifier="BAD")
        assert result["success"] is False
        assert result["error_type"] == "not_found"
        assert "BAD" in result["error"]

    def test_save_failure_dispatches_unknown(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_import_vcard.side_effect = ContactsError(
                "CN save failed: disk-full"
            )
            result = import_vcard("BEGIN:VCARD")
        assert result["success"] is False
        assert result["error_type"] == "unknown"
        assert "save failed" in result["error"]


# ---------------------------------------------------------------------------
# read_photo / write_photo
# ---------------------------------------------------------------------------


_JPEG_BYTES = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00fake-jpeg-payload"
_JPEG_B64 = base64.b64encode(_JPEG_BYTES).decode("ascii")


class TestReadPhotoValidation:
    @pytest.mark.parametrize("identifier", ["", "   ", "\t"])
    def test_blank_identifier_returns_validation_error(
        self, identifier: str
    ) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            result = read_photo(identifier=identifier)
        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        mock_connector._run_cn_authorization_status.assert_not_called()


class TestReadPhotoAuthFlow:
    def test_auth_denied_passthrough_skips_read(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "denied"
            result = read_photo(identifier="ABCD")
        assert result["success"] is False
        assert result["error_type"] == "authorization_denied"
        mock_connector._run_cn_read_photo.assert_not_called()


class TestReadPhotoHappyPath:
    def test_returns_base64_encoded_jpeg(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_read_photo.return_value = {
                "available": True,
                "image_data": _JPEG_BYTES,
            }
            result = read_photo(identifier="ABCD")
        assert result == {
            "success": True,
            "identifier": "ABCD",
            "image_data": _JPEG_B64,
            "format": "jpeg",
            "size_bytes": len(_JPEG_BYTES),
        }

    def test_contact_without_photo_returns_null_image_data(self) -> None:
        """Distinct from not_found: contact exists, photo unset."""
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_read_photo.return_value = {
                "available": False,
                "image_data": b"",
            }
            result = read_photo(identifier="ABCD")
        assert result == {
            "success": True,
            "identifier": "ABCD",
            "image_data": None,
            "format": None,
            "size_bytes": 0,
        }

    def test_unknown_format_still_succeeds(self) -> None:
        """Caller gets whatever Apple stored, even if magic bytes don't match
        a known image format. format='unknown' on the response is normal."""
        weird = b"\x00\x01\x02\x03some-weird-format"
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_read_photo.return_value = {
                "available": True,
                "image_data": weird,
            }
            result = read_photo(identifier="ABCD")
        assert result["success"] is True
        assert result["format"] == "unknown"
        assert result["size_bytes"] == len(weird)


class TestReadPhotoNotFound:
    def test_missing_contact_returns_not_found(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_read_photo.return_value = None
            result = read_photo(identifier="BAD")
        assert result["success"] is False
        assert result["error_type"] == "not_found"


class TestReadPhotoErrors:
    def test_unexpected_exception_returns_unknown(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_read_photo.side_effect = ContactsError(
                "boom"
            )
            result = read_photo(identifier="ABCD")
        assert result["success"] is False
        assert result["error_type"] == "unknown"
        assert "boom" in result["error"]


class TestWritePhotoValidation:
    def test_empty_identifier_returns_validation_error(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            result = write_photo(identifier="", image_data=_JPEG_B64)
        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        mock_connector._run_cn_authorization_status.assert_not_called()

    def test_invalid_base64_returns_validation_error(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            result = write_photo(identifier="ABCD", image_data="not-base64!!!")
        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        assert "base64" in result["error"]
        mock_connector._run_cn_authorization_status.assert_not_called()

    def test_non_string_image_data_returns_validation_error(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            # type: ignore[arg-type] — testing runtime guard
            result = write_photo(identifier="ABCD", image_data=123)  # type: ignore[arg-type]
        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        mock_connector._run_cn_authorization_status.assert_not_called()


class TestWritePhotoAuthFlow:
    def test_auth_denied_passthrough_skips_write(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "denied"
            result = write_photo(identifier="ABCD", image_data=_JPEG_B64)
        assert result["success"] is False
        assert result["error_type"] == "authorization_denied"
        mock_connector._run_cn_write_photo.assert_not_called()


class TestWritePhotoTestModeSafety:
    def test_test_mode_without_group_arg_blocked(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("CONTACTS_TEST_MODE", "true")
        monkeypatch.setenv("CONTACTS_TEST_GROUP", "MCP-Test")
        _get_test_group_identifiers.cache_clear()
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            with patch("subprocess.run", side_effect=FileNotFoundError):
                result = write_photo(
                    identifier="ABCD", image_data=_JPEG_B64
                )
        assert result["success"] is False
        assert result["error_type"] == "safety_violation"
        mock_connector._run_cn_write_photo.assert_not_called()


class TestWritePhotoHappyPath:
    def test_writes_decoded_bytes_to_connector(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_write_photo.return_value = "ABCD"
            result = write_photo(identifier="ABCD", image_data=_JPEG_B64)
        assert result == {"success": True, "identifier": "ABCD"}
        kwargs = mock_connector._run_cn_write_photo.call_args.kwargs
        assert kwargs["identifier"] == "ABCD"
        assert kwargs["image_data"] == _JPEG_BYTES

    def test_clear_passes_none_through(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_write_photo.return_value = "ABCD"
            result = write_photo(identifier="ABCD", image_data=None)
        assert result == {"success": True, "identifier": "ABCD"}
        kwargs = mock_connector._run_cn_write_photo.call_args.kwargs
        assert kwargs["image_data"] is None


class TestWritePhotoErrors:
    def test_not_found_from_connector(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_write_photo.side_effect = (
                ContactsNotFoundError("Contact not found: 'BAD'")
            )
            result = write_photo(identifier="BAD", image_data=_JPEG_B64)
        assert result["success"] is False
        assert result["error_type"] == "not_found"

    def test_save_failure_returns_unknown(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_write_photo.side_effect = ContactsError(
                "save boom"
            )
            result = write_photo(identifier="ABCD", image_data=_JPEG_B64)
        assert result["success"] is False
        assert result["error_type"] == "unknown"
        assert "save boom" in result["error"]


# ---------------------------------------------------------------------------
# create_group
# ---------------------------------------------------------------------------


_NEW_GROUP_DICT = {
    "id": "NEW-GRP:ABGroup",
    "name": "MyGroup",
    "container_id": "ICLOUD:ABAccount",
}


class TestCreateGroupValidation:
    @pytest.mark.parametrize("name", ["", "   ", "\t"])
    def test_blank_name_returns_validation_error(self, name: str) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            result = create_group(name=name)
        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        mock_connector._run_cn_authorization_status.assert_not_called()


class TestCreateGroupAuthFlow:
    def test_auth_denied_passthrough_skips_create(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "denied"
            result = create_group(name="X")
        assert result["success"] is False
        assert result["error_type"] == "authorization_denied"
        mock_connector._run_cn_create_group.assert_not_called()


class TestCreateGroupTestModeSafety:
    def test_test_mode_without_group_arg_blocked(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("CONTACTS_TEST_MODE", "true")
        monkeypatch.setenv("CONTACTS_TEST_GROUP", "MCP-Test")
        _get_test_group_identifiers.cache_clear()
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            with patch("subprocess.run", side_effect=FileNotFoundError):
                result = create_group(name="X")
        assert result["success"] is False
        assert result["error_type"] == "safety_violation"
        mock_connector._run_cn_create_group.assert_not_called()

    def test_test_mode_with_matching_group_proceeds(
        self, monkeypatch: Any
    ) -> None:
        monkeypatch.setenv("CONTACTS_TEST_MODE", "true")
        monkeypatch.setenv("CONTACTS_TEST_GROUP", "MCP-Test")
        _get_test_group_identifiers.cache_clear()
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_create_group.return_value = _NEW_GROUP_DICT
            with patch("subprocess.run", side_effect=FileNotFoundError):
                result = create_group(
                    name="X", group_identifier="MCP-Test"
                )
        assert result == {"success": True, "group": _NEW_GROUP_DICT}


class TestCreateGroupHappyPath:
    def test_returns_group_dict_from_connector(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_create_group.return_value = _NEW_GROUP_DICT
            result = create_group(name="MyGroup")
        assert result == {"success": True, "group": _NEW_GROUP_DICT}
        kwargs = mock_connector._run_cn_create_group.call_args.kwargs
        assert kwargs["name"] == "MyGroup"
        assert kwargs["container_identifier"] is None

    def test_passes_container_identifier_through(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_create_group.return_value = _NEW_GROUP_DICT
            create_group(
                name="MyGroup",
                container_identifier="GMAIL:ABAccount",
            )
        kwargs = mock_connector._run_cn_create_group.call_args.kwargs
        assert kwargs["container_identifier"] == "GMAIL:ABAccount"


class TestCreateGroupErrors:
    def test_save_failure_returns_unknown(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_create_group.side_effect = ContactsError(
                "save boom"
            )
            result = create_group(name="X")
        assert result["success"] is False
        assert result["error_type"] == "unknown"
        assert "save boom" in result["error"]


# ---------------------------------------------------------------------------
# rename_group
# ---------------------------------------------------------------------------


_RENAMED_GROUP_DICT = {
    "id": "GRP-1:ABGroup",
    "name": "Renamed",
    "container_id": "ICLOUD:ABAccount",
}


class TestRenameGroupValidation:
    @pytest.mark.parametrize("identifier", ["", "   ", "\t"])
    def test_blank_identifier_returns_validation_error(
        self, identifier: str
    ) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            result = rename_group(identifier=identifier, new_name="X")
        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        mock_connector._run_cn_authorization_status.assert_not_called()

    @pytest.mark.parametrize("new_name", ["", "   ", "\t"])
    def test_blank_new_name_returns_validation_error(
        self, new_name: str
    ) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            result = rename_group(identifier="GRP-1", new_name=new_name)
        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        mock_connector._run_cn_authorization_status.assert_not_called()


class TestRenameGroupAuthFlow:
    def test_auth_denied_passthrough_skips_rename(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "denied"
            result = rename_group(identifier="GRP-1", new_name="X")
        assert result["success"] is False
        assert result["error_type"] == "authorization_denied"
        mock_connector._run_cn_rename_group.assert_not_called()


class TestRenameGroupTestModeSafety:
    def test_test_mode_without_group_arg_blocked(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("CONTACTS_TEST_MODE", "true")
        monkeypatch.setenv("CONTACTS_TEST_GROUP", "MCP-Test")
        _get_test_group_identifiers.cache_clear()
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            with patch("subprocess.run", side_effect=FileNotFoundError):
                result = rename_group(identifier="GRP-1", new_name="X")
        assert result["success"] is False
        assert result["error_type"] == "safety_violation"
        mock_connector._run_cn_rename_group.assert_not_called()

    def test_test_mode_with_matching_group_proceeds(
        self, monkeypatch: Any
    ) -> None:
        monkeypatch.setenv("CONTACTS_TEST_MODE", "true")
        monkeypatch.setenv("CONTACTS_TEST_GROUP", "MCP-Test")
        _get_test_group_identifiers.cache_clear()
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_rename_group.return_value = _RENAMED_GROUP_DICT
            with patch("subprocess.run", side_effect=FileNotFoundError):
                result = rename_group(
                    identifier="GRP-1",
                    new_name="Renamed",
                    group_identifier="MCP-Test",
                )
        assert result == {"success": True, "group": _RENAMED_GROUP_DICT}


class TestRenameGroupErrors:
    def test_not_found_from_connector(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_rename_group.side_effect = (
                ContactsNotFoundError("Group not found: 'BAD'")
            )
            result = rename_group(identifier="BAD", new_name="X")
        assert result["success"] is False
        assert result["error_type"] == "not_found"

    def test_save_failure_returns_unknown(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_rename_group.side_effect = ContactsError(
                "save boom"
            )
            result = rename_group(identifier="GRP-1", new_name="X")
        assert result["success"] is False
        assert result["error_type"] == "unknown"
        assert "save boom" in result["error"]


# ---------------------------------------------------------------------------
# delete_group
# ---------------------------------------------------------------------------


class TestDeleteGroupValidation:
    async def test_empty_identifier_returns_validation_error(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            result = await delete_group(MagicMock(), identifier="")
        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        mock_connector._run_cn_authorization_status.assert_not_called()


class TestDeleteGroupTestModePath:
    async def test_no_group_arg_returns_safety_violation(
        self, monkeypatch: Any
    ) -> None:
        monkeypatch.setenv("CONTACTS_TEST_MODE", "true")
        monkeypatch.setenv("CONTACTS_TEST_GROUP", "MCP-Test")
        _get_test_group_identifiers.cache_clear()
        ctx = MagicMock()
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            with patch("subprocess.run", side_effect=FileNotFoundError):
                result = await delete_group(ctx, identifier="GRP-1")
        assert result["success"] is False
        assert result["error_type"] == "safety_violation"
        mock_connector._run_cn_delete_group.assert_not_called()
        ctx.elicit.assert_not_called()

    async def test_matching_group_proceeds_without_elicit(
        self, monkeypatch: Any
    ) -> None:
        monkeypatch.setenv("CONTACTS_TEST_MODE", "true")
        monkeypatch.setenv("CONTACTS_TEST_GROUP", "MCP-Test")
        _get_test_group_identifiers.cache_clear()
        ctx = MagicMock()
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_delete_group.return_value = "GRP-1"
            with patch("subprocess.run", side_effect=FileNotFoundError):
                result = await delete_group(
                    ctx, identifier="GRP-1", group_identifier="MCP-Test"
                )
        assert result == {"success": True, "identifier": "GRP-1"}
        ctx.elicit.assert_not_called()


class TestDeleteGroupConfirmation:
    """Outside test mode, the user must confirm via elicitation."""

    async def test_accept_yes_proceeds_with_delete(self, monkeypatch: Any) -> None:
        monkeypatch.delenv("CONTACTS_TEST_MODE", raising=False)
        ctx = _make_ctx_accept("Yes, delete")
        fake_group = MagicMock()
        fake_group.name.return_value = "Family"
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_fetch_group.return_value = fake_group
            mock_connector._run_cn_delete_group.return_value = "GRP-1"
            result = await delete_group(ctx, identifier="GRP-1")
        assert result == {"success": True, "identifier": "GRP-1"}
        ctx.elicit.assert_awaited_once()
        mock_connector._run_cn_delete_group.assert_called_once_with(
            identifier="GRP-1"
        )

    async def test_declined_returns_user_declined(self, monkeypatch: Any) -> None:
        monkeypatch.delenv("CONTACTS_TEST_MODE", raising=False)
        ctx = _make_ctx_declined()
        fake_group = MagicMock()
        fake_group.name.return_value = "Family"
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_fetch_group.return_value = fake_group
            result = await delete_group(ctx, identifier="GRP-1")
        assert result["success"] is False
        assert result["error_type"] == "user_declined"
        mock_connector._run_cn_delete_group.assert_not_called()

    async def test_elicit_unsupported_returns_safety_violation(
        self, monkeypatch: Any
    ) -> None:
        monkeypatch.delenv("CONTACTS_TEST_MODE", raising=False)
        ctx = _make_ctx_unsupported()
        fake_group = MagicMock()
        fake_group.name.return_value = "Family"
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_fetch_group.return_value = fake_group
            result = await delete_group(ctx, identifier="GRP-1")
        assert result["success"] is False
        assert result["error_type"] == "safety_violation"
        assert "CONTACTS_TEST_MODE" in result["error"]
        mock_connector._run_cn_delete_group.assert_not_called()

    async def test_missing_group_returns_not_found_without_prompting(
        self, monkeypatch: Any
    ) -> None:
        monkeypatch.delenv("CONTACTS_TEST_MODE", raising=False)
        ctx = _make_ctx_accept()
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_fetch_group.return_value = None
            result = await delete_group(ctx, identifier="MISSING")
        assert result["success"] is False
        assert result["error_type"] == "not_found"
        ctx.elicit.assert_not_called()


class TestDeleteGroupErrors:
    async def test_not_found_from_connector(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("CONTACTS_TEST_MODE", "true")
        monkeypatch.setenv("CONTACTS_TEST_GROUP", "MCP-Test")
        _get_test_group_identifiers.cache_clear()
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_delete_group.side_effect = (
                ContactsNotFoundError("Group not found: 'BAD'")
            )
            with patch("subprocess.run", side_effect=FileNotFoundError):
                result = await delete_group(
                    MagicMock(), identifier="BAD", group_identifier="MCP-Test"
                )
        assert result["success"] is False
        assert result["error_type"] == "not_found"

    async def test_save_failure_returns_unknown(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("CONTACTS_TEST_MODE", "true")
        monkeypatch.setenv("CONTACTS_TEST_GROUP", "MCP-Test")
        _get_test_group_identifiers.cache_clear()
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_delete_group.side_effect = ContactsError(
                "save boom"
            )
            with patch("subprocess.run", side_effect=FileNotFoundError):
                result = await delete_group(
                    MagicMock(), identifier="GRP-1", group_identifier="MCP-Test"
                )
        assert result["success"] is False
        assert result["error_type"] == "unknown"
        assert "save boom" in result["error"]


# ---------------------------------------------------------------------------
# Issue #37: authorization revocation mid-process
# ---------------------------------------------------------------------------


class TestVerifyAuthorizationStillGranted:
    """Direct unit tests for _verify_authorization_still_granted()."""

    def test_authorized_returns_none(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            assert _verify_authorization_still_granted() is None

    def test_limited_returns_none(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "limited"
            assert _verify_authorization_still_granted() is None

    @pytest.mark.parametrize("status", ["denied", "restricted", "notDetermined"])
    def test_revoked_returns_authorization_denied_dict(self, status: str) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = status
            result = _verify_authorization_still_granted()
        assert result is not None
        assert result["success"] is False
        assert result["error_type"] == "authorization_denied"
        assert result["status"] == status
        assert "revoked during the call" in result["error"]
        assert "remediation" in result

    def test_status_call_raises_returns_unknown(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.side_effect = RuntimeError(
                "store fell over"
            )
            result = _verify_authorization_still_granted()
        assert result is not None
        assert result["success"] is False
        assert result["error_type"] == "unknown"
        assert "store fell over" in result["error"]


class TestPostCallRevocationOnReadTools:
    """Empty result + revoked-since-entry → authorization_denied (not empty)."""

    def test_list_contacts_empty_after_revocation_returns_denied(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            # Entry check sees authorized; post-call check sees denied.
            mock_connector._run_cn_authorization_status.side_effect = [
                "authorized",
                "denied",
            ]
            mock_connector._run_cn_enumerate_contacts.return_value = []
            result = list_contacts()
        assert result["success"] is False
        assert result["error_type"] == "authorization_denied"
        assert result["status"] == "denied"

    def test_list_contacts_non_empty_skips_post_call_check(self) -> None:
        """Non-empty results don't pay the post-call check (perf)."""
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_enumerate_contacts.return_value = [
                {"id": "X", "given_name": "Y", "family_name": "Z", "organization": ""}
            ]
            result = list_contacts()
        assert result["success"] is True
        # Only the entry check; no post-call check.
        assert mock_connector._run_cn_authorization_status.call_count == 1

    def test_get_contact_none_after_revocation_returns_denied(self) -> None:
        """None contact + revoked → authorization_denied (not the misleading not_found)."""
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.side_effect = [
                "authorized",
                "denied",
            ]
            mock_connector._run_cn_unified_contact.return_value = None
            result = get_contact("ABCD")
        assert result["success"] is False
        assert result["error_type"] == "authorization_denied"

    def test_get_contact_genuine_not_found_still_returns_not_found(self) -> None:
        """None contact + still authorized → genuine not_found."""
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_unified_contact.return_value = None
            result = get_contact("MISSING")
        assert result["success"] is False
        assert result["error_type"] == "not_found"


class TestPostCallRevocationOnDestructiveTools:
    """Save succeeded + revoked-since-entry → authorization_denied, telling
    the caller the persistence is undefined."""

    def test_create_contact_revoked_after_save_returns_denied(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.side_effect = [
                "authorized",
                "denied",
            ]
            mock_connector._run_cn_create_contact.return_value = "NEW-ID"
            result = create_contact(given_name="Alice")
        assert result["success"] is False
        assert result["error_type"] == "authorization_denied"

    def test_create_contact_still_authorized_after_save_succeeds(self) -> None:
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.return_value = "authorized"
            mock_connector._run_cn_create_contact.return_value = "NEW-ID"
            result = create_contact(given_name="Alice")
        assert result["success"] is True
        assert result["identifier"] == "NEW-ID"

    async def test_delete_contact_revoked_after_save_returns_denied(
        self, monkeypatch: Any
    ) -> None:
        monkeypatch.setenv("CONTACTS_TEST_MODE", "true")
        monkeypatch.setenv("CONTACTS_TEST_GROUP", "MCP-Test")
        _get_test_group_identifiers.cache_clear()
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            mock_connector._run_cn_authorization_status.side_effect = [
                "authorized",
                "denied",
            ]
            mock_connector._run_cn_delete_contact.return_value = "ABCD"
            with patch("subprocess.run", side_effect=FileNotFoundError):
                result = await delete_contact(
                    MagicMock(), identifier="ABCD", group_identifier="MCP-Test"
                )
        assert result["success"] is False
        assert result["error_type"] == "authorization_denied"


# ---------------------------------------------------------------------------
# Issue #46: rate-limit wiring drift guard
# ---------------------------------------------------------------------------


class TestRateLimitWiringPerTool:
    """Every @mcp.tool() must call check_rate_limit with its own name.

    Two checks:

    1. Source-level: scan server.py for every `def <tool>(`-after-`@mcp.tool()`
       and verify the function body contains `check_rate_limit("<tool>")`.
       Catches the drift case where someone adds a new tool but forgets the
       gate.

    2. Runtime: pick one tool from each tier and verify that a mocked
       rate-limit deny short-circuits before the connector is touched.
       Catches the case where the gate is *present* but placed too late.
    """

    @pytest.fixture(autouse=True)
    def _isolate(self) -> None:
        rate_limiter.reset()
        yield
        rate_limiter.reset()

    def test_every_tool_calls_check_rate_limit_with_own_name(self) -> None:
        import re
        from pathlib import Path

        server_src = (
            Path(__file__).resolve().parents[2]
            / "src"
            / "apple_contacts_mcp"
            / "server.py"
        ).read_text()
        # Capture each tool's name and its full body (until the next @mcp.tool
        # or end of file). For each, assert check_rate_limit("<name>") appears.
        tool_decl = re.compile(
            r"@mcp\.tool\(\)\s+(?:async\s+)?def\s+(\w+)\s*\([^)]*\)",
            re.MULTILINE,
        )
        matches = list(tool_decl.finditer(server_src))
        # For each match, the body runs from match.end() to the next match (or EOF)
        missing: list[str] = []
        for i, m in enumerate(matches):
            tool_name = m.group(1)
            body_start = m.end()
            body_end = matches[i + 1].start() if i + 1 < len(matches) else len(
                server_src
            )
            body = server_src[body_start:body_end]
            if f'check_rate_limit("{tool_name}")' not in body:
                missing.append(tool_name)
        assert not missing, (
            f"tools missing check_rate_limit gate or with mismatched op name: "
            f"{missing}"
        )

    def test_list_contacts_short_circuits_when_rate_limited(self) -> None:
        # Fill the cheap_reads tier so the next call denies.
        from apple_contacts_mcp.security import TIER_LIMITS

        max_calls, _ = TIER_LIMITS["cheap_reads"]
        for _ in range(max_calls):
            assert rate_limiter.check("cheap_reads") is True
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            result = list_contacts()
        assert result["success"] is False
        assert result["error_type"] == "rate_limited"
        mock_connector._run_cn_authorization_status.assert_not_called()
        mock_connector._run_cn_enumerate_contacts.assert_not_called()

    async def test_delete_contact_short_circuits_when_rate_limited(self) -> None:
        from apple_contacts_mcp.security import TIER_LIMITS

        max_calls, _ = TIER_LIMITS["destructives"]
        for _ in range(max_calls):
            assert rate_limiter.check("destructives") is True
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            result = await delete_contact(MagicMock(), identifier="ABCD")
        assert result["success"] is False
        assert result["error_type"] == "rate_limited"
        mock_connector._run_cn_authorization_status.assert_not_called()
        mock_connector._run_cn_delete_contact.assert_not_called()

    def test_create_contact_short_circuits_when_rate_limited(self) -> None:
        from apple_contacts_mcp.security import TIER_LIMITS

        max_calls, _ = TIER_LIMITS["expensive_ops"]
        for _ in range(max_calls):
            assert rate_limiter.check("expensive_ops") is True
        with patch("apple_contacts_mcp.server.connector") as mock_connector:
            result = create_contact(given_name="Alice")
        assert result["success"] is False
        assert result["error_type"] == "rate_limited"
        mock_connector._run_cn_authorization_status.assert_not_called()
        mock_connector._run_cn_create_contact.assert_not_called()
