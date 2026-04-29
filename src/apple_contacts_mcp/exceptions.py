"""Custom exceptions for Apple Contacts MCP operations."""


class ContactsError(Exception):
    """Base exception for Contacts operations."""

    pass


class ContactsAuthorizationError(ContactsError):
    """TCC authorization not granted for the Contacts data class."""

    pass
