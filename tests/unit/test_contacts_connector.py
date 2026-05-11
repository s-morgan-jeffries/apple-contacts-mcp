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
    ContactsNotFoundError,
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
# _run_applescript_read_note / _run_applescript_write_note
# ---------------------------------------------------------------------------


def _patch_run_applescript(
    connector: ContactsConnector,
    return_value: str | None = None,
    side_effect: BaseException | None = None,
) -> Any:
    """Patch `_run_applescript` on the given connector and return the patcher."""
    if side_effect is not None:
        return patch.object(
            connector, "_run_applescript", side_effect=side_effect
        )
    return patch.object(
        connector, "_run_applescript", return_value=return_value or ""
    )


def test_read_note_returns_applescript_stdout() -> None:
    connector = ContactsConnector()
    with _patch_run_applescript(connector, return_value="hello world"):
        assert (
            connector._run_applescript_read_note("ABCD-1234:ABPerson")
            == "hello world"
        )


def test_read_note_passes_identifier_through_unchanged() -> None:
    """Empirical: AppleScript's `id of person` IS the `:ABPerson`-suffixed
    form. Stripping breaks lookups. Verified by integration probe; lock the
    invariant here so future refactors can't reintroduce the strip."""
    connector = ContactsConnector()
    with _patch_run_applescript(connector, return_value="") as mock:
        connector._run_applescript_read_note("ABCD-1234:ABPerson")
    (script,) = mock.call_args.args
    assert 'first person whose id is "ABCD-1234:ABPerson"' in script


@pytest.mark.parametrize(
    "applescript_msg",
    [
        # Curly apostrophe — what AppleScript actually emits
        "Contacts got an error: Can’t get person id \"x\". Invalid index. (-1719)",
        # Straight apostrophe — defensive
        "Contacts got an error: Can't get person id \"x\"",
        # "Invalid index" alone (sometimes returned without the Can't-get prefix)
        "execution error: Invalid index. (-1719)",
    ],
)
def test_read_note_maps_not_found_pattern_to_contacts_not_found(
    applescript_msg: str,
) -> None:
    connector = ContactsConnector()
    err = ContactsAppleScriptError(applescript_msg)
    with _patch_run_applescript(connector, side_effect=err):
        with pytest.raises(ContactsNotFoundError) as exc_info:
            connector._run_applescript_read_note("missing-id")
    assert "missing-id" in str(exc_info.value)


def test_read_note_reraises_unrelated_applescript_error() -> None:
    connector = ContactsConnector()
    err = ContactsAppleScriptError("some other failure")
    with _patch_run_applescript(connector, side_effect=err):
        with pytest.raises(ContactsAppleScriptError) as exc_info:
            connector._run_applescript_read_note("ABCD-1234")
    assert "some other failure" in str(exc_info.value)


def test_write_note_invokes_applescript_with_save() -> None:
    connector = ContactsConnector()
    with _patch_run_applescript(connector, return_value="") as mock:
        connector._run_applescript_write_note("ABCD-1234", "hello")
    (script,) = mock.call_args.args
    assert "set note of p to \"hello\"" in script
    assert "\nsave\n" in script.replace("  ", "")  # save is in the script


def test_write_note_escapes_quotes_and_backslashes() -> None:
    connector = ContactsConnector()
    with _patch_run_applescript(connector, return_value="") as mock:
        connector._run_applescript_write_note(
            "ABCD-1234", 'has "quotes" and \\ backslash'
        )
    (script,) = mock.call_args.args
    assert (
        'set note of p to "has \\"quotes\\" and \\\\ backslash"' in script
    )


def test_write_note_empty_string_emits_empty_literal() -> None:
    connector = ContactsConnector()
    with _patch_run_applescript(connector, return_value="") as mock:
        connector._run_applescript_write_note("ABCD-1234", "")
    (script,) = mock.call_args.args
    assert 'set note of p to ""' in script


def test_write_note_passes_identifier_through_unchanged() -> None:
    connector = ContactsConnector()
    with _patch_run_applescript(connector, return_value="") as mock:
        connector._run_applescript_write_note(
            "ABCD-1234:ABPerson", "x"
        )
    (script,) = mock.call_args.args
    assert 'first person whose id is "ABCD-1234:ABPerson"' in script


def test_write_note_escapes_adversarial_identifier() -> None:
    """Defense-in-depth: identifiers containing AppleScript metacharacters
    are escaped before interpolation. Real CN identifiers never contain
    these characters, so this is a safety net for adversarial input."""
    connector = ContactsConnector()
    with _patch_run_applescript(connector, return_value="") as mock:
        connector._run_applescript_write_note('x" & "y', "note")
    (script,) = mock.call_args.args
    assert 'first person whose id is "x\\" & \\"y"' in script


def test_read_note_escapes_adversarial_identifier() -> None:
    connector = ContactsConnector()
    with _patch_run_applescript(connector, return_value="") as mock:
        connector._run_applescript_read_note('x" & "y')
    (script,) = mock.call_args.args
    assert 'first person whose id is "x\\" & \\"y"' in script


def test_write_note_maps_not_found_pattern_to_contacts_not_found() -> None:
    connector = ContactsConnector()
    err = ContactsAppleScriptError(
        "Contacts got an error: Can't get person id"
    )
    with _patch_run_applescript(connector, side_effect=err):
        with pytest.raises(ContactsNotFoundError) as exc_info:
            connector._run_applescript_write_note("missing-id", "x")
    assert "missing-id" in str(exc_info.value)


def test_write_note_reraises_unrelated_applescript_error() -> None:
    connector = ContactsConnector()
    err = ContactsAppleScriptError("permission denied or whatever")
    with _patch_run_applescript(connector, side_effect=err):
        with pytest.raises(ContactsAppleScriptError) as exc_info:
            connector._run_applescript_write_note("ABCD-1234", "x")
    assert "permission denied" in str(exc_info.value)


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


