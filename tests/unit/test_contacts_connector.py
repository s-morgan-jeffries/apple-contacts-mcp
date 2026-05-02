"""Unit tests for ContactsConnector mock boundary.

These tests mock at the `_run_applescript` and `_run_cn_*` boundaries — never
import PyObjC, never invoke `osascript`. Integration coverage of the real
boundaries lands in tests/integration/ (issue #15).
"""

from __future__ import annotations

import subprocess
import sys
import threading
import types
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from apple_contacts_mcp.contacts_connector import ContactsConnector
from apple_contacts_mcp.exceptions import (
    ContactsAppleScriptError,
    ContactsAuthorizationError,
    ContactsError,
    ContactsTimeoutError,
)


def test_new_exception_hierarchy() -> None:
    assert issubclass(ContactsAppleScriptError, ContactsError)
    assert issubclass(ContactsTimeoutError, ContactsError)


# ---------------------------------------------------------------------------
# _run_applescript
# ---------------------------------------------------------------------------


def test_run_applescript_returns_stripped_stdout() -> None:
    connector = ContactsConnector()
    fake_result = subprocess.CompletedProcess(
        args=["/usr/bin/osascript", "-"],
        returncode=0,
        stdout="hello\n",
        stderr="",
    )
    with patch("subprocess.run", return_value=fake_result) as mock_run:
        out = connector._run_applescript('return "hello"')
    assert out == "hello"
    args, kwargs = mock_run.call_args
    assert args[0] == ["/usr/bin/osascript", "-"]
    assert kwargs["input"] == 'return "hello"'
    assert kwargs["text"] is True
    assert kwargs["capture_output"] is True
    assert kwargs["timeout"] == connector.timeout


def test_run_applescript_nonzero_exit_raises_applescript_error() -> None:
    connector = ContactsConnector()
    fake_result = subprocess.CompletedProcess(
        args=["/usr/bin/osascript", "-"],
        returncode=1,
        stdout="",
        stderr="syntax error: bad thing",
    )
    with patch("subprocess.run", return_value=fake_result):
        with pytest.raises(ContactsAppleScriptError) as exc_info:
            connector._run_applescript("garbage")
    assert "syntax error: bad thing" in str(exc_info.value)


def test_run_applescript_timeout_raises_timeout_error() -> None:
    connector = ContactsConnector(timeout=0.5)

    def boom(*_args: Any, **_kwargs: Any) -> None:
        raise subprocess.TimeoutExpired(cmd=["/usr/bin/osascript", "-"], timeout=0.5)

    with patch("subprocess.run", side_effect=boom):
        with pytest.raises(ContactsTimeoutError) as exc_info:
            connector._run_applescript("delay 99")
    assert "0.5" in str(exc_info.value)


def test_run_applescript_uses_default_timeout_of_10s() -> None:
    connector = ContactsConnector()
    assert connector.timeout == 10.0


# ---------------------------------------------------------------------------
# _get_store
# ---------------------------------------------------------------------------


def _install_fake_contacts_module(
    monkeypatch: pytest.MonkeyPatch, store_factory: MagicMock | None = None
) -> tuple[MagicMock, types.ModuleType]:
    """Install a fake `Contacts` module and return (CNContactStore_mock, module)."""
    fake_module = types.ModuleType("Contacts")
    cn_store_class = MagicMock(name="CNContactStore")
    if store_factory is not None:
        cn_store_class.alloc.return_value.init.return_value = store_factory
    fake_module.CNContactStore = cn_store_class  # type: ignore[attr-defined]
    fake_module.CNEntityTypeContacts = 0  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "Contacts", fake_module)
    return cn_store_class, fake_module


def test_get_store_caches_single_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_store = MagicMock(name="store_instance")
    cn_store_class, _ = _install_fake_contacts_module(monkeypatch, store_factory=fake_store)
    connector = ContactsConnector()
    first = connector._get_store()
    second = connector._get_store()
    assert first is second is fake_store
    assert cn_store_class.alloc.return_value.init.call_count == 1


