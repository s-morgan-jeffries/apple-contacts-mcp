"""ContactsConnector: backend layer for Apple Contacts operations.

All external I/O — every `osascript` invocation and every Contacts.framework
call — flows through helpers on this class. Unit tests mock at these helpers
(`_run_applescript`, `_run_cn_*`); integration tests hit the real boundaries.

See `docs/research/contacts-api-gap-analysis.md` §7 for the boundary spec.
"""

from __future__ import annotations

import logging
import subprocess
import threading
from typing import TYPE_CHECKING, Any

from .exceptions import (
    ContactsAppleScriptError,
    ContactsAuthorizationError,
    ContactsError,
    ContactsTimeoutError,
)

if TYPE_CHECKING:
    from Contacts import CNContactStore  # pragma: no cover

logger = logging.getLogger(__name__)

_CN_AUTHORIZATION_STATUS: dict[int, str] = {
    0: "notDetermined",
    1: "restricted",
    2: "denied",
    3: "authorized",
    4: "limited",  # macOS 14+
}


class ContactsConnector:
    """Backend connector for Apple Contacts operations."""

    def __init__(self, timeout: float = 10.0) -> None:
        self.timeout = timeout
        self._store: CNContactStore | None = None

    # ------------------------------------------------------------------
    # AppleScript boundary
    # ------------------------------------------------------------------

    def _run_applescript(self, script: str) -> str:
        """Execute an AppleScript via `osascript` and return stripped stdout.

        Raises:
            ContactsTimeoutError: subprocess exceeded `self.timeout`.
            ContactsAppleScriptError: non-zero exit or any other failure.
        """
        try:
            result = subprocess.run(
                ["/usr/bin/osascript", "-"],
                input=script,
                text=True,
                capture_output=True,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise ContactsTimeoutError(
                f"osascript timed out after {self.timeout}s"
            ) from exc
        except Exception as exc:
            raise ContactsAppleScriptError(f"osascript failed: {exc}") from exc

        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            logger.error("AppleScript error: %s", stderr)
            raise ContactsAppleScriptError(stderr or "osascript exited non-zero")

        return result.stdout.strip()

    # ------------------------------------------------------------------
    # Contacts.framework boundary
    # ------------------------------------------------------------------

    def _get_store(self) -> CNContactStore:
        """Return the lazily-initialized CNContactStore singleton.

        PyObjC import is deferred so unit tests that mock `_run_cn_*` never
        trigger the import.
        """
        if self._store is None:
            from Contacts import CNContactStore

            self._store = CNContactStore.alloc().init()
        return self._store

    def _run_cn_authorization_status(self) -> str:
        """Return the current TCC status string for the Contacts entity.

        Wraps the synchronous class method
        +[CNContactStore authorizationStatusForEntityType:]. No permission
        is required to call this; it's a status getter.
        """
        from Contacts import CNContactStore, CNEntityTypeContacts

        raw = int(
            CNContactStore.authorizationStatusForEntityType_(CNEntityTypeContacts)
        )
        status = _CN_AUTHORIZATION_STATUS.get(raw)
        if status is None:
            logger.warning("Unknown CN authorization status: %d", raw)
            return "notDetermined"
        return status

    def _run_cn_enumerate_contacts(
        self, offset: int, limit: int
    ) -> list[dict[str, str]]:
        """Enumerate contacts and return basic-field dicts.

        Implements pagination inside the enumeration callback: skip the
        first `offset` contacts, then accumulate up to `limit` and signal
        the framework to short-circuit via `stop_ptr[0] = True`. Without
        the stop flag we'd pay the full O(N) walk even for a 50-element
        page.
        """
        from Contacts import (
            CNContactFamilyNameKey,
            CNContactFetchRequest,
            CNContactGivenNameKey,
            CNContactIdentifierKey,
            CNContactOrganizationNameKey,
        )

        keys = [
            CNContactIdentifierKey,
            CNContactGivenNameKey,
            CNContactFamilyNameKey,
            CNContactOrganizationNameKey,
        ]
        req = CNContactFetchRequest.alloc().initWithKeysToFetch_(keys)

        contacts: list[dict[str, str]] = []
        skipped = 0

        def _collect(contact: Any, stop_ptr: Any) -> None:
            nonlocal skipped
            if skipped < offset:
                skipped += 1
                return
            if len(contacts) >= limit:
                stop_ptr[0] = True
                return
            contacts.append(
                {
                    "id": str(contact.identifier()),
                    "given_name": str(contact.givenName()),
                    "family_name": str(contact.familyName()),
                    "organization": str(contact.organizationName()),
                }
            )

        store = self._get_store()
        ok, err = store.enumerateContactsWithFetchRequest_error_usingBlock_(
            req, None, _collect
        )
        if not ok:
            raise ContactsError(f"CN enumerate failed: {err}")
        return contacts

    def _run_cn_request_access(self) -> bool:
        """Request TCC access to the Contacts entity. First `_run_cn_*` helper.

        Bridges CN's async completion handler to a synchronous call via a
        `threading.Event`. The callback is invoked on a CN-internal queue.

        Returns:
            True if access granted, False if explicitly denied.

        Raises:
            ContactsAuthorizationError: TCC returned an NSError.
            ContactsTimeoutError: completion handler did not fire within
                `self.timeout`.
        """
        from Contacts import CNEntityTypeContacts

        store = self._get_store()
        done = threading.Event()
        result: dict[str, Any] = {"granted": False, "error": None}

        def _callback(granted: bool, error: object | None) -> None:
            result["granted"] = bool(granted)
            result["error"] = error
            done.set()

        store.requestAccessForEntityType_completionHandler_(
            CNEntityTypeContacts, _callback
        )

        if not done.wait(timeout=self.timeout):
            raise ContactsTimeoutError(
                f"CNContactStore.requestAccess did not complete within {self.timeout}s"
            )

        if result["error"] is not None:
            raise ContactsAuthorizationError(str(result["error"]))

        return bool(result["granted"])