def test_enumerate_handles_none_stop_ptr_without_crashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real PyObjC passes `BOOL *stop` as None for this selector — the
    callback must not raise when trying to short-circuit. We just stop
    serializing and let the framework finish its walk.

    Caught by integration testing (issue #15); regression test added at
    the unit level so it doesn't recur silently.
    """
    fake_module = types.ModuleType("Contacts")
    fake_module.CNContactIdentifierKey = "id_key"  # type: ignore[attr-defined]
    fake_module.CNContactGivenNameKey = "given_key"  # type: ignore[attr-defined]
    fake_module.CNContactFamilyNameKey = "family_key"  # type: ignore[attr-defined]
    fake_module.CNContactOrganizationNameKey = "org_key"  # type: ignore[attr-defined]
    fetch_request_class = MagicMock(name="CNContactFetchRequest")
    fetch_request_class.alloc.return_value.initWithKeysToFetch_.return_value = (
        MagicMock()
    )
    fake_module.CNContactFetchRequest = fetch_request_class  # type: ignore[attr-defined]

    cn_store_class = MagicMock(name="CNContactStore")
    store_instance = MagicMock(name="store_instance")
    fakes = [_make_fake_contact(f"id-{i}", "G", "F", "O") for i in range(5)]

    def fake_enumerate(
        _req: Any, _err: Any, callback: Any
    ) -> tuple[bool, object | None]:
        # Pass None to mimic real PyObjC behavior for this selector.
        for contact in fakes:
            callback(contact, None)
        return True, None

    store_instance.enumerateContactsWithFetchRequest_error_usingBlock_.side_effect = (
        fake_enumerate
    )
    cn_store_class.alloc.return_value.init.return_value = store_instance
    fake_module.CNContactStore = cn_store_class  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "Contacts", fake_module)

    connector = ContactsConnector()
    # limit=2 means after 2 contacts we want to stop, but stop_ptr is None.
    # The helper should still return only 2 (early-return in the callback)
    # and not crash trying to mutate None.
    result = connector._run_cn_enumerate_contacts(offset=0, limit=2)
    assert [c["id"] for c in result] == ["id-0", "id-1"]


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


# ---------------------------------------------------------------------------
# _run_cn_search_contacts
# ---------------------------------------------------------------------------


def _make_basic_contact(
    cn_id: str, given: str = "G", family: str = "F", org: str = "O"
) -> MagicMock:
    """Build a fake CNContact with only the four basic selectors."""
    c = MagicMock(name=f"CNContact({cn_id})")
    c.identifier.return_value = cn_id
    c.givenName.return_value = given
    c.familyName.return_value = family
    c.organizationName.return_value = org
    return c


def _install_fake_contacts_for_search(
    monkeypatch: pytest.MonkeyPatch,
    results: list[MagicMock] | None,
    fake_error: object | None = None,
) -> tuple[MagicMock, MagicMock, MagicMock, MagicMock]:
    """Install fake ``Contacts`` + ``Foundation`` modules + a store whose
    unifiedContactsMatchingPredicate... returns ``(results, fake_error)``.

    Returns ``(CNContact_class_mock, store_instance_mock,
    CNPhoneNumber_class_mock, NSPredicate_class_mock)`` for per-mode
    predicate assertions.
    """
    fake_module = types.ModuleType("Contacts")
    fake_module.CNContactIdentifierKey = "id_key"  # type: ignore[attr-defined]
    fake_module.CNContactGivenNameKey = "given_key"  # type: ignore[attr-defined]
    fake_module.CNContactFamilyNameKey = "family_key"  # type: ignore[attr-defined]
    fake_module.CNContactOrganizationNameKey = "org_key"  # type: ignore[attr-defined]
    fake_module.CNContactPhoneNumbersKey = "phones_key"  # type: ignore[attr-defined]
    fake_module.CNContactEmailAddressesKey = "emails_key"  # type: ignore[attr-defined]

    cn_contact_class = MagicMock(name="CNContact")
    name_pred = MagicMock(name="NamePredicate")
    phone_pred = MagicMock(name="PhonePredicate")
    email_pred = MagicMock(name="EmailPredicate")
    cn_contact_class.predicateForContactsMatchingName_.return_value = name_pred
    cn_contact_class.predicateForContactsMatchingPhoneNumber_.return_value = (
        phone_pred
    )
    cn_contact_class.predicateForContactsMatchingEmailAddress_.return_value = (
        email_pred
    )
    fake_module.CNContact = cn_contact_class  # type: ignore[attr-defined]

    cn_phone_number_class = MagicMock(name="CNPhoneNumber")
    cn_phone_number_class.phoneNumberWithStringValue_.return_value = MagicMock(
        name="CNPhoneNumberInstance"
    )
    fake_module.CNPhoneNumber = cn_phone_number_class  # type: ignore[attr-defined]

    cn_store_class = MagicMock(name="CNContactStore")
    store_instance = MagicMock(name="store_instance")
    store_instance.unifiedContactsMatchingPredicate_keysToFetch_error_.return_value = (
        results,
        fake_error,
    )
    cn_store_class.alloc.return_value.init.return_value = store_instance
    fake_module.CNContactStore = cn_store_class  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "Contacts", fake_module)

    fake_foundation = types.ModuleType("Foundation")
    ns_predicate_class = MagicMock(name="NSPredicate")
    ns_predicate_class.predicateWithFormat_.return_value = MagicMock(
        name="OrgPredicate"
    )
    fake_foundation.NSPredicate = ns_predicate_class  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "Foundation", fake_foundation)

    return cn_contact_class, store_instance, cn_phone_number_class, ns_predicate_class


def test_search_returns_empty_when_no_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_contacts_for_search(monkeypatch, results=[])
    connector = ContactsConnector()
    assert (
        connector._run_cn_search_contacts(field="name", value="nobody", limit=200)
        == []
    )


def test_search_returns_dicts_for_each_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fakes = [
        _make_basic_contact("id-0", "John", "Smith", "Acme"),
        _make_basic_contact("id-1", "Johnny", "Walker", ""),
    ]
    _install_fake_contacts_for_search(monkeypatch, results=fakes)
    connector = ContactsConnector()
    result = connector._run_cn_search_contacts(
        field="name", value="john", limit=200
    )
    assert result == [
        {"id": "id-0", "given_name": "John", "family_name": "Smith", "organization": "Acme"},
        {"id": "id-1", "given_name": "Johnny", "family_name": "Walker", "organization": ""},
    ]


def test_search_caps_results_at_limit_and_skips_serializing_extras(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fakes = [_make_basic_contact(f"id-{i}") for i in range(250)]
    _install_fake_contacts_for_search(monkeypatch, results=fakes)
    connector = ContactsConnector()
    result = connector._run_cn_search_contacts(
        field="name", value="x", limit=200
    )
    assert len(result) == 200
    assert result[0]["id"] == "id-0"
    assert result[-1]["id"] == "id-199"
    # 201st contact's selectors must never have been touched.
    assert fakes[200].identifier.call_count == 0


def test_search_name_calls_name_predicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cn_contact_class, _, cn_phone_number_class, ns_predicate_class = (
        _install_fake_contacts_for_search(monkeypatch, results=[])
    )
    connector = ContactsConnector()
    connector._run_cn_search_contacts(field="name", value="alice", limit=200)
    cn_contact_class.predicateForContactsMatchingName_.assert_called_once_with(
        "alice"
    )
    cn_contact_class.predicateForContactsMatchingPhoneNumber_.assert_not_called()
    cn_contact_class.predicateForContactsMatchingEmailAddress_.assert_not_called()
    cn_phone_number_class.phoneNumberWithStringValue_.assert_not_called()
    ns_predicate_class.predicateWithFormat_.assert_not_called()


def test_search_phone_wraps_value_in_cn_phone_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cn_contact_class, _, cn_phone_number_class, ns_predicate_class = (
        _install_fake_contacts_for_search(monkeypatch, results=[])
    )
    connector = ContactsConnector()
    connector._run_cn_search_contacts(
        field="phone", value="+15551234567", limit=200
    )
    cn_phone_number_class.phoneNumberWithStringValue_.assert_called_once_with(
        "+15551234567"
    )
    wrapped = cn_phone_number_class.phoneNumberWithStringValue_.return_value
    cn_contact_class.predicateForContactsMatchingPhoneNumber_.assert_called_once_with(
        wrapped
    )
    cn_contact_class.predicateForContactsMatchingName_.assert_not_called()
    cn_contact_class.predicateForContactsMatchingEmailAddress_.assert_not_called()
    ns_predicate_class.predicateWithFormat_.assert_not_called()


def test_search_email_calls_email_predicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cn_contact_class, _, cn_phone_number_class, ns_predicate_class = (
        _install_fake_contacts_for_search(monkeypatch, results=[])
    )
    connector = ContactsConnector()
    connector._run_cn_search_contacts(
        field="email", value="alice@example.com", limit=200
    )
    cn_contact_class.predicateForContactsMatchingEmailAddress_.assert_called_once_with(
        "alice@example.com"
    )
    cn_contact_class.predicateForContactsMatchingName_.assert_not_called()
    cn_contact_class.predicateForContactsMatchingPhoneNumber_.assert_not_called()
    cn_phone_number_class.phoneNumberWithStringValue_.assert_not_called()
    ns_predicate_class.predicateWithFormat_.assert_not_called()


def test_search_organization_uses_contains_cd_nspredicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cn_contact_class, _, cn_phone_number_class, ns_predicate_class = (
        _install_fake_contacts_for_search(monkeypatch, results=[])
    )
    connector = ContactsConnector()
    connector._run_cn_search_contacts(
        field="organization", value="acme", limit=200
    )
    ns_predicate_class.predicateWithFormat_.assert_called_once_with(
        "organizationName CONTAINS[cd] %@", "acme"
    )
    cn_contact_class.predicateForContactsMatchingName_.assert_not_called()
    cn_contact_class.predicateForContactsMatchingPhoneNumber_.assert_not_called()
    cn_contact_class.predicateForContactsMatchingEmailAddress_.assert_not_called()
    cn_phone_number_class.phoneNumberWithStringValue_.assert_not_called()


def test_search_unknown_field_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_contacts_for_search(monkeypatch, results=[])
    connector = ContactsConnector()
    with pytest.raises(ContactsError):
        connector._run_cn_search_contacts(
            field="bogus",  # type: ignore[arg-type]
            value="x",
            limit=200,
        )


@pytest.mark.parametrize(
    ("field", "value", "required_key"),
    [
        ("phone", "+15551234567", "phones_key"),
        ("email", "alice@example.com", "emails_key"),
    ],
)
def test_search_includes_matched_field_in_keys_to_fetch(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
    required_key: str,
) -> None:
    """Apple's phone predicate silently returns zero results unless
    CNContactPhoneNumbersKey is in keysToFetch. We empirically guard the
    same invariant for email even though it currently matches without."""
    _, store_instance, _, _ = _install_fake_contacts_for_search(
        monkeypatch, results=[]
    )
    connector = ContactsConnector()
    connector._run_cn_search_contacts(field=field, value=value, limit=200)  # type: ignore[arg-type]
    (
        _pred,
        keys,
        _err,
    ) = store_instance.unifiedContactsMatchingPredicate_keysToFetch_error_.call_args.args
    assert required_key in keys


def test_search_name_keys_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Name search doesn't need any extra keys — the basic 4 are enough."""
    _, store_instance, _, _ = _install_fake_contacts_for_search(
        monkeypatch, results=[]
    )
    connector = ContactsConnector()
    connector._run_cn_search_contacts(field="name", value="alice", limit=200)
    (
        _pred,
        keys,
        _err,
    ) = store_instance.unifiedContactsMatchingPredicate_keysToFetch_error_.call_args.args
    assert "phones_key" not in keys
    assert "emails_key" not in keys


def test_search_dict_keys_are_exactly_the_four_basic_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_contacts_for_search(
        monkeypatch, results=[_make_basic_contact("id-0", "A", "B", "C")]
    )
    connector = ContactsConnector()
    [entry] = connector._run_cn_search_contacts(
        field="name", value="a", limit=200
    )
    assert set(entry.keys()) == {"id", "given_name", "family_name", "organization"}


def test_search_raises_contacts_error_when_results_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_error = MagicMock(name="NSError")
    fake_error.__str__.return_value = "boom"
    _install_fake_contacts_for_search(
        monkeypatch, results=None, fake_error=fake_error
    )
    connector = ContactsConnector()
    with pytest.raises(ContactsError) as exc_info:
        connector._run_cn_search_contacts(field="name", value="x", limit=200)
    assert "boom" in str(exc_info.value)


# ---------------------------------------------------------------------------
# _run_cn_fetch_group + _run_cn_create_contact
# ---------------------------------------------------------------------------


def _install_fake_contacts_for_create(
    monkeypatch: pytest.MonkeyPatch,
    group_results: list[Any] | None = None,
    save_succeeds: bool = True,
    fake_save_error: Any | None = None,
    new_identifier: str = "NEW-ID-1234",
) -> tuple[MagicMock, MagicMock, MagicMock, MagicMock]:
    """Install fake Contacts + Foundation modules + a store that:
      - groupsMatchingPredicate_error_ returns (group_results, None) (or
        (None, error) if group_results is None to simulate error path).
      - executeSaveRequest_error_ returns (save_succeeds, fake_save_error).
      - CNMutableContact.alloc().init() returns a MagicMock whose
        .identifier() returns `new_identifier` (CN populates it post-save).

    Returns (CNContactStore_class, store_instance, CNMutableContact_class,
    CNSaveRequest_instance) for assertions.
    """
    fake_contacts = types.ModuleType("Contacts")

    cn_mutable_contact_class = MagicMock(name="CNMutableContact")
    mutable_instance = MagicMock(name="mutable_contact_instance")
    mutable_instance.identifier.return_value = new_identifier
    cn_mutable_contact_class.alloc.return_value.init.return_value = mutable_instance
    fake_contacts.CNMutableContact = cn_mutable_contact_class  # type: ignore[attr-defined]

    cn_postal_class = MagicMock(name="CNMutablePostalAddress")
    postal_instance = MagicMock(name="postal_instance")
    cn_postal_class.alloc.return_value.init.return_value = postal_instance
    fake_contacts.CNMutablePostalAddress = cn_postal_class  # type: ignore[attr-defined]

    cn_phone_class = MagicMock(name="CNPhoneNumber")
    cn_phone_class.phoneNumberWithStringValue_.side_effect = (
        lambda v: f"PhoneNumber({v})"
    )
    fake_contacts.CNPhoneNumber = cn_phone_class  # type: ignore[attr-defined]

    cn_labeled_class = MagicMock(name="CNLabeledValue")
    cn_labeled_class.labeledValueWithLabel_value_.side_effect = (
        lambda lbl, val: ("labeled", lbl, val)
    )
    fake_contacts.CNLabeledValue = cn_labeled_class  # type: ignore[attr-defined]

    cn_save_request_class = MagicMock(name="CNSaveRequest")
    save_request_instance = MagicMock(name="save_request_instance")
    cn_save_request_class.alloc.return_value.init.return_value = save_request_instance
    fake_contacts.CNSaveRequest = cn_save_request_class  # type: ignore[attr-defined]

    cn_group_class = MagicMock(name="CNGroup")
    cn_group_class.predicateForGroupsWithIdentifiers_.return_value = "GROUP_PRED"
    fake_contacts.CNGroup = cn_group_class  # type: ignore[attr-defined]

    cn_store_class = MagicMock(name="CNContactStore")
    store_instance = MagicMock(name="store_instance")
    if group_results is None:
        store_instance.groupsMatchingPredicate_error_.return_value = (
            None,
            MagicMock(name="NSError(group_fetch)"),
        )
    else:
        store_instance.groupsMatchingPredicate_error_.return_value = (
            group_results,
            None,
        )
    store_instance.executeSaveRequest_error_.return_value = (
        save_succeeds,
        fake_save_error,
    )
    cn_store_class.alloc.return_value.init.return_value = store_instance
    fake_contacts.CNContactStore = cn_store_class  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "Contacts", fake_contacts)

    fake_foundation = types.ModuleType("Foundation")
    nsdc_class = MagicMock(name="NSDateComponents")
    nsdc_instance = MagicMock(name="nsdc_instance")
    nsdc_class.alloc.return_value.init.return_value = nsdc_instance
    fake_foundation.NSDateComponents = nsdc_class  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "Foundation", fake_foundation)

    return cn_store_class, store_instance, cn_mutable_contact_class, save_request_instance