# ---------------------------------------------------------------------------
# _run_cn_request_access
# ---------------------------------------------------------------------------


def _make_store_with_immediate_callback(
    granted: bool, error: object | None
) -> MagicMock:
    """A fake store whose requestAccess... invokes its callback synchronously."""
    store = MagicMock(name="store_instance")

    def request_access(_entity_type: int, callback: Any) -> None:
        callback(granted, error)

    store.requestAccessForEntityType_completionHandler_.side_effect = request_access
    return store


def test_run_cn_request_access_returns_true_when_granted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _make_store_with_immediate_callback(granted=True, error=None)
    _install_fake_contacts_module(monkeypatch, store_factory=store)
    connector = ContactsConnector()
    assert connector._run_cn_request_access() is True
    store.requestAccessForEntityType_completionHandler_.assert_called_once()


def test_run_cn_request_access_returns_false_when_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _make_store_with_immediate_callback(granted=False, error=None)
    _install_fake_contacts_module(monkeypatch, store_factory=store)
    connector = ContactsConnector()
    assert connector._run_cn_request_access() is False


def test_run_cn_request_access_raises_when_error_returned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_error = MagicMock(name="NSError")
    fake_error.__str__.return_value = "TCC denied"
    store = _make_store_with_immediate_callback(granted=False, error=fake_error)
    _install_fake_contacts_module(monkeypatch, store_factory=store)
    connector = ContactsConnector()
    with pytest.raises(ContactsAuthorizationError) as exc_info:
        connector._run_cn_request_access()
    assert "TCC denied" in str(exc_info.value)


def test_run_cn_request_access_times_out_when_callback_never_fires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MagicMock(name="store_instance")

    def never_call(*_args: Any, **_kwargs: Any) -> None:
        pass

    store.requestAccessForEntityType_completionHandler_.side_effect = never_call
    _install_fake_contacts_module(monkeypatch, store_factory=store)
    connector = ContactsConnector(timeout=0.05)
    with pytest.raises(ContactsTimeoutError):
        connector._run_cn_request_access()


