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
from collections.abc import Callable
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

    def _run_cn_unified_contact(
        self, identifier: str
    ) -> dict[str, Any] | None:
        """Fetch a single contact by identifier with the full P1 key set.

        Returns the serialized dict, or None if no contact matches.
        """
        from Contacts import (
            CNContactBirthdayKey,
            CNContactDepartmentNameKey,
            CNContactEmailAddressesKey,
            CNContactFamilyNameKey,
            CNContactGivenNameKey,
            CNContactJobTitleKey,
            CNContactMiddleNameKey,
            CNContactNamePrefixKey,
            CNContactNameSuffixKey,
            CNContactNicknameKey,
            CNContactOrganizationNameKey,
            CNContactPhoneNumbersKey,
            CNContactPostalAddressesKey,
            CNContactUrlAddressesKey,
            CNLabeledValue,
        )

        keys = [
            CNContactGivenNameKey,
            CNContactFamilyNameKey,
            CNContactMiddleNameKey,
            CNContactNamePrefixKey,
            CNContactNameSuffixKey,
            CNContactNicknameKey,
            CNContactOrganizationNameKey,
            CNContactJobTitleKey,
            CNContactDepartmentNameKey,
            CNContactPhoneNumbersKey,
            CNContactEmailAddressesKey,
            CNContactPostalAddressesKey,
            CNContactUrlAddressesKey,
            CNContactBirthdayKey,
        ]

        store = self._get_store()
        contact, _err = store.unifiedContactWithIdentifier_keysToFetch_error_(
            identifier, keys, None
        )
        if contact is None:
            return None
        return _serialize_contact(contact, CNLabeledValue)

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


# ---------------------------------------------------------------------------
# CNContact serializers (module-private)
# ---------------------------------------------------------------------------


def _serialize_contact(contact: Any, CNLabeledValue: Any) -> dict[str, Any]:
    return {
        "id": str(contact.identifier()),
        "given_name": str(contact.givenName()),
        "family_name": str(contact.familyName()),
        "middle_name": str(contact.middleName()),
        "name_prefix": str(contact.namePrefix()),
        "name_suffix": str(contact.nameSuffix()),
        "nickname": str(contact.nickname()),
        "organization": str(contact.organizationName()),
        "job_title": str(contact.jobTitle()),
        "department": str(contact.departmentName()),
        "phones": _serialize_labeled_values(
            contact.phoneNumbers(),
            CNLabeledValue,
            lambda v: {"value": str(v.stringValue())},
        ),
        "emails": _serialize_labeled_values(
            contact.emailAddresses(),
            CNLabeledValue,
            lambda v: {"value": str(v)},
        ),
        "urls": _serialize_labeled_values(
            contact.urlAddresses(),
            CNLabeledValue,
            lambda v: {"value": str(v)},
        ),
        "postal_addresses": _serialize_labeled_values(
            contact.postalAddresses(),
            CNLabeledValue,
            _serialize_postal_address,
        ),
        "birthday": _serialize_birthday(contact.birthday()),
    }


def _serialize_labeled_values(
    items: Any,
    CNLabeledValue: Any,
    value_fn: Callable[[Any], dict[str, Any]],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if items is None:
        return out
    for item in items:
        raw = item.label()
        if raw is None:
            label_raw = ""
            human = ""
        else:
            label_raw = str(raw)
            human = str(CNLabeledValue.localizedStringForLabel_(raw))
        entry: dict[str, Any] = {"label_raw": label_raw, "label": human}
        entry.update(value_fn(item.value()))
        out.append(entry)
    return out


def _serialize_postal_address(addr: Any) -> dict[str, str]:
    return {
        "street": str(addr.street()),
        "sub_locality": str(addr.subLocality()),
        "city": str(addr.city()),
        "sub_administrative_area": str(addr.subAdministrativeArea()),
        "state": str(addr.state()),
        "postal_code": str(addr.postalCode()),
        "country": str(addr.country()),
        "iso_country_code": str(addr.ISOCountryCode()),
    }


def _serialize_birthday(date_components: Any) -> dict[str, int] | None:
    """Serialize NSDateComponents → {year?, month?, day?} or None.

    Apple uses NSNotFound (≈ NSIntegerMax) for unset components and lets
    users set a birthday without a year. Filter via 0 < val < 10_000.
    """
    if date_components is None:
        return None

    def _safe(v: Any) -> int | None:
        try:
            n = int(v)
        except (TypeError, ValueError):
            return None
        return n if 0 < n < 10_000 else None

    out: dict[str, int] = {}
    for attr, key in (("year", "year"), ("month", "month"), ("day", "day")):
        v = _safe(getattr(date_components, attr)())
        if v is not None:
            out[key] = v
    return out or None