# ----- _run_cn_fetch_group ----------------------------------------------------


def test_fetch_group_returns_group_when_found(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_group = MagicMock(name="CNGroup_instance")
    _install_fake_contacts_for_create(monkeypatch, group_results=[fake_group])
    connector = ContactsConnector()
    assert connector._run_cn_fetch_group("group-id") is fake_group


def test_fetch_group_returns_none_when_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_contacts_for_create(monkeypatch, group_results=[])
    connector = ContactsConnector()
    assert connector._run_cn_fetch_group("missing") is None


def test_fetch_group_raises_on_cn_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_contacts_for_create(monkeypatch, group_results=None)
    connector = ContactsConnector()
    with pytest.raises(ContactsError):
        connector._run_cn_fetch_group("any")


# ----- _run_cn_create_contact -------------------------------------------------


def test_create_contact_minimal_returns_new_identifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, store, _, save_req = _install_fake_contacts_for_create(
        monkeypatch, new_identifier="NEW-ID-1"
    )
    connector = ContactsConnector()
    result = connector._run_cn_create_contact(
        fields={"given_name": "Alice"}, group_identifier=None
    )
    assert result == "NEW-ID-1"
    save_req.addContact_toContainerWithIdentifier_.assert_called_once()
    args, _ = save_req.addContact_toContainerWithIdentifier_.call_args
    assert args[1] is None  # default container
    save_req.addMember_toGroup_.assert_not_called()
    store.executeSaveRequest_error_.assert_called_once()


def test_create_contact_with_explicit_container_passes_uuid_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, _, save_req = _install_fake_contacts_for_create(monkeypatch)
    connector = ContactsConnector()
    connector._run_cn_create_contact(
        fields={"given_name": "Alice"},
        group_identifier=None,
        container_identifier="GMAIL-UUID:ABAccount",
    )
    save_req.addContact_toContainerWithIdentifier_.assert_called_once()
    args, _ = save_req.addContact_toContainerWithIdentifier_.call_args
    assert args[1] == "GMAIL-UUID:ABAccount"


def test_create_contact_sets_all_simple_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, mutable_class, _ = _install_fake_contacts_for_create(monkeypatch)
    connector = ContactsConnector()
    connector._run_cn_create_contact(
        fields={
            "given_name": "G",
            "family_name": "F",
            "middle_name": "M",
            "name_prefix": "P",
            "name_suffix": "S",
            "nickname": "N",
            "organization": "O",
            "job_title": "J",
            "department": "D",
        },
        group_identifier=None,
    )
    mutable = mutable_class.alloc.return_value.init.return_value
    mutable.setGivenName_.assert_called_once_with("G")
    mutable.setFamilyName_.assert_called_once_with("F")
    mutable.setMiddleName_.assert_called_once_with("M")
    mutable.setNamePrefix_.assert_called_once_with("P")
    mutable.setNameSuffix_.assert_called_once_with("S")
    mutable.setNickname_.assert_called_once_with("N")
    mutable.setOrganizationName_.assert_called_once_with("O")
    mutable.setJobTitle_.assert_called_once_with("J")
    mutable.setDepartmentName_.assert_called_once_with("D")


def test_create_contact_skips_setters_for_empty_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, mutable_class, _ = _install_fake_contacts_for_create(monkeypatch)
    connector = ContactsConnector()
    connector._run_cn_create_contact(
        fields={"given_name": "Alice"}, group_identifier=None
    )
    mutable = mutable_class.alloc.return_value.init.return_value
    mutable.setFamilyName_.assert_not_called()
    mutable.setOrganizationName_.assert_not_called()
    mutable.setBirthday_.assert_not_called()


def test_create_contact_with_phones_emails_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, mutable_class, _ = _install_fake_contacts_for_create(monkeypatch)
    connector = ContactsConnector()
    connector._run_cn_create_contact(
        fields={
            "given_name": "Alice",
            "phones": [{"label": "mobile", "value": "+1 555-1212"}],
            "emails": [{"label": "home", "value": "alice@example.com"}],
            "urls": [{"label": "", "value": "https://example.com"}],
        },
        group_identifier=None,
    )
    mutable = mutable_class.alloc.return_value.init.return_value
    mutable.setPhoneNumbers_.assert_called_once()
    mutable.setEmailAddresses_.assert_called_once()
    mutable.setUrlAddresses_.assert_called_once()


def test_create_contact_with_postal_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, mutable_class, _ = _install_fake_contacts_for_create(monkeypatch)
    connector = ContactsConnector()
    connector._run_cn_create_contact(
        fields={
            "given_name": "Alice",
            "postal_addresses": [
                {
                    "label": "home",
                    "street": "1 Loop", "city": "Cupertino",
                    "state": "CA", "postal_code": "95014",
                    "country": "USA", "iso_country_code": "us",
                }
            ],
        },
        group_identifier=None,
    )
    mutable = mutable_class.alloc.return_value.init.return_value
    mutable.setPostalAddresses_.assert_called_once()


def test_create_contact_with_birthday(monkeypatch: pytest.MonkeyPatch) -> None:
    _, _, mutable_class, _ = _install_fake_contacts_for_create(monkeypatch)
    connector = ContactsConnector()
    connector._run_cn_create_contact(
        fields={
            "given_name": "Alice",
            "birthday": {"year": 1990, "month": 5, "day": 15},
        },
        group_identifier=None,
    )
    mutable = mutable_class.alloc.return_value.init.return_value
    mutable.setBirthday_.assert_called_once()


def test_create_contact_with_group_attaches_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_group = MagicMock(name="CNGroup_instance")
    _, store, _, save_req = _install_fake_contacts_for_create(
        monkeypatch, group_results=[fake_group]
    )
    connector = ContactsConnector()
    result = connector._run_cn_create_contact(
        fields={"given_name": "Alice"}, group_identifier="MCP-Test-id"
    )
    assert result  # any identifier
    save_req.addContact_toContainerWithIdentifier_.assert_called_once()
    save_req.addMember_toGroup_.assert_called_once()
    args, _ = save_req.addMember_toGroup_.call_args
    assert args[1] is fake_group
    store.executeSaveRequest_error_.assert_called_once()


def test_create_contact_raises_not_found_when_group_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, store, _, save_req = _install_fake_contacts_for_create(
        monkeypatch, group_results=[]
    )
    connector = ContactsConnector()
    with pytest.raises(ContactsNotFoundError):
        connector._run_cn_create_contact(
            fields={"given_name": "Alice"}, group_identifier="missing"
        )
    save_req.addContact_toContainerWithIdentifier_.assert_not_called()
    store.executeSaveRequest_error_.assert_not_called()


def test_create_contact_raises_when_save_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_save_error = MagicMock(name="NSError(save)")
    fake_save_error.__str__.return_value = "save boom"
    _install_fake_contacts_for_create(
        monkeypatch, save_succeeds=False, fake_save_error=fake_save_error
    )
    connector = ContactsConnector()
    with pytest.raises(ContactsError) as exc_info:
        connector._run_cn_create_contact(
            fields={"given_name": "Alice"}, group_identifier=None
        )
    assert "save boom" in str(exc_info.value)


# ---------------------------------------------------------------------------
# _run_cn_update_contact + _run_cn_delete_contact
# ---------------------------------------------------------------------------


def _install_fake_contacts_for_modify(
    monkeypatch: pytest.MonkeyPatch,
    fetched_contact: MagicMock | None,
    save_succeeds: bool = True,
    fake_save_error: Any | None = None,
    group_results: list[Any] | None = None,
) -> tuple[MagicMock, MagicMock, MagicMock, MagicMock]:
    """Install a fake `Contacts` + `Foundation` set for update/delete tests.

    The store's unifiedContactWithIdentifier_keysToFetch_error_ returns
    (fetched_contact, None) — pass None to simulate not-found.
    The fetched_contact (if non-None) needs a .mutableCopy() that returns
    a fresh MagicMock.
    Returns (store_instance, mutable_instance, save_request_instance,
    cn_save_request_class).
    """
    fake_contacts = types.ModuleType("Contacts")
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
        "CNContactIdentifierKey",
    ):
        setattr(fake_contacts, k, k)

    cn_phone_class = MagicMock(name="CNPhoneNumber")
    cn_phone_class.phoneNumberWithStringValue_.side_effect = (
        lambda v: f"PhoneNumber({v})"
    )
    fake_contacts.CNPhoneNumber = cn_phone_class  # type: ignore[attr-defined]

    cn_postal_class = MagicMock(name="CNMutablePostalAddress")
    cn_postal_class.alloc.return_value.init.return_value = MagicMock(
        name="postal_instance"
    )
    fake_contacts.CNMutablePostalAddress = cn_postal_class  # type: ignore[attr-defined]

    cn_labeled_class = MagicMock(name="CNLabeledValue")
    cn_labeled_class.labeledValueWithLabel_value_.side_effect = (
        lambda lbl, val: ("labeled", lbl, val)
    )
    fake_contacts.CNLabeledValue = cn_labeled_class  # type: ignore[attr-defined]

    cn_save_request_class = MagicMock(name="CNSaveRequest")
    save_request_instance = MagicMock(name="save_request_instance")
    cn_save_request_class.alloc.return_value.init.return_value = save_request_instance
    fake_contacts.CNSaveRequest = cn_save_request_class  # type: ignore[attr-defined]

    # CNGroup is consulted by `_run_cn_fetch_group`, which is reused by the
    # group-membership writes (`_load_contact_and_group`). For tests that
    # don't touch groups, group_results stays None and the store will return
    # an empty list — which is fine because those tests never reach the
    # group-fetch branch.
    cn_group_class = MagicMock(name="CNGroup")
    cn_group_class.predicateForGroupsWithIdentifiers_.return_value = "GROUP_PRED"
    fake_contacts.CNGroup = cn_group_class  # type: ignore[attr-defined]

    cn_store_class = MagicMock(name="CNContactStore")
    store_instance = MagicMock(name="store_instance")
    if fetched_contact is None:
        store_instance.unifiedContactWithIdentifier_keysToFetch_error_.return_value = (
            None,
            MagicMock(name="NSError(not_found)"),
        )
        mutable_instance = MagicMock(name="mutable_unused")
    else:
        store_instance.unifiedContactWithIdentifier_keysToFetch_error_.return_value = (
            fetched_contact,
            None,
        )
        mutable_instance = MagicMock(name="mutable_instance")
        fetched_contact.mutableCopy.return_value = mutable_instance
    store_instance.executeSaveRequest_error_.return_value = (
        save_succeeds,
        fake_save_error,
    )
    if group_results is None:
        # Default: no group present (used for non-membership tests; safe since
        # they never call _run_cn_fetch_group).
        store_instance.groupsMatchingPredicate_error_.return_value = ([], None)
    else:
        store_instance.groupsMatchingPredicate_error_.return_value = (
            group_results,
            None,
        )
    cn_store_class.alloc.return_value.init.return_value = store_instance
    fake_contacts.CNContactStore = cn_store_class  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "Contacts", fake_contacts)

    fake_foundation = types.ModuleType("Foundation")
    nsdc_class = MagicMock(name="NSDateComponents")
    nsdc_class.alloc.return_value.init.return_value = MagicMock(name="nsdc_instance")
    fake_foundation.NSDateComponents = nsdc_class  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "Foundation", fake_foundation)

    return store_instance, mutable_instance, save_request_instance, cn_save_request_class