def test_run_cn_request_access_callback_from_other_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CN completion handler runs on a background thread in real life;
    confirm the threading.Event bridge handles that correctly."""
    store = MagicMock(name="store_instance")

    def request_access_async(_entity_type: int, callback: Any) -> None:
        threading.Thread(target=callback, args=(True, None), daemon=True).start()

    store.requestAccessForEntityType_completionHandler_.side_effect = request_access_async
    _install_fake_contacts_module(monkeypatch, store_factory=store)
    connector = ContactsConnector(timeout=2.0)
    assert connector._run_cn_request_access() is True


# ---------------------------------------------------------------------------
# _run_cn_authorization_status
# ---------------------------------------------------------------------------


def _install_contacts_with_status(
    monkeypatch: pytest.MonkeyPatch, raw_status: int
) -> MagicMock:
    """Install a fake `Contacts` module whose CNContactStore.authorizationStatusForEntityType_
    returns the given int. Returns the CNContactStore mock for assertions."""
    fake_module = types.ModuleType("Contacts")
    cn_store_class = MagicMock(name="CNContactStore")
    cn_store_class.authorizationStatusForEntityType_.return_value = raw_status
    fake_module.CNContactStore = cn_store_class  # type: ignore[attr-defined]
    fake_module.CNEntityTypeContacts = 7  # arbitrary; just must round-trip
    monkeypatch.setitem(sys.modules, "Contacts", fake_module)
    return cn_store_class


@pytest.mark.parametrize(
    "raw,expected",
    [
        (0, "notDetermined"),
        (1, "restricted"),
        (2, "denied"),
        (3, "authorized"),
        (4, "limited"),
    ],
)
def test_run_cn_authorization_status_maps_each_known_value(
    raw: int, expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    cn_store_class = _install_contacts_with_status(monkeypatch, raw)
    connector = ContactsConnector()
    assert connector._run_cn_authorization_status() == expected
    cn_store_class.authorizationStatusForEntityType_.assert_called_once_with(7)


def test_run_cn_authorization_status_unknown_value_falls_back_to_not_determined(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _install_contacts_with_status(monkeypatch, 99)
    connector = ContactsConnector()
    with caplog.at_level("WARNING", logger="apple_contacts_mcp.contacts_connector"):
        assert connector._run_cn_authorization_status() == "notDetermined"
    assert any("Unknown CN authorization status" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# _run_cn_enumerate_contacts
# ---------------------------------------------------------------------------


def _make_fake_contact(
    cn_id: str, given: str, family: str, org: str
) -> MagicMock:
    """Build a fake CNContact with the four selectors list_contacts reads."""
    c = MagicMock(name=f"CNContact({cn_id})")
    c.identifier.return_value = cn_id
    c.givenName.return_value = given
    c.familyName.return_value = family
    c.organizationName.return_value = org
    return c


def _install_fake_contacts_for_enumerate(
    monkeypatch: pytest.MonkeyPatch,
    contacts: list[MagicMock],
    enumerate_succeeds: bool = True,
    fake_error: object | None = None,
) -> MagicMock:
    """Install a fake `Contacts` module + a store whose enumerate iterates
    `contacts`, honoring the `stop_ptr[0] = True` short-circuit.

    Returns the CNContactStore mock for assertion of call args.
    """
    fake_module = types.ModuleType("Contacts")
    fake_module.CNContactIdentifierKey = "id_key"  # type: ignore[attr-defined]
    fake_module.CNContactGivenNameKey = "given_key"  # type: ignore[attr-defined]
    fake_module.CNContactFamilyNameKey = "family_key"  # type: ignore[attr-defined]
    fake_module.CNContactOrganizationNameKey = "org_key"  # type: ignore[attr-defined]

    # CNContactFetchRequest.alloc().initWithKeysToFetch_(keys) returns a request stub.
    fetch_request_stub = MagicMock(name="CNContactFetchRequest_instance")
    fetch_request_class = MagicMock(name="CNContactFetchRequest")
    fetch_request_class.alloc.return_value.initWithKeysToFetch_.return_value = (
        fetch_request_stub
    )
    fake_module.CNContactFetchRequest = fetch_request_class  # type: ignore[attr-defined]

    cn_store_class = MagicMock(name="CNContactStore")
    store_instance = MagicMock(name="store_instance")

    def fake_enumerate(
        _req: Any, _err: Any, callback: Any
    ) -> tuple[bool, object | None]:
        if not enumerate_succeeds:
            return False, fake_error
        for contact in contacts:
            stop = [False]
            callback(contact, stop)
            if stop[0]:
                break
        return True, None

    store_instance.enumerateContactsWithFetchRequest_error_usingBlock_.side_effect = (
        fake_enumerate
    )
    cn_store_class.alloc.return_value.init.return_value = store_instance
    fake_module.CNContactStore = cn_store_class  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "Contacts", fake_module)
    return store_instance


def test_enumerate_returns_empty_list_when_store_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_contacts_for_enumerate(monkeypatch, contacts=[])
    connector = ContactsConnector()
    assert connector._run_cn_enumerate_contacts(offset=0, limit=10) == []


def test_enumerate_returns_all_when_under_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fakes = [
        _make_fake_contact(f"id-{i}", f"Given{i}", f"Family{i}", f"Org{i}")
        for i in range(5)
    ]
    _install_fake_contacts_for_enumerate(monkeypatch, contacts=fakes)
    connector = ContactsConnector()
    result = connector._run_cn_enumerate_contacts(offset=0, limit=10)
    assert len(result) == 5
    assert result[0] == {
        "id": "id-0",
        "given_name": "Given0",
        "family_name": "Family0",
        "organization": "Org0",
    }


def test_enumerate_stops_after_limit_via_stop_ptr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fakes = [
        _make_fake_contact(f"id-{i}", f"G{i}", f"F{i}", f"O{i}") for i in range(10)
    ]
    _install_fake_contacts_for_enumerate(monkeypatch, contacts=fakes)
    connector = ContactsConnector()
    result = connector._run_cn_enumerate_contacts(offset=0, limit=3)
    assert [c["id"] for c in result] == ["id-0", "id-1", "id-2"]
    # Confirm later contacts' selectors were never accessed (stop short-circuited).
    assert fakes[3].identifier.call_count == 0


def test_enumerate_skips_offset_then_returns_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fakes = [
        _make_fake_contact(f"id-{i}", f"G{i}", f"F{i}", f"O{i}") for i in range(10)
    ]
    _install_fake_contacts_for_enumerate(monkeypatch, contacts=fakes)
    connector = ContactsConnector()
    result = connector._run_cn_enumerate_contacts(offset=4, limit=3)
    assert [c["id"] for c in result] == ["id-4", "id-5", "id-6"]


def test_enumerate_offset_past_end_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fakes = [
        _make_fake_contact(f"id-{i}", "G", "F", "O") for i in range(5)
    ]
    _install_fake_contacts_for_enumerate(monkeypatch, contacts=fakes)
    connector = ContactsConnector()
    assert connector._run_cn_enumerate_contacts(offset=20, limit=3) == []


def test_enumerate_dict_keys_are_exactly_the_four_basic_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fakes = [_make_fake_contact("id-0", "Alice", "Adams", "Acme")]
    _install_fake_contacts_for_enumerate(monkeypatch, contacts=fakes)
    connector = ContactsConnector()
    [entry] = connector._run_cn_enumerate_contacts(offset=0, limit=1)
    assert set(entry.keys()) == {"id", "given_name", "family_name", "organization"}


def test_enumerate_raises_contacts_error_when_store_returns_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_error = MagicMock(name="NSError")
    fake_error.__str__.return_value = "boom"
    _install_fake_contacts_for_enumerate(
        monkeypatch,
        contacts=[],
        enumerate_succeeds=False,
        fake_error=fake_error,
    )
    connector = ContactsConnector()
    with pytest.raises(ContactsError) as exc_info:
        connector._run_cn_enumerate_contacts(offset=0, limit=10)
    assert "boom" in str(exc_info.value)


# ---------------------------------------------------------------------------
# _run_cn_unified_contact
# ---------------------------------------------------------------------------


_LOCALIZED_LABELS = {
    "_$!<Mobile>!$_": "mobile",
    "_$!<Home>!$_": "home",
    "_$!<Work>!$_": "work",
    "_$!<HomePage>!$_": "homepage",
}


def _make_phone(value: str) -> MagicMock:
    p = MagicMock(name=f"CNPhoneNumber({value})")
    p.stringValue.return_value = value
    return p


def _make_postal(**fields: str) -> MagicMock:
    """Build a fake CNPostalAddress with all 8 selectors."""
    a = MagicMock(name="CNPostalAddress")
    a.street.return_value = fields.get("street", "")
    a.subLocality.return_value = fields.get("sub_locality", "")
    a.city.return_value = fields.get("city", "")
    a.subAdministrativeArea.return_value = fields.get("sub_administrative_area", "")
    a.state.return_value = fields.get("state", "")
    a.postalCode.return_value = fields.get("postal_code", "")
    a.country.return_value = fields.get("country", "")
    a.ISOCountryCode.return_value = fields.get("iso_country_code", "")
    return a


def _make_labeled(label: str | None, value: Any) -> MagicMock:
    lv = MagicMock(name=f"CNLabeledValue({label!r})")
    lv.label.return_value = label
    lv.value.return_value = value
    return lv


def _make_birthday(
    year: int | None = None, month: int | None = None, day: int | None = None
) -> MagicMock:
    """Build a fake NSDateComponents.

    `None` for any component means "set to NSIntegerMax" (the undefined
    sentinel CN actually returns).
    """
    UNDEFINED = 9223372036854775807
    bd = MagicMock(name="NSDateComponents")
    bd.year.return_value = UNDEFINED if year is None else year
    bd.month.return_value = UNDEFINED if month is None else month
    bd.day.return_value = UNDEFINED if day is None else day
    return bd


def _make_full_contact(
    cn_id: str = "ABCD",
    given: str = "Alice",
    family: str = "Adams",
    middle: str = "M",
    prefix: str = "Dr.",
    suffix: str = "Jr.",
    nickname: str = "Ali",
    organization: str = "Acme",
    job: str = "Engineer",
    department: str = "R&D",
    phones: list[MagicMock] | None = None,
    emails: list[MagicMock] | None = None,
    urls: list[MagicMock] | None = None,
    postal: list[MagicMock] | None = None,
    birthday: MagicMock | None = None,
) -> MagicMock:
    c = MagicMock(name=f"CNContact({cn_id})")
    c.identifier.return_value = cn_id
    c.givenName.return_value = given
    c.familyName.return_value = family
    c.middleName.return_value = middle
    c.namePrefix.return_value = prefix
    c.nameSuffix.return_value = suffix
    c.nickname.return_value = nickname
    c.organizationName.return_value = organization
    c.jobTitle.return_value = job
    c.departmentName.return_value = department
    c.phoneNumbers.return_value = phones or []
    c.emailAddresses.return_value = emails or []
    c.urlAddresses.return_value = urls or []
    c.postalAddresses.return_value = postal or []
    c.birthday.return_value = birthday
    return c


def _install_fake_contacts_for_unified(
    monkeypatch: pytest.MonkeyPatch,
    contact: MagicMock | None,
    fake_error: object | None = None,
) -> MagicMock:
    """Install a fake `Contacts` module + a store whose
    unifiedContactWithIdentifier... returns (contact, fake_error).
    """
    fake_module = types.ModuleType("Contacts")
    for k in (
        "CNContactGivenNameKey",
        "CNContactFamilyNameKey",
        "CNContactMiddleNameKey",
        "CNContactNamePrefixKey",
        "CNContactNameSuffixKey",
        "CNContactNicknameKey",
        "CNContactOrganizationNameKey",
        "CNContactJobTitleKey",
        "CNContactDepartmentNameKey",
        "CNContactPhoneNumbersKey",
        "CNContactEmailAddressesKey",
        "CNContactPostalAddressesKey",
        "CNContactUrlAddressesKey",
        "CNContactBirthdayKey",
    ):
        setattr(fake_module, k, k)

    cn_labeled_value = MagicMock(name="CNLabeledValue")
    cn_labeled_value.localizedStringForLabel_.side_effect = (
        lambda raw: _LOCALIZED_LABELS.get(raw, raw)
    )
    fake_module.CNLabeledValue = cn_labeled_value  # type: ignore[attr-defined]

    cn_store_class = MagicMock(name="CNContactStore")
    store_instance = MagicMock(name="store_instance")
    store_instance.unifiedContactWithIdentifier_keysToFetch_error_.return_value = (
        contact,
        fake_error,
    )
    cn_store_class.alloc.return_value.init.return_value = store_instance
    fake_module.CNContactStore = cn_store_class  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "Contacts", fake_module)
    return store_instance


def test_unified_contact_returns_none_when_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_error = MagicMock(name="NSError")
    _install_fake_contacts_for_unified(
        monkeypatch, contact=None, fake_error=fake_error
    )
    connector = ContactsConnector()
    assert connector._run_cn_unified_contact("nonexistent") is None


def test_unified_contact_full_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    contact = _make_full_contact(
        phones=[_make_labeled("_$!<Mobile>!$_", _make_phone("+1 555-1212"))],
        emails=[_make_labeled("_$!<Home>!$_", "alice@example.com")],
        urls=[_make_labeled("_$!<HomePage>!$_", "https://acme.example")],
        postal=[
            _make_labeled(
                "_$!<Work>!$_",
                _make_postal(
                    street="1 Loop", city="Cupertino", state="CA",
                    postal_code="95014", country="USA", iso_country_code="us",
                ),
            )
        ],
        birthday=_make_birthday(year=1990, month=5, day=15),
    )
    store = _install_fake_contacts_for_unified(monkeypatch, contact=contact)
    connector = ContactsConnector()

    result = connector._run_cn_unified_contact("ABCD")

    assert result is not None
    assert result["id"] == "ABCD"
    assert result["given_name"] == "Alice"
    assert result["family_name"] == "Adams"
    assert result["middle_name"] == "M"
    assert result["name_prefix"] == "Dr."
    assert result["name_suffix"] == "Jr."
    assert result["nickname"] == "Ali"
    assert result["organization"] == "Acme"
    assert result["job_title"] == "Engineer"
    assert result["department"] == "R&D"
    assert result["phones"] == [
        {"label_raw": "_$!<Mobile>!$_", "label": "mobile", "value": "+1 555-1212"}
    ]
    assert result["emails"] == [
        {"label_raw": "_$!<Home>!$_", "label": "home", "value": "alice@example.com"}
    ]
    assert result["urls"] == [
        {
            "label_raw": "_$!<HomePage>!$_",
            "label": "homepage",
            "value": "https://acme.example",
        }
    ]
    assert result["postal_addresses"] == [
        {
            "label_raw": "_$!<Work>!$_",
            "label": "work",
            "street": "1 Loop",
            "sub_locality": "",
            "city": "Cupertino",
            "sub_administrative_area": "",
            "state": "CA",
            "postal_code": "95014",
            "country": "USA",
            "iso_country_code": "us",
        }
    ]
    assert result["birthday"] == {"year": 1990, "month": 5, "day": 15}

    store.unifiedContactWithIdentifier_keysToFetch_error_.assert_called_once()
    args, _ = store.unifiedContactWithIdentifier_keysToFetch_error_.call_args
    assert args[0] == "ABCD"
    assert "CNContactGivenNameKey" in args[1]
    assert "CNContactBirthdayKey" in args[1]


def test_unified_contact_empty_multivalued_fields_become_empty_lists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contact = _make_full_contact()  # all defaults; no phones/emails/etc.
    _install_fake_contacts_for_unified(monkeypatch, contact=contact)
    connector = ContactsConnector()

    result = connector._run_cn_unified_contact("ABCD")
    assert result is not None
    assert result["phones"] == []
    assert result["emails"] == []
    assert result["urls"] == []
    assert result["postal_addresses"] == []
    assert result["birthday"] is None


def test_unified_contact_custom_label_passes_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contact = _make_full_contact(
        phones=[_make_labeled("buzzer", _make_phone("+1 555-9999"))]
    )
    _install_fake_contacts_for_unified(monkeypatch, contact=contact)
    connector = ContactsConnector()

    result = connector._run_cn_unified_contact("ABCD")
    assert result is not None
    assert result["phones"] == [
        {"label_raw": "buzzer", "label": "buzzer", "value": "+1 555-9999"}
    ]


def test_unified_contact_none_label_becomes_empty_strings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contact = _make_full_contact(
        emails=[_make_labeled(None, "anon@example.com")]
    )
    _install_fake_contacts_for_unified(monkeypatch, contact=contact)
    connector = ContactsConnector()

    result = connector._run_cn_unified_contact("ABCD")
    assert result is not None
    assert result["emails"] == [
        {"label_raw": "", "label": "", "value": "anon@example.com"}
    ]


def test_unified_contact_birthday_year_undefined(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contact = _make_full_contact(
        birthday=_make_birthday(year=None, month=5, day=15)
    )
    _install_fake_contacts_for_unified(monkeypatch, contact=contact)
    connector = ContactsConnector()

    result = connector._run_cn_unified_contact("ABCD")
    assert result is not None
    assert result["birthday"] == {"month": 5, "day": 15}
    assert "year" not in result["birthday"]


def test_unified_contact_birthday_all_components_zero_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contact = _make_full_contact(birthday=_make_birthday(year=0, month=0, day=0))
    _install_fake_contacts_for_unified(monkeypatch, contact=contact)
    connector = ContactsConnector()

    result = connector._run_cn_unified_contact("ABCD")
    assert result is not None
    assert result["birthday"] is None
