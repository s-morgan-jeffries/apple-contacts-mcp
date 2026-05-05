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
    ContactsNotFoundError,
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
                # Apple's `BOOL *stop` arrives via PyObjC as either a
                # mutable 1-element sequence or None depending on the
                # selector's metadata — for `enumerateContactsWith...`
                # it's None in practice. Guard defensively; if we can't
                # short-circuit we just keep returning early (the
                # framework still walks the rest, but we don't pay the
                # serialization cost).
                if stop_ptr is not None:
                    try:
                        stop_ptr[0] = True
                    except (TypeError, IndexError):
                        pass
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

    def _run_cn_fetch_group(self, identifier: str) -> Any | None:
        """Fetch a CNGroup by its identifier.

        Returns the CNGroup object (PyObjC-typed, not serialized) or None
        if no group matches. Used by destructive tools that need to
        add/remove members — they want the live object to feed into
        CNSaveRequest.
        """
        from Contacts import CNGroup

        pred = CNGroup.predicateForGroupsWithIdentifiers_([identifier])
        store = self._get_store()
        results, err = store.groupsMatchingPredicate_error_(pred, None)
        if results is None:
            raise ContactsError(f"CN group fetch failed: {err}")
        if len(results) == 0:
            return None
        return results[0]

    def _run_cn_create_contact(
        self, fields: dict[str, Any], group_identifier: str | None
    ) -> str:
        """Create a contact in the default container, optionally also
        adding it to a group, in a single atomic CNSaveRequest.

        Returns the new contact's CN identifier (CN populates the
        CNMutableContact's identifier in-place after save).
        """
        from Contacts import CNSaveRequest

        mutable = _build_mutable_contact(fields)

        group = None
        if group_identifier is not None:
            group = self._run_cn_fetch_group(group_identifier)
            if group is None:
                raise ContactsNotFoundError(
                    f"Group not found: {group_identifier!r}"
                )

        save_req = CNSaveRequest.alloc().init()
        save_req.addContact_toContainerWithIdentifier_(mutable, None)
        if group is not None:
            save_req.addMember_toGroup_(mutable, group)

        store = self._get_store()
        ok, err = store.executeSaveRequest_error_(save_req, None)
        if not ok:
            raise ContactsError(f"CN save failed: {err}")

        return str(mutable.identifier())

    def _run_cn_update_contact(
        self, identifier: str, fields: dict[str, Any]
    ) -> str:
        """Apply partial field updates to an existing contact and save.

        Fetches the contact with the full P1 key set (so any subset of
        fields is writable without CNPropertyNotFetchedException), takes
        a mutable copy, applies only the keys present in ``fields``, and
        saves via CNSaveRequest.updateContact_.

        Returns the identifier (echoes input) for response symmetry.
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
            CNSaveRequest,
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
            raise ContactsNotFoundError(
                f"Contact not found: {identifier!r}"
            )

        mutable = contact.mutableCopy()
        _apply_update_fields(mutable, fields)

        save_req = CNSaveRequest.alloc().init()
        save_req.updateContact_(mutable)
        ok, err = store.executeSaveRequest_error_(save_req, None)
        if not ok:
            raise ContactsError(f"CN update failed: {err}")

        return identifier

    def _run_cn_delete_contact(self, identifier: str) -> str:
        """Delete an existing contact. Returns the identifier (echo)."""
        from Contacts import CNContactIdentifierKey, CNSaveRequest

        store = self._get_store()
        contact, _err = store.unifiedContactWithIdentifier_keysToFetch_error_(
            identifier, [CNContactIdentifierKey], None
        )
        if contact is None:
            raise ContactsNotFoundError(
                f"Contact not found: {identifier!r}"
            )

        mutable = contact.mutableCopy()
        save_req = CNSaveRequest.alloc().init()
        save_req.deleteContact_(mutable)
        ok, err = store.executeSaveRequest_error_(save_req, None)
        if not ok:
            raise ContactsError(f"CN delete failed: {err}")

        return identifier

    def _run_cn_search_contacts(
        self, query: str, limit: int
    ) -> list[dict[str, str]]:
        """Search contacts by name predicate, returning basic-field dicts.

        Predicate execution is offloaded to the framework — fast even on
        large address books. We slice to ``limit`` *while serializing*
        so we don't pay full O(N_matches) for entries we'll throw away.
        """
        from Contacts import (
            CNContact,
            CNContactFamilyNameKey,
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
        pred = CNContact.predicateForContactsMatchingName_(query)
        store = self._get_store()
        results, err = store.unifiedContactsMatchingPredicate_keysToFetch_error_(
            pred, keys, None
        )
        if results is None:
            raise ContactsError(f"CN search failed: {err}")

        out: list[dict[str, str]] = []
        for contact in results:
            if len(out) >= limit:
                break
            out.append(
                {
                    "id": str(contact.identifier()),
                    "given_name": str(contact.givenName()),
                    "family_name": str(contact.familyName()),
                    "organization": str(contact.organizationName()),
                }
            )
        return out

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
# CNContact builders (module-private; inverse of serializers below)
# ---------------------------------------------------------------------------


def _build_mutable_contact(fields: dict[str, Any]) -> Any:
    """Build a CNMutableContact from a dict shaped like _serialize_contact's
    output (minus ``id``). Only sets fields the caller provided — empty
    strings and missing keys leave the corresponding CN property unset.
    """
    from Contacts import (
        CNLabeledValue,
        CNMutableContact,
        CNPhoneNumber,
    )
    from Foundation import NSDateComponents

    c = CNMutableContact.alloc().init()

    if v := fields.get("given_name"):
        c.setGivenName_(v)
    if v := fields.get("family_name"):
        c.setFamilyName_(v)
    if v := fields.get("middle_name"):
        c.setMiddleName_(v)
    if v := fields.get("name_prefix"):
        c.setNamePrefix_(v)
    if v := fields.get("name_suffix"):
        c.setNameSuffix_(v)
    if v := fields.get("nickname"):
        c.setNickname_(v)
    if v := fields.get("organization"):
        c.setOrganizationName_(v)
    if v := fields.get("job_title"):
        c.setJobTitle_(v)
    if v := fields.get("department"):
        c.setDepartmentName_(v)

    if phones := fields.get("phones"):
        c.setPhoneNumbers_(
            [
                CNLabeledValue.labeledValueWithLabel_value_(
                    p.get("label_raw", ""),
                    CNPhoneNumber.phoneNumberWithStringValue_(p["value"]),
                )
                for p in phones
            ]
        )
    if emails := fields.get("emails"):
        c.setEmailAddresses_(
            [
                CNLabeledValue.labeledValueWithLabel_value_(
                    e.get("label_raw", ""), e["value"]
                )
                for e in emails
            ]
        )
    if urls := fields.get("urls"):
        c.setUrlAddresses_(
            [
                CNLabeledValue.labeledValueWithLabel_value_(
                    u.get("label_raw", ""), u["value"]
                )
                for u in urls
            ]
        )
    if postal := fields.get("postal_addresses"):
        c.setPostalAddresses_(
            [
                CNLabeledValue.labeledValueWithLabel_value_(
                    a.get("label_raw", ""), _build_mutable_postal_address(a)
                )
                for a in postal
            ]
        )

    if bday := fields.get("birthday"):
        c.setBirthday_(_build_birthday_components(bday, NSDateComponents))

    return c


def _build_mutable_postal_address(a: dict[str, str]) -> Any:
    from Contacts import CNMutablePostalAddress

    addr = CNMutablePostalAddress.alloc().init()
    if v := a.get("street"):
        addr.setStreet_(v)
    if v := a.get("sub_locality"):
        addr.setSubLocality_(v)
    if v := a.get("city"):
        addr.setCity_(v)
    if v := a.get("sub_administrative_area"):
        addr.setSubAdministrativeArea_(v)
    if v := a.get("state"):
        addr.setState_(v)
    if v := a.get("postal_code"):
        addr.setPostalCode_(v)
    if v := a.get("country"):
        addr.setCountry_(v)
    if v := a.get("iso_country_code"):
        addr.setISOCountryCode_(v)
    return addr


def _build_birthday_components(
    bday: dict[str, int], NSDateComponents: Any
) -> Any:
    dc = NSDateComponents.alloc().init()
    if y := bday.get("year"):
        dc.setYear_(y)
    if m := bday.get("month"):
        dc.setMonth_(m)
    if d := bday.get("day"):
        dc.setDay_(d)
    return dc


_UPDATE_SIMPLE_SETTERS: list[tuple[str, str]] = [
    ("given_name", "setGivenName_"),
    ("family_name", "setFamilyName_"),
    ("middle_name", "setMiddleName_"),
    ("name_prefix", "setNamePrefix_"),
    ("name_suffix", "setNameSuffix_"),
    ("nickname", "setNickname_"),
    ("organization", "setOrganizationName_"),
    ("job_title", "setJobTitle_"),
    ("department", "setDepartmentName_"),
]


def _apply_update_fields(mutable: Any, fields: dict[str, Any]) -> None:
    """Apply only the fields present in ``fields`` to a CNMutableContact.

    Uses **presence** check (``"key" in fields``), not truthy: passing
    ``given_name=""`` explicitly clears the field; omitting ``given_name``
    leaves it untouched. Multi-valued fields (phones / emails / urls /
    postal_addresses) follow REST-PUT semantics — the supplied list
    replaces the existing list entirely.
    """
    from Contacts import CNLabeledValue, CNPhoneNumber

    for key, setter in _UPDATE_SIMPLE_SETTERS:
        if key in fields:
            getattr(mutable, setter)(fields[key])

    if "phones" in fields:
        mutable.setPhoneNumbers_(
            [
                CNLabeledValue.labeledValueWithLabel_value_(
                    p.get("label_raw", ""),
                    CNPhoneNumber.phoneNumberWithStringValue_(p["value"]),
                )
                for p in fields["phones"]
            ]
        )
    if "emails" in fields:
        mutable.setEmailAddresses_(
            [
                CNLabeledValue.labeledValueWithLabel_value_(
                    e.get("label_raw", ""), e["value"]
                )
                for e in fields["emails"]
            ]
        )
    if "urls" in fields:
        mutable.setUrlAddresses_(
            [
                CNLabeledValue.labeledValueWithLabel_value_(
                    u.get("label_raw", ""), u["value"]
                )
                for u in fields["urls"]
            ]
        )
    if "postal_addresses" in fields:
        mutable.setPostalAddresses_(
            [
                CNLabeledValue.labeledValueWithLabel_value_(
                    a.get("label_raw", ""), _build_mutable_postal_address(a)
                )
                for a in fields["postal_addresses"]
            ]
        )

    if "birthday" in fields:
        from Foundation import NSDateComponents

        mutable.setBirthday_(
            _build_birthday_components(fields["birthday"], NSDateComponents)
        )


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