# ----- _run_cn_update_contact -------------------------------------------------


def test_update_contact_raises_not_found_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _, save_req, _ = _install_fake_contacts_for_modify(
        monkeypatch, fetched_contact=None
    )
    connector = ContactsConnector()
    with pytest.raises(ContactsNotFoundError):
        connector._run_cn_update_contact("missing", {"given_name": "X"})
    save_req.updateContact_.assert_not_called()
    store.executeSaveRequest_error_.assert_not_called()


def test_update_contact_only_supplied_setters_fire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetched = MagicMock(name="CNContact_fetched")
    store, mutable, save_req, _ = _install_fake_contacts_for_modify(
        monkeypatch, fetched_contact=fetched
    )
    connector = ContactsConnector()
    result = connector._run_cn_update_contact(
        "ABCD", {"given_name": "Alice"}
    )
    assert result == "ABCD"
    mutable.setGivenName_.assert_called_once_with("Alice")
    mutable.setFamilyName_.assert_not_called()
    mutable.setOrganizationName_.assert_not_called()
    mutable.setPhoneNumbers_.assert_not_called()
    mutable.setBirthday_.assert_not_called()
    save_req.updateContact_.assert_called_once_with(mutable)
    store.executeSaveRequest_error_.assert_called_once()


def test_update_contact_empty_string_clears_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Presence semantics: passing given_name='' clears, doesn't skip."""
    fetched = MagicMock(name="CNContact_fetched")
    _, mutable, _, _ = _install_fake_contacts_for_modify(
        monkeypatch, fetched_contact=fetched
    )
    connector = ContactsConnector()
    connector._run_cn_update_contact("ABCD", {"given_name": ""})
    mutable.setGivenName_.assert_called_once_with("")


def test_update_contact_phones_empty_list_clears_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetched = MagicMock(name="CNContact_fetched")
    _, mutable, _, _ = _install_fake_contacts_for_modify(
        monkeypatch, fetched_contact=fetched
    )
    connector = ContactsConnector()
    connector._run_cn_update_contact("ABCD", {"phones": []})
    mutable.setPhoneNumbers_.assert_called_once_with([])


def test_update_contact_phones_replaces_with_supplied_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetched = MagicMock(name="CNContact_fetched")
    _, mutable, _, _ = _install_fake_contacts_for_modify(
        monkeypatch, fetched_contact=fetched
    )
    connector = ContactsConnector()
    connector._run_cn_update_contact(
        "ABCD",
        {"phones": [{"label": "mobile", "value": "+1 555-1212"}]},
    )
    mutable.setPhoneNumbers_.assert_called_once()
    args, _ = mutable.setPhoneNumbers_.call_args
    assert len(args[0]) == 1


def test_update_contact_birthday_replaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetched = MagicMock(name="CNContact_fetched")
    _, mutable, _, _ = _install_fake_contacts_for_modify(
        monkeypatch, fetched_contact=fetched
    )
    connector = ContactsConnector()
    connector._run_cn_update_contact(
        "ABCD", {"birthday": {"year": 1991, "month": 6, "day": 1}}
    )
    mutable.setBirthday_.assert_called_once()


def test_update_contact_save_failure_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetched = MagicMock(name="CNContact_fetched")
    fake_save_error = MagicMock(name="NSError(save)")
    fake_save_error.__str__.return_value = "save boom"
    _install_fake_contacts_for_modify(
        monkeypatch,
        fetched_contact=fetched,
        save_succeeds=False,
        fake_save_error=fake_save_error,
    )
    connector = ContactsConnector()
    with pytest.raises(ContactsError) as exc_info:
        connector._run_cn_update_contact("ABCD", {"given_name": "X"})
    assert "save boom" in str(exc_info.value)


# ----- _run_cn_delete_contact -------------------------------------------------


def test_delete_contact_raises_not_found_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _, save_req, _ = _install_fake_contacts_for_modify(
        monkeypatch, fetched_contact=None
    )
    connector = ContactsConnector()
    with pytest.raises(ContactsNotFoundError):
        connector._run_cn_delete_contact("missing")
    save_req.deleteContact_.assert_not_called()
    store.executeSaveRequest_error_.assert_not_called()


def test_delete_contact_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    fetched = MagicMock(name="CNContact_fetched")
    store, mutable, save_req, _ = _install_fake_contacts_for_modify(
        monkeypatch, fetched_contact=fetched
    )
    connector = ContactsConnector()
    result = connector._run_cn_delete_contact("ABCD")
    assert result == "ABCD"
    save_req.deleteContact_.assert_called_once_with(mutable)
    store.executeSaveRequest_error_.assert_called_once()


def test_delete_contact_save_failure_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetched = MagicMock(name="CNContact_fetched")
    fake_save_error = MagicMock(name="NSError(save)")
    fake_save_error.__str__.return_value = "delete boom"
    _install_fake_contacts_for_modify(
        monkeypatch,
        fetched_contact=fetched,
        save_succeeds=False,
        fake_save_error=fake_save_error,
    )
    connector = ContactsConnector()
    with pytest.raises(ContactsError) as exc_info:
        connector._run_cn_delete_contact("ABCD")
    assert "delete boom" in str(exc_info.value)


# ---------------------------------------------------------------------------
# _run_cn_read_photo / _run_cn_write_photo
# ---------------------------------------------------------------------------


def _install_fake_contacts_for_photo(
    monkeypatch: pytest.MonkeyPatch,
    fetched_contact: MagicMock | None = None,
    save_succeeds: bool = True,
    fake_save_error: Any | None = None,
) -> tuple[MagicMock, MagicMock, MagicMock]:
    """Install a fake `Contacts` module covering read/write photo paths.

    Returns (store_instance, save_request_instance, mutable_instance).
    For read tests, mutable_instance is unused. For write tests with a
    real fetched_contact, mutable_instance is what mutableCopy() returns;
    callers can inspect setImageData_ calls on it.
    """
    fake_contacts = types.ModuleType("Contacts")
    fake_contacts.CNContactImageDataKey = "CNContactImageDataKey"  # type: ignore[attr-defined]
    fake_contacts.CNContactImageDataAvailableKey = (  # type: ignore[attr-defined]
        "CNContactImageDataAvailableKey"
    )

    save_req = MagicMock(name="save_request")
    cn_save_request_class = MagicMock(name="CNSaveRequest")
    cn_save_request_class.alloc.return_value.init.return_value = save_req
    fake_contacts.CNSaveRequest = cn_save_request_class  # type: ignore[attr-defined]

    store_instance = MagicMock(name="store_instance")
    mutable_instance = MagicMock(name="mutable_instance")
    if fetched_contact is None:
        store_instance.unifiedContactWithIdentifier_keysToFetch_error_.return_value = (
            None,
            MagicMock(name="NSError(not_found)"),
        )
    else:
        store_instance.unifiedContactWithIdentifier_keysToFetch_error_.return_value = (
            fetched_contact,
            None,
        )
        fetched_contact.mutableCopy.return_value = mutable_instance
    store_instance.executeSaveRequest_error_.return_value = (
        save_succeeds,
        fake_save_error,
    )

    cn_store_class = MagicMock(name="CNContactStore")
    cn_store_class.alloc.return_value.init.return_value = store_instance
    fake_contacts.CNContactStore = cn_store_class  # type: ignore[attr-defined]
    fake_contacts.CNEntityTypeContacts = 0  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "Contacts", fake_contacts)
    return store_instance, save_req, mutable_instance


# ----- _run_cn_read_photo -----------------------------------------------------


def test_read_photo_missing_contact_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_contacts_for_photo(monkeypatch, fetched_contact=None)
    connector = ContactsConnector()
    assert connector._run_cn_read_photo("MISSING") is None


def test_read_photo_returns_empty_bytes_when_no_photo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """imageDataAvailable() is False → contract returns {available: False,
    image_data: b''} WITHOUT calling imageData()."""
    fetched = MagicMock(name="fetched")
    fetched.imageDataAvailable.return_value = False
    _install_fake_contacts_for_photo(monkeypatch, fetched_contact=fetched)
    connector = ContactsConnector()
    result = connector._run_cn_read_photo("ABCD")
    assert result == {"available": False, "image_data": b""}
    fetched.imageData.assert_not_called()


def test_read_photo_returns_bytes_when_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetched = MagicMock(name="fetched")
    fetched.imageDataAvailable.return_value = True
    fetched.imageData.return_value = b"\xff\xd8\xfffake-jpeg"
    _install_fake_contacts_for_photo(monkeypatch, fetched_contact=fetched)
    connector = ContactsConnector()
    result = connector._run_cn_read_photo("ABCD")
    assert result == {
        "available": True,
        "image_data": b"\xff\xd8\xfffake-jpeg",
    }


