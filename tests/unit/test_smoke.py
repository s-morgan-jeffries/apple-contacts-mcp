"""Smoke tests: every module imports and the version is what we expect.

These exist so the bootstrap skeleton produces ≥90% statement coverage with
no real logic written yet. They get replaced by feature-specific tests as
modules grow.
"""

from apple_contacts_mcp import __version__
from apple_contacts_mcp.contacts_connector import ContactsConnector
from apple_contacts_mcp.exceptions import ContactsAuthorizationError, ContactsError
from apple_contacts_mcp.security import sanitize_input


def test_version() -> None:
    assert __version__ == "0.0.0"


def test_connector_instantiates() -> None:
    connector = ContactsConnector()
    assert connector is not None


def test_exceptions_hierarchy() -> None:
    assert issubclass(ContactsAuthorizationError, ContactsError)
    assert issubclass(ContactsError, Exception)


def test_sanitize_input_passthrough() -> None:
    assert sanitize_input("hello") == "hello"


def test_server_module_imports() -> None:
    from apple_contacts_mcp import server

    assert server.mcp is not None
    assert callable(server.main)


def test_utils_module_imports() -> None:
    from apple_contacts_mcp import utils

    assert utils is not None
