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