def test_read_photo_handles_falsy_imagedata_defensively(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If imageDataAvailable() lies (True) but imageData() returns None,
    we don't crash; we return empty bytes."""
    fetched = MagicMock(name="fetched")
    fetched.imageDataAvailable.return_value = True
    fetched.imageData.return_value = None
    _install_fake_contacts_for_photo(monkeypatch, fetched_contact=fetched)
    connector = ContactsConnector()
    result = connector._run_cn_read_photo("ABCD")
    assert result == {"available": True, "image_data": b""}


# ----- _run_cn_write_photo ----------------------------------------------------


def test_write_photo_raises_not_found_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, save_req, _ = _install_fake_contacts_for_photo(
        monkeypatch, fetched_contact=None
    )
    connector = ContactsConnector()
    with pytest.raises(ContactsNotFoundError):
        connector._run_cn_write_photo("MISSING", b"x")
    save_req.updateContact_.assert_not_called()
    store.executeSaveRequest_error_.assert_not_called()


def test_write_photo_happy_path_sets_image_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetched = MagicMock(name="fetched")
    store, save_req, mutable = _install_fake_contacts_for_photo(
        monkeypatch, fetched_contact=fetched
    )
    connector = ContactsConnector()
    payload = b"\xff\xd8\xfffake-jpeg"
    result = connector._run_cn_write_photo("ABCD", payload)
    assert result == "ABCD"
    mutable.setImageData_.assert_called_once_with(payload)
    save_req.updateContact_.assert_called_once_with(mutable)
    store.executeSaveRequest_error_.assert_called_once()


def test_write_photo_clear_passes_none_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetched = MagicMock(name="fetched")
    _, _, mutable = _install_fake_contacts_for_photo(
        monkeypatch, fetched_contact=fetched
    )
    connector = ContactsConnector()
    result = connector._run_cn_write_photo("ABCD", None)
    assert result == "ABCD"
    mutable.setImageData_.assert_called_once_with(None)


def test_write_photo_save_failure_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetched = MagicMock(name="fetched")
    err = MagicMock(name="NSError(save)")
    err.__str__.return_value = "photo boom"
    _install_fake_contacts_for_photo(
        monkeypatch,
        fetched_contact=fetched,
        save_succeeds=False,
        fake_save_error=err,
    )
    connector = ContactsConnector()
    with pytest.raises(ContactsError) as exc_info:
        connector._run_cn_write_photo("ABCD", b"x")
    assert "CN photo write failed" in str(exc_info.value)
    assert "photo boom" in str(exc_info.value)


# ---------------------------------------------------------------------------
# _run_cn_create_group / _run_cn_rename_group / _run_cn_delete_group
# ---------------------------------------------------------------------------


def _install_fake_contacts_for_group_crud(
    monkeypatch: pytest.MonkeyPatch,
    fetched_group: MagicMock | None = None,
    new_group_id: str = "NEW-GROUP-ID:ABGroup",
    new_group_name: str = "Probe Group",
    container_results: list[MagicMock] | None = None,
    save_succeeds: bool = True,
    fake_save_error: Any | None = None,
) -> tuple[MagicMock, MagicMock, MagicMock]:
    """Install a fake `Contacts` module for group CRUD tests.

    The store's ``groupsMatchingPredicate_error_`` returns
    ``([fetched_group], None)`` if ``fetched_group`` is non-None,
    otherwise ``([], None)`` so ``_run_cn_fetch_group`` returns None.

    ``container_results`` powers the post-save container_id resolution
    in ``_resolve_container_id``. Defaults to a single fake container
    with identifier "DEFAULT-CONT:ABAccount".

    Returns (store_instance, save_request_instance, new_mutable_instance).
    The new_mutable_instance is what CNMutableGroup.alloc().init() returns —
    callers can inspect ``setName_`` calls on it.
    """
    fake_contacts = types.ModuleType("Contacts")

    new_mutable = MagicMock(name="new_mutable_group")
    new_mutable.identifier.return_value = new_group_id
    new_mutable.name.return_value = new_group_name
    cn_mutable_group_class = MagicMock(name="CNMutableGroup")
    cn_mutable_group_class.alloc.return_value.init.return_value = new_mutable
    fake_contacts.CNMutableGroup = cn_mutable_group_class  # type: ignore[attr-defined]

    cn_group_class = MagicMock(name="CNGroup")
    cn_group_class.predicateForGroupsWithIdentifiers_.return_value = "GROUP_PRED"
    fake_contacts.CNGroup = cn_group_class  # type: ignore[attr-defined]

    cn_container_class = MagicMock(name="CNContainer")
    cn_container_class.predicateForContainerOfGroupWithIdentifier_.return_value = (
        "CONTAINER_PRED"
    )
    fake_contacts.CNContainer = cn_container_class  # type: ignore[attr-defined]

    save_req = MagicMock(name="save_request")
    cn_save_request_class = MagicMock(name="CNSaveRequest")
    cn_save_request_class.alloc.return_value.init.return_value = save_req
    fake_contacts.CNSaveRequest = cn_save_request_class  # type: ignore[attr-defined]

    store_instance = MagicMock(name="store_instance")
    if fetched_group is None:
        store_instance.groupsMatchingPredicate_error_.return_value = (
            [],
            None,
        )
    else:
        store_instance.groupsMatchingPredicate_error_.return_value = (
            [fetched_group],
            None,
        )
    store_instance.executeSaveRequest_error_.return_value = (
        save_succeeds,
        fake_save_error,
    )
    if container_results is None:
        default_container = MagicMock(name="default_container")
        default_container.identifier.return_value = "DEFAULT-CONT:ABAccount"
        container_results = [default_container]
    store_instance.containersMatchingPredicate_error_.return_value = (
        container_results,
        None,
    )

    cn_store_class = MagicMock(name="CNContactStore")
    cn_store_class.alloc.return_value.init.return_value = store_instance
    fake_contacts.CNContactStore = cn_store_class  # type: ignore[attr-defined]
    fake_contacts.CNEntityTypeContacts = 0  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "Contacts", fake_contacts)
    return store_instance, save_req, new_mutable


# ----- _run_cn_create_group ---------------------------------------------------


def test_create_group_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    store, save_req, new_mutable = _install_fake_contacts_for_group_crud(
        monkeypatch,
        new_group_id="GRP-1:ABGroup",
        new_group_name="MyGroup",
    )
    connector = ContactsConnector()
    result = connector._run_cn_create_group(
        name="MyGroup", container_identifier=None
    )
    assert result == {
        "id": "GRP-1:ABGroup",
        "name": "MyGroup",
        "container_id": "DEFAULT-CONT:ABAccount",
    }
    new_mutable.setName_.assert_called_once_with("MyGroup")
    save_req.addGroup_toContainerWithIdentifier_.assert_called_once_with(
        new_mutable, None
    )
    store.executeSaveRequest_error_.assert_called_once()


def test_create_group_with_explicit_container_passes_uuid_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, save_req, new_mutable = _install_fake_contacts_for_group_crud(monkeypatch)
    connector = ContactsConnector()
    connector._run_cn_create_group(
        name="X", container_identifier="GMAIL-UUID:ABAccount"
    )
    args, _ = save_req.addGroup_toContainerWithIdentifier_.call_args
    assert args[0] is new_mutable
    assert args[1] == "GMAIL-UUID:ABAccount"


def test_create_group_falls_back_to_empty_container_id_when_lookup_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_contacts_for_group_crud(
        monkeypatch, container_results=[]
    )
    connector = ContactsConnector()
    result = connector._run_cn_create_group(
        name="MyGroup", container_identifier=None
    )
    assert result["container_id"] == ""


def test_create_group_save_failure_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    err = MagicMock(name="NSError(save)")
    err.__str__.return_value = "create boom"
    _install_fake_contacts_for_group_crud(
        monkeypatch, save_succeeds=False, fake_save_error=err
    )
    connector = ContactsConnector()
    with pytest.raises(ContactsError) as exc_info:
        connector._run_cn_create_group(name="X", container_identifier=None)
    assert "CN group create failed" in str(exc_info.value)
    assert "create boom" in str(exc_info.value)


# ----- _run_cn_rename_group ---------------------------------------------------


def test_rename_group_raises_not_found_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, save_req, _ = _install_fake_contacts_for_group_crud(
        monkeypatch, fetched_group=None
    )
    connector = ContactsConnector()
    with pytest.raises(ContactsNotFoundError):
        connector._run_cn_rename_group("MISSING", "Newname")
    save_req.updateGroup_.assert_not_called()
    store.executeSaveRequest_error_.assert_not_called()


def test_rename_group_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    existing = MagicMock(name="existing_group")
    mutable_copy = MagicMock(name="mutable_copy")
    mutable_copy.name.return_value = "Updated Name"
    existing.mutableCopy.return_value = mutable_copy
    store, save_req, _ = _install_fake_contacts_for_group_crud(
        monkeypatch, fetched_group=existing
    )
    connector = ContactsConnector()
    result = connector._run_cn_rename_group("GRP-1:ABGroup", "Updated Name")
    assert result == {
        "id": "GRP-1:ABGroup",
        "name": "Updated Name",
        "container_id": "DEFAULT-CONT:ABAccount",
    }
    mutable_copy.setName_.assert_called_once_with("Updated Name")
    save_req.updateGroup_.assert_called_once_with(mutable_copy)
    store.executeSaveRequest_error_.assert_called_once()


def test_rename_group_save_failure_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = MagicMock(name="existing_group")
    existing.mutableCopy.return_value = MagicMock(name="mutable_copy")
    err = MagicMock(name="NSError(save)")
    err.__str__.return_value = "rename boom"
    _install_fake_contacts_for_group_crud(
        monkeypatch,
        fetched_group=existing,
        save_succeeds=False,
        fake_save_error=err,
    )
    connector = ContactsConnector()
    with pytest.raises(ContactsError) as exc_info:
        connector._run_cn_rename_group("GRP-1", "X")
    assert "CN group rename failed" in str(exc_info.value)
    assert "rename boom" in str(exc_info.value)


# ----- _run_cn_delete_group ---------------------------------------------------


def test_delete_group_raises_not_found_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, save_req, _ = _install_fake_contacts_for_group_crud(
        monkeypatch, fetched_group=None
    )
    connector = ContactsConnector()
    with pytest.raises(ContactsNotFoundError):
        connector._run_cn_delete_group("MISSING")
    save_req.deleteGroup_.assert_not_called()
    store.executeSaveRequest_error_.assert_not_called()


def test_delete_group_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    existing = MagicMock(name="existing_group")
    mutable_copy = MagicMock(name="mutable_copy")
    existing.mutableCopy.return_value = mutable_copy
    store, save_req, _ = _install_fake_contacts_for_group_crud(
        monkeypatch, fetched_group=existing
    )
    connector = ContactsConnector()
    result = connector._run_cn_delete_group("GRP-1:ABGroup")
    assert result == "GRP-1:ABGroup"
    save_req.deleteGroup_.assert_called_once_with(mutable_copy)
    store.executeSaveRequest_error_.assert_called_once()


def test_delete_group_save_failure_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = MagicMock(name="existing_group")
    existing.mutableCopy.return_value = MagicMock(name="mutable_copy")
    err = MagicMock(name="NSError(save)")
    err.__str__.return_value = "delete boom"
    _install_fake_contacts_for_group_crud(
        monkeypatch,
        fetched_group=existing,
        save_succeeds=False,
        fake_save_error=err,
    )
    connector = ContactsConnector()
    with pytest.raises(ContactsError) as exc_info:
        connector._run_cn_delete_group("GRP-1")
    assert "CN group delete failed" in str(exc_info.value)
    assert "delete boom" in str(exc_info.value)


