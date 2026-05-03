"""Custom exceptions for Apple Contacts MCP operations."""


class ContactsError(Exception):
    """Base exception for Contacts operations."""

    pass


class ContactsAuthorizationError(ContactsError):
    """TCC authorization not granted for the Contacts data class."""

    pass


class ContactsAppleScriptError(ContactsError):
    """`osascript` exited non-zero or emitted a parse/runtime error."""

    pass


class ContactsTimeoutError(ContactsError):
    """A subprocess or CN async operation exceeded its timeout."""

    pass


class ContactsNotFoundError(ContactsError):
    """A referenced CN object (contact or group) was not found."""

    pass