# ---------------------------------------------------------------------------
# _run_cn_list_containers
# ---------------------------------------------------------------------------


def _make_fake_cn_container(
    container_id: str, name: str, type_int: int
) -> MagicMock:
    c = MagicMock(name=f"CNContainer({container_id})")
    c.identifier.return_value = container_id
    c.name.return_value = name
    c.type.return_value = type_int
    return c


def _install_fake_contacts_for_containers(
    monkeypatch: pytest.MonkeyPatch,
    containers: list[MagicMock] | None,
    default_id: str = "DEFAULT-UUID:ABAccount",
    containers_err: object | None = None,
) -> MagicMock:
    """Install a fake `Contacts` module whose store returns the given
    container list from ``containersMatchingPredicate_error_`` and the
    given ``defaultContainerIdentifier``."""
    store_instance = MagicMock(name="store_instance")
    store_instance.containersMatchingPredicate_error_.return_value = (
        containers,
        containers_err,
    )
    store_instance.defaultContainerIdentifier.return_value = default_id
    _install_fake_contacts_module(monkeypatch, store_factory=store_instance)
    return store_instance


def test_list_containers_empty_store_returns_empty_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_contacts_for_containers(monkeypatch, containers=[])
    connector = ContactsConnector()
    assert connector._run_cn_list_containers() == []


def test_list_containers_serializes_id_name_type_and_default_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    icloud = _make_fake_cn_container("ICLOUD-UUID:ABAccount", "iCloud", 3)
    gmail = _make_fake_cn_container("GMAIL-UUID:ABAccount", "Gmail", 3)
    _install_fake_contacts_for_containers(
        monkeypatch,
        containers=[icloud, gmail],
        default_id="ICLOUD-UUID:ABAccount",
    )
    connector = ContactsConnector()
    result = connector._run_cn_list_containers()
    assert result == [
        {
            "id": "ICLOUD-UUID:ABAccount",
            "name": "iCloud",
            "type": "cardDAV",
            "is_default": True,
        },
        {
            "id": "GMAIL-UUID:ABAccount",
            "name": "Gmail",
            "type": "cardDAV",
            "is_default": False,
        },
    ]


def test_list_containers_maps_each_known_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local = _make_fake_cn_container("L:ABAccount", "On My Mac", 1)
    exchange = _make_fake_cn_container("E:ABAccount", "Work", 2)
    carddav = _make_fake_cn_container("C:ABAccount", "iCloud", 3)
    _install_fake_contacts_for_containers(
        monkeypatch,
        containers=[local, exchange, carddav],
        default_id="C:ABAccount",
    )
    connector = ContactsConnector()
    result = connector._run_cn_list_containers()
    types = {r["name"]: r["type"] for r in result}
    assert types == {
        "On My Mac": "local",
        "Work": "exchange",
        "iCloud": "cardDAV",
    }


def test_list_containers_marks_unknown_type_explicitly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    weird = _make_fake_cn_container("X:ABAccount", "Future", 99)
    _install_fake_contacts_for_containers(monkeypatch, containers=[weird])
    connector = ContactsConnector()
    result = connector._run_cn_list_containers()
    assert result[0]["type"] == "unknown(99)"


def test_list_containers_raises_when_predicate_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_contacts_for_containers(
        monkeypatch, containers=None, containers_err="cn boom"
    )
    connector = ContactsConnector()
    with pytest.raises(ContactsError) as exc_info:
        connector._run_cn_list_containers()
    assert "CN containers fetch failed" in str(exc_info.value)
    assert "cn boom" in str(exc_info.value)


# ---------------------------------------------------------------------------
# _run_cn_list_groups
# ---------------------------------------------------------------------------


def _make_fake_group(group_id: str, name: str) -> MagicMock:
    g = MagicMock(name=f"CNGroup({group_id})")
    g.identifier.return_value = group_id
    g.name.return_value = name
    return g


def _make_fake_container(container_id: str) -> MagicMock:
    c = MagicMock(name=f"CNContainer({container_id})")
    c.identifier.return_value = container_id
    return c


def _install_fake_contacts_for_groups(
    monkeypatch: pytest.MonkeyPatch,
    groups: list[MagicMock] | None,
    container_lookup: dict[str, list[MagicMock] | None] | None = None,
    groups_err: object | None = None,
) -> tuple[MagicMock, MagicMock]:
    """Install a fake `Contacts` module exposing CNContainer + CNContact +
    a CNContactStore whose ``groupsMatchingPredicate_error_(None, None)``
    returns ``(groups, groups_err)`` and whose
    ``containersMatchingPredicate_error_`` looks up by group id via
    ``container_lookup``.

    ``container_lookup`` maps group_id → (containers_list, err); a missing
    key behaves as `(None, fake_error)` to surface raise paths cleanly.
    """
    fake_module = types.ModuleType("Contacts")

    cn_container_class = MagicMock(name="CNContainer")
    # Each call captures the group_id passed in; the predicate object is
    # opaque but uniquely tied to the group via `.return_value` per call.
    pred_factory = MagicMock(
        name="predicateForContainerOfGroupWithIdentifier_"
    )

    def _pred_factory(group_id: str) -> MagicMock:
        m = MagicMock(name=f"ContainerPred({group_id})")
        m._group_id = group_id  # for the store-side dispatcher to read
        return m

    pred_factory.side_effect = _pred_factory
    cn_container_class.predicateForContainerOfGroupWithIdentifier_ = (
        pred_factory
    )
    fake_module.CNContainer = cn_container_class  # type: ignore[attr-defined]

    store_instance = MagicMock(name="store_instance")
    store_instance.groupsMatchingPredicate_error_.return_value = (
        groups,
        groups_err,
    )

    def _containers_dispatch(pred: MagicMock, _err: Any) -> tuple[Any, Any]:
        gid = getattr(pred, "_group_id", None)
        if container_lookup is not None and gid in container_lookup:
            containers, cerr = (
                container_lookup[gid]
                if isinstance(container_lookup[gid], tuple)
                else (container_lookup[gid], None)
            )
            return containers, cerr
        # default: every group resolves to a single fake container
        return [_make_fake_container(f"container-of-{gid}")], None

    store_instance.containersMatchingPredicate_error_.side_effect = (
        _containers_dispatch
    )

    cn_store_class = MagicMock(name="CNContactStore")
    cn_store_class.alloc.return_value.init.return_value = store_instance
    fake_module.CNContactStore = cn_store_class  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "Contacts", fake_module)
    return cn_container_class, store_instance


def test_list_groups_empty_store_returns_empty_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_contacts_for_groups(monkeypatch, groups=[])
    connector = ContactsConnector()
    assert connector._run_cn_list_groups() == []


def test_list_groups_serializes_id_name_and_container_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_groups = [
        _make_fake_group("G1", "Family"),
        _make_fake_group("G2", "Work"),
    ]
    _install_fake_contacts_for_groups(monkeypatch, groups=fake_groups)
    connector = ContactsConnector()
    result = connector._run_cn_list_groups()
    assert result == [
        {"id": "G1", "name": "Family", "container_id": "container-of-G1"},
        {"id": "G2", "name": "Work", "container_id": "container-of-G2"},
    ]


def test_list_groups_falls_back_to_empty_container_id_when_lookup_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_groups = [_make_fake_group("G1", "Orphan")]
    _install_fake_contacts_for_groups(
        monkeypatch,
        groups=fake_groups,
        container_lookup={"G1": ([], None)},
    )
    connector = ContactsConnector()
    [entry] = connector._run_cn_list_groups()
    assert entry["container_id"] == ""


def test_list_groups_raises_when_groups_predicate_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_err = MagicMock(name="NSError")
    fake_err.__str__.return_value = "groups-boom"
    _install_fake_contacts_for_groups(
        monkeypatch, groups=None, groups_err=fake_err
    )
    connector = ContactsConnector()
    with pytest.raises(ContactsError) as exc_info:
        connector._run_cn_list_groups()
    assert "groups-boom" in str(exc_info.value)


def test_list_groups_raises_when_container_lookup_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_groups = [_make_fake_group("G1", "Family")]
    fake_err = MagicMock(name="NSError")
    fake_err.__str__.return_value = "container-boom"
    _install_fake_contacts_for_groups(
        monkeypatch,
        groups=fake_groups,
        container_lookup={"G1": (None, fake_err)},
    )
    connector = ContactsConnector()
    with pytest.raises(ContactsError) as exc_info:
        connector._run_cn_list_groups()
    assert "container-boom" in str(exc_info.value)


# ---------------------------------------------------------------------------
# _run_cn_contacts_in_group
# ---------------------------------------------------------------------------


def _install_fake_contacts_for_group_members(
    monkeypatch: pytest.MonkeyPatch,
    results: list[MagicMock] | None,
    fake_error: object | None = None,
) -> tuple[MagicMock, MagicMock]:
    """Install a fake `Contacts` module + a store whose
    ``unifiedContactsMatchingPredicate_keysToFetch_error_`` returns
    ``(results, fake_error)``. Returns ``(CNContact_class, store)`` for
    predicate-call assertions.
    """
    fake_module = types.ModuleType("Contacts")
    fake_module.CNContactIdentifierKey = "id_key"  # type: ignore[attr-defined]
    fake_module.CNContactGivenNameKey = "given_key"  # type: ignore[attr-defined]
    fake_module.CNContactFamilyNameKey = "family_key"  # type: ignore[attr-defined]
    fake_module.CNContactOrganizationNameKey = "org_key"  # type: ignore[attr-defined]

    cn_contact_class = MagicMock(name="CNContact")
    pred_stub = MagicMock(name="MembershipPredicate")
    cn_contact_class.predicateForContactsInGroupWithIdentifier_.return_value = (
        pred_stub
    )
    fake_module.CNContact = cn_contact_class  # type: ignore[attr-defined]

    store_instance = MagicMock(name="store_instance")
    store_instance.unifiedContactsMatchingPredicate_keysToFetch_error_.return_value = (
        results,
        fake_error,
    )
    cn_store_class = MagicMock(name="CNContactStore")
    cn_store_class.alloc.return_value.init.return_value = store_instance
    fake_module.CNContactStore = cn_store_class  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "Contacts", fake_module)
    return cn_contact_class, store_instance


def test_contacts_in_group_predicate_uses_supplied_group_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cn_contact_class, _ = _install_fake_contacts_for_group_members(
        monkeypatch, results=[]
    )
    connector = ContactsConnector()
    connector._run_cn_contacts_in_group("MY-GROUP", limit=10)
    cn_contact_class.predicateForContactsInGroupWithIdentifier_.assert_called_once_with(
        "MY-GROUP"
    )


def test_contacts_in_group_returns_4_field_dicts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fakes = [
        _make_basic_contact("id-0", "Alice", "Anderson", "Acme"),
        _make_basic_contact("id-1", "Bob", "Brown", ""),
    ]
    _install_fake_contacts_for_group_members(monkeypatch, results=fakes)
    connector = ContactsConnector()
    result = connector._run_cn_contacts_in_group("G", limit=200)
    assert result == [
        {"id": "id-0", "given_name": "Alice", "family_name": "Anderson", "organization": "Acme"},
        {"id": "id-1", "given_name": "Bob", "family_name": "Brown", "organization": ""},
    ]


def test_contacts_in_group_caps_at_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fakes = [_make_basic_contact(f"id-{i}") for i in range(250)]
    _install_fake_contacts_for_group_members(monkeypatch, results=fakes)
    connector = ContactsConnector()
    result = connector._run_cn_contacts_in_group("G", limit=200)
    assert len(result) == 200
    # 201st contact's selectors must never have been touched.
    assert fakes[200].identifier.call_count == 0


def test_contacts_in_group_empty_for_unknown_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Apple's predicate returns [] (not error) for unknown group_ids."""
    _install_fake_contacts_for_group_members(monkeypatch, results=[])
    connector = ContactsConnector()
    assert (
        connector._run_cn_contacts_in_group("nonexistent", limit=200) == []
    )


def test_contacts_in_group_raises_when_store_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_err = MagicMock(name="NSError")
    fake_err.__str__.return_value = "membership-boom"
    _install_fake_contacts_for_group_members(
        monkeypatch, results=None, fake_error=fake_err
    )
    connector = ContactsConnector()
    with pytest.raises(ContactsError) as exc_info:
        connector._run_cn_contacts_in_group("G", limit=200)
    assert "membership-boom" in str(exc_info.value)


# ---------------------------------------------------------------------------
# _load_contact_and_group + _run_cn_add_contact_to_group +
# _run_cn_remove_contact_from_group
# ---------------------------------------------------------------------------


def test_load_contact_and_group_happy_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetched = MagicMock(name="fetched_contact")
    fake_group = MagicMock(name="CNGroup_instance")
    store, mutable, _save_req, _ = _install_fake_contacts_for_modify(
        monkeypatch, fetched_contact=fetched, group_results=[fake_group]
    )
    connector = ContactsConnector()
    got_mutable, got_group = connector._load_contact_and_group(
        "CONTACT-1", "GROUP-1"
    )
    assert got_mutable is mutable
    assert got_group is fake_group


def test_load_contact_and_group_raises_when_contact_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_contacts_for_modify(
        monkeypatch,
        fetched_contact=None,
        group_results=[MagicMock(name="never-reached")],
    )
    connector = ContactsConnector()
    with pytest.raises(ContactsNotFoundError) as exc_info:
        connector._load_contact_and_group("BAD-CONTACT", "GROUP-1")
    assert "Contact not found" in str(exc_info.value)
    assert "BAD-CONTACT" in str(exc_info.value)


def test_load_contact_and_group_raises_when_group_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetched = MagicMock(name="fetched_contact")
    _install_fake_contacts_for_modify(
        monkeypatch, fetched_contact=fetched, group_results=[]
    )
    connector = ContactsConnector()
    with pytest.raises(ContactsNotFoundError) as exc_info:
        connector._load_contact_and_group("CONTACT-1", "BAD-GROUP")
    assert "Group not found" in str(exc_info.value)
    assert "BAD-GROUP" in str(exc_info.value)


# ----- _run_cn_add_contact_to_group ----------------------------------------


def test_add_contact_to_group_happy_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetched = MagicMock(name="fetched_contact")
    fake_group = MagicMock(name="CNGroup_instance")
    store, mutable, save_req, _ = _install_fake_contacts_for_modify(
        monkeypatch, fetched_contact=fetched, group_results=[fake_group]
    )
    connector = ContactsConnector()
    connector._run_cn_add_contact_to_group("CONTACT-1", "GROUP-1")
    save_req.addMember_toGroup_.assert_called_once_with(mutable, fake_group)
    save_req.removeMember_fromGroup_.assert_not_called()
    store.executeSaveRequest_error_.assert_called_once()


def test_add_contact_to_group_raises_not_found_when_contact_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_contacts_for_modify(
        monkeypatch,
        fetched_contact=None,
        group_results=[MagicMock(name="g")],
    )
    connector = ContactsConnector()
    with pytest.raises(ContactsNotFoundError) as exc_info:
        connector._run_cn_add_contact_to_group("BAD-CONTACT", "GROUP-1")
    assert "Contact not found" in str(exc_info.value)


def test_add_contact_to_group_raises_not_found_when_group_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetched = MagicMock(name="fetched_contact")
    _install_fake_contacts_for_modify(
        monkeypatch, fetched_contact=fetched, group_results=[]
    )
    connector = ContactsConnector()
    with pytest.raises(ContactsNotFoundError) as exc_info:
        connector._run_cn_add_contact_to_group("CONTACT-1", "BAD-GROUP")
    assert "Group not found" in str(exc_info.value)


def test_add_contact_to_group_save_failure_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetched = MagicMock(name="fetched_contact")
    fake_group = MagicMock(name="CNGroup_instance")
    fake_save_error = MagicMock(name="NSError(save)")
    fake_save_error.__str__.return_value = "add-boom"
    _install_fake_contacts_for_modify(
        monkeypatch,
        fetched_contact=fetched,
        group_results=[fake_group],
        save_succeeds=False,
        fake_save_error=fake_save_error,
    )
    connector = ContactsConnector()
    with pytest.raises(ContactsError) as exc_info:
        connector._run_cn_add_contact_to_group("CONTACT-1", "GROUP-1")
    assert "CN add-member failed" in str(exc_info.value)
    assert "add-boom" in str(exc_info.value)


# ----- _run_applescript_remove_contact_from_group --------------------------


def test_remove_contact_from_group_invokes_applescript(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The remove path uses AppleScript (CN's `removeMember:fromGroup:`
    silently no-ops on macOS — verified during #18). Pre-flight via
    `_load_contact_and_group` for clean not-found errors, then run the
    AppleScript."""
    fetched = MagicMock(name="fetched_contact")
    fake_group = MagicMock(name="CNGroup_instance")
    _install_fake_contacts_for_modify(
        monkeypatch, fetched_contact=fetched, group_results=[fake_group]
    )
    connector = ContactsConnector()
    with patch.object(connector, "_run_applescript", return_value="") as mock:
        connector._run_applescript_remove_contact_from_group(
            "CONTACT-1:ABPerson", "GROUP-1:ABGroup"
        )
    (script,) = mock.call_args.args
    assert 'first person whose id is "CONTACT-1:ABPerson"' in script
    assert 'first group whose id is "GROUP-1:ABGroup"' in script
    assert "remove p from g" in script
    assert "\n  save\n" in script


def test_remove_contact_from_group_raises_not_found_when_contact_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-flight catches missing contact before AppleScript runs."""
    _install_fake_contacts_for_modify(
        monkeypatch,
        fetched_contact=None,
        group_results=[MagicMock(name="g")],
    )
    connector = ContactsConnector()
    with patch.object(connector, "_run_applescript") as mock:
        with pytest.raises(ContactsNotFoundError) as exc_info:
            connector._run_applescript_remove_contact_from_group(
                "BAD-CONTACT", "GROUP-1"
            )
    assert "Contact not found" in str(exc_info.value)
    mock.assert_not_called()


def test_remove_contact_from_group_raises_not_found_when_group_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetched = MagicMock(name="fetched_contact")
    _install_fake_contacts_for_modify(
        monkeypatch, fetched_contact=fetched, group_results=[]
    )
    connector = ContactsConnector()
    with patch.object(connector, "_run_applescript") as mock:
        with pytest.raises(ContactsNotFoundError) as exc_info:
            connector._run_applescript_remove_contact_from_group(
                "CONTACT-1", "BAD-GROUP"
            )
    assert "Group not found" in str(exc_info.value)
    mock.assert_not_called()


def test_remove_contact_from_group_translates_applescript_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If AppleScript surfaces a not-found error (e.g., a race), translate
    to ContactsNotFoundError so the server layer dispatches `not_found`."""
    fetched = MagicMock(name="fetched_contact")
    fake_group = MagicMock(name="CNGroup_instance")
    _install_fake_contacts_for_modify(
        monkeypatch, fetched_contact=fetched, group_results=[fake_group]
    )
    connector = ContactsConnector()
    err = ContactsAppleScriptError("Can't get person id \"X\"")
    with patch.object(connector, "_run_applescript", side_effect=err):
        with pytest.raises(ContactsNotFoundError) as exc_info:
            connector._run_applescript_remove_contact_from_group(
                "CONTACT-1", "GROUP-1"
            )
    assert "Contact or group not found" in str(exc_info.value)


def test_remove_contact_from_group_reraises_unrelated_applescript_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetched = MagicMock(name="fetched_contact")
    fake_group = MagicMock(name="CNGroup_instance")
    _install_fake_contacts_for_modify(
        monkeypatch, fetched_contact=fetched, group_results=[fake_group]
    )
    connector = ContactsConnector()
    err = ContactsAppleScriptError("permission denied or whatever")
    with patch.object(connector, "_run_applescript", side_effect=err):
        with pytest.raises(ContactsAppleScriptError) as exc_info:
            connector._run_applescript_remove_contact_from_group(
                "CONTACT-1", "GROUP-1"
            )
    assert "permission denied" in str(exc_info.value)


def test_remove_contact_from_group_escapes_adversarial_identifiers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defense-in-depth: identifiers containing AppleScript metacharacters
    are escaped before interpolation. Real CN identifiers never contain
    these characters, so this is a safety net for adversarial input."""
    fetched = MagicMock(name="fetched_contact")
    fake_group = MagicMock(name="CNGroup_instance")
    _install_fake_contacts_for_modify(
        monkeypatch, fetched_contact=fetched, group_results=[fake_group]
    )
    connector = ContactsConnector()
    with patch.object(connector, "_run_applescript", return_value="") as mock:
        connector._run_applescript_remove_contact_from_group(
            'c" & "x', 'g" & "y'
        )
    (script,) = mock.call_args.args
    assert 'first person whose id is "c\\" & \\"x"' in script
    assert 'first group whose id is "g\\" & \\"y"' in script


# ---------------------------------------------------------------------------
# _run_cn_export_vcard / _run_cn_import_vcard
# ---------------------------------------------------------------------------


def _install_fake_contacts_for_vcard(
    monkeypatch: pytest.MonkeyPatch,
    fetched_contacts: dict[str, MagicMock | None] | None = None,
    export_data: bytes | None = None,
    export_error: object | None = None,
    parsed_contacts: list[MagicMock] | None = None,
    parse_error: object | None = None,
    group_results: list[Any] | None = None,
    save_succeeds: bool = True,
    fake_save_error: Any | None = None,
) -> tuple[MagicMock, MagicMock, MagicMock, MagicMock]:
    """Install a fake `Contacts` module + store for vCard tests.

    ``fetched_contacts``: id → CNContact mock (or None for not-found). Used
    for export's per-id ``unifiedContactWithIdentifier_`` lookups.
    ``export_data`` / ``export_error``: drives ``dataWithContacts_error_``.
    ``parsed_contacts`` / ``parse_error``: drives ``contactsWithData_error_``.
    ``group_results``: list of CNGroup mocks (or empty list to simulate
    missing) returned by ``groupsMatchingPredicate_error_``.

    Returns ``(store, vcard_class, save_req, cn_save_request_class)``.
    """
    fake_contacts = types.ModuleType("Contacts")

    cn_vcard_class = MagicMock(name="CNContactVCardSerialization")
    cn_vcard_class.descriptorForRequiredKeys.return_value = "VCARD_DESCRIPTOR"
    cn_vcard_class.dataWithContacts_error_.return_value = (
        export_data,
        export_error,
    )
    cn_vcard_class.contactsWithData_error_.return_value = (
        parsed_contacts,
        parse_error,
    )
    fake_contacts.CNContactVCardSerialization = cn_vcard_class  # type: ignore[attr-defined]

    cn_save_request_class = MagicMock(name="CNSaveRequest")
    save_request_instance = MagicMock(name="save_request_instance")
    cn_save_request_class.alloc.return_value.init.return_value = (
        save_request_instance
    )
    fake_contacts.CNSaveRequest = cn_save_request_class  # type: ignore[attr-defined]

    cn_group_class = MagicMock(name="CNGroup")
    cn_group_class.predicateForGroupsWithIdentifiers_.return_value = "GP"
    fake_contacts.CNGroup = cn_group_class  # type: ignore[attr-defined]

    cn_store_class = MagicMock(name="CNContactStore")
    store_instance = MagicMock(name="store_instance")

    def _unified_lookup(
        ident: str, _keys: Any, _err: Any
    ) -> tuple[Any, Any]:
        if fetched_contacts is None:
            return (None, MagicMock(name="NSError(no_lookup_table)"))
        c = fetched_contacts.get(ident)
        if c is None:
            return (None, MagicMock(name="NSError(not_found)"))
        return (c, None)

    store_instance.unifiedContactWithIdentifier_keysToFetch_error_.side_effect = (
        _unified_lookup
    )
    if group_results is None:
        store_instance.groupsMatchingPredicate_error_.return_value = (
            [],
            None,
        )
    else:
        store_instance.groupsMatchingPredicate_error_.return_value = (
            group_results,
            None,
        )
    store_instance.executeSaveRequest_error_.return_value = (
        save_succeeds,
        fake_save_error,
    )
    cn_store_class.alloc.return_value.init.return_value = store_instance
    fake_contacts.CNContactStore = cn_store_class  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "Contacts", fake_contacts)
    return store_instance, cn_vcard_class, save_request_instance, cn_save_request_class


# ----- _run_cn_export_vcard -------------------------------------------------


def test_export_vcard_single_happy_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = MagicMock(name="CNContact(id-1)")
    store, vcard, _save, _ = _install_fake_contacts_for_vcard(
        monkeypatch,
        fetched_contacts={"id-1": fake},
        export_data=b"BEGIN:VCARD\nVERSION:3.0\nEND:VCARD\n",
    )
    connector = ContactsConnector()
    out = connector._run_cn_export_vcard(["id-1"])
    assert out == "BEGIN:VCARD\nVERSION:3.0\nEND:VCARD\n"
    vcard.dataWithContacts_error_.assert_called_once_with([fake], None)


def test_export_vcard_multi_preserves_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a = MagicMock(name="CNContact(a)")
    b = MagicMock(name="CNContact(b)")
    store, vcard, _save, _ = _install_fake_contacts_for_vcard(
        monkeypatch,
        fetched_contacts={"a": a, "b": b},
        export_data=b"BEGIN:VCARD\n...two contacts...\nEND:VCARD\n",
    )
    connector = ContactsConnector()
    out = connector._run_cn_export_vcard(["a", "b"])
    assert "two contacts" in out
    # Argument list passed to dataWithContacts is in input order.
    call_args = vcard.dataWithContacts_error_.call_args.args
    assert call_args[0] == [a, b]


def test_export_vcard_uses_descriptor_for_required_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = MagicMock(name="CNContact")
    store, vcard, _save, _ = _install_fake_contacts_for_vcard(
        monkeypatch,
        fetched_contacts={"id-1": fake},
        export_data=b"x",
    )
    connector = ContactsConnector()
    connector._run_cn_export_vcard(["id-1"])
    # Each per-id lookup uses the [descriptor] keys list.
    call_args = (
        store.unifiedContactWithIdentifier_keysToFetch_error_.call_args.args
    )
    assert call_args[0] == "id-1"
    assert call_args[1] == ["VCARD_DESCRIPTOR"]


def test_export_vcard_first_miss_aborts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First missing id raises before later fetches run."""
    a = MagicMock(name="CNContact(a)")
    store, vcard, _save, _ = _install_fake_contacts_for_vcard(
        monkeypatch,
        fetched_contacts={"a": a, "missing": None, "c": MagicMock()},
        export_data=b"x",
    )
    connector = ContactsConnector()
    with pytest.raises(ContactsNotFoundError) as exc_info:
        connector._run_cn_export_vcard(["a", "missing", "c"])
    assert "missing" in str(exc_info.value)
    # Serialization never reached.
    vcard.dataWithContacts_error_.assert_not_called()


def test_export_vcard_serialization_failure_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = MagicMock(name="CNContact")
    fake_err = MagicMock(name="NSError")
    fake_err.__str__.return_value = "ser-boom"
    _install_fake_contacts_for_vcard(
        monkeypatch,
        fetched_contacts={"id-1": fake},
        export_data=None,
        export_error=fake_err,
    )
    connector = ContactsConnector()
    with pytest.raises(ContactsError) as exc_info:
        connector._run_cn_export_vcard(["id-1"])
    assert "ser-boom" in str(exc_info.value)


# ----- _run_cn_import_vcard ------------------------------------------------


def _make_parsed_contact(label: str, new_id: str) -> MagicMock:
    parsed = MagicMock(name=f"CNContact(parsed-{label})")
    mutable = MagicMock(name=f"CNMutableContact(parsed-{label})")
    mutable.identifier.return_value = new_id
    parsed.mutableCopy.return_value = mutable
    return parsed


def test_import_vcard_single_happy_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parsed = _make_parsed_contact("solo", "NEW-1")
    store, vcard, save_req, _ = _install_fake_contacts_for_vcard(
        monkeypatch, parsed_contacts=[parsed]
    )
    connector = ContactsConnector()
    ids = connector._run_cn_import_vcard("BEGIN:VCARD...", group_identifier=None)
    assert ids == ["NEW-1"]
    save_req.addContact_toContainerWithIdentifier_.assert_called_once()
    save_req.addMember_toGroup_.assert_not_called()


def test_import_vcard_multi_preserves_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parsed_a = _make_parsed_contact("a", "NEW-A")
    parsed_b = _make_parsed_contact("b", "NEW-B")
    store, vcard, save_req, _ = _install_fake_contacts_for_vcard(
        monkeypatch, parsed_contacts=[parsed_a, parsed_b]
    )
    connector = ContactsConnector()
    ids = connector._run_cn_import_vcard("two-vcards", group_identifier=None)
    assert ids == ["NEW-A", "NEW-B"]
    assert save_req.addContact_toContainerWithIdentifier_.call_count == 2


def test_import_vcard_parse_failure_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_err = MagicMock(name="NSError")
    fake_err.__str__.return_value = "bad-input"
    _install_fake_contacts_for_vcard(
        monkeypatch, parsed_contacts=None, parse_error=fake_err
    )
    connector = ContactsConnector()
    with pytest.raises(ContactsError) as exc_info:
        connector._run_cn_import_vcard("garbage", group_identifier=None)
    assert "vCard parse failed" in str(exc_info.value)
    assert "bad-input" in str(exc_info.value)


def test_import_vcard_empty_parse_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Apple sometimes returns an empty list for syntactically valid but
    semantically empty input. Treat as parse failure."""
    _install_fake_contacts_for_vcard(monkeypatch, parsed_contacts=[])
    connector = ContactsConnector()
    with pytest.raises(ContactsError) as exc_info:
        connector._run_cn_import_vcard("BEGIN:VCARD", group_identifier=None)
    assert "No vCards found" in str(exc_info.value)


def test_import_vcard_group_not_found_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parsed = _make_parsed_contact("x", "NEW-1")
    store, _vcard, save_req, _ = _install_fake_contacts_for_vcard(
        monkeypatch, parsed_contacts=[parsed], group_results=[]
    )
    connector = ContactsConnector()
    with pytest.raises(ContactsNotFoundError) as exc_info:
        connector._run_cn_import_vcard(
            "BEGIN:VCARD", group_identifier="BAD-GROUP"
        )
    assert "Group not found" in str(exc_info.value)
    # Save never invoked.
    store.executeSaveRequest_error_.assert_not_called()


def test_import_vcard_with_group_adds_membership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parsed = _make_parsed_contact("x", "NEW-1")
    fake_group = MagicMock(name="CNGroup_instance")
    store, _vcard, save_req, _ = _install_fake_contacts_for_vcard(
        monkeypatch,
        parsed_contacts=[parsed],
        group_results=[fake_group],
    )
    connector = ContactsConnector()
    ids = connector._run_cn_import_vcard(
        "BEGIN:VCARD", group_identifier="GROUP-1"
    )
    assert ids == ["NEW-1"]
    save_req.addMember_toGroup_.assert_called_once()
    args = save_req.addMember_toGroup_.call_args.args
    assert args[1] is fake_group


def test_import_vcard_save_failure_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parsed = _make_parsed_contact("x", "NEW-1")
    fake_err = MagicMock(name="NSError")
    fake_err.__str__.return_value = "save-boom"
    _install_fake_contacts_for_vcard(
        monkeypatch,
        parsed_contacts=[parsed],
        save_succeeds=False,
        fake_save_error=fake_err,
    )
    connector = ContactsConnector()
    with pytest.raises(ContactsError) as exc_info:
        connector._run_cn_import_vcard("BEGIN:VCARD", group_identifier=None)
    assert "CN save failed" in str(exc_info.value)
    assert "save-boom" in str(exc_info.value)
