"""ContactsConnector: backend layer for Apple Contacts operations.

All external I/O — every `osascript` invocation and every Contacts.framework
call — flows through helpers on this class. Unit tests mock at these helpers
(`_run_applescript`, `_run_cn_*`); integration tests hit the real boundaries.

See `docs/research/contacts-api-gap-analysis.md` §7 for the boundary spec.
"""

from __future__ import annotations

import logging
import re
import subprocess
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal

from .exceptions import (
    ContactsAppleScriptError,
    ContactsAuthorizationError,
    ContactsError,
    ContactsNotFoundError,
    ContactsTimeoutError,
)
from .utils import escape_applescript_string, label_to_apple_token

if TYPE_CHECKING:
    from Contacts import CNContactStore  # pragma: no cover

logger = logging.getLogger(__name__)

SearchField = Literal["name", "phone", "email", "organization"]


# AppleScript error patterns that mean "the contact doesn't exist". Translated
# to ContactsNotFoundError at the connector boundary so the server layer can
# dispatch a clean not_found response. Pattern set is empirical — tighten or
# expand as integration tests surface new wordings. The "Invalid index" wording
# comes back from `whose id is "X"` lookups when no match exists; the curly
# apostrophe in "Can't" is what AppleScript actually emits, not a bug.
_APPLESCRIPT_NOT_FOUND_PATTERN = re.compile(
    r"Can(?:'|’)t get|Invalid index", re.IGNORECASE
)


def _is_not_found_error(exc: ContactsAppleScriptError) -> bool:
    return bool(_APPLESCRIPT_NOT_FOUND_PATTERN.search(str(exc)))


_CN_AUTHORIZATION_STATUS: dict[int, str] = {
    0: "notDetermined",
    1: "restricted",
    2: "denied",
    3: "authorized",
    4: "limited",  # macOS 14+
}

# CNContainerType raw values (per the Apple SDK). Verified empirically against
# macOS 26.3.1: even iCloud comes back as cardDAV (3), since iCloud's contact
# sync is CardDAV under the hood. Local (1) is the legacy "On My Mac" account.
_CN_CONTAINER_TYPE: dict[int, str] = {
    1: "local",
    2: "exchange",
    3: "cardDAV",
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

    def _run_applescript_read_note(self, identifier: str) -> str:
        """Read the ``note`` field of a contact via AppleScript.

        ``note`` is entitlement-gated in Contacts.framework, so this is the
        only path. Returns the note text, or ``""`` if the contact has no
        note set. Raises ``ContactsNotFoundError`` if the contact doesn't
        exist; other AppleScript errors propagate as
        ``ContactsAppleScriptError``.

        ``identifier`` must be the full CN identifier (the ``<UUID>:ABPerson``
        form returned by ``unifiedContactsMatchingPredicate:`` and friends).
        Bare-UUID input raises ``ContactsNotFoundError`` because AppleScript's
        ``id of person`` includes the ``:ABPerson`` suffix and ``whose id is
        "<bare-uuid>"`` won't match. Verified empirically; do not strip.

        The identifier is escaped via ``escape_applescript_string`` before
        interpolation. CN-issued identifiers contain only hex digits, hyphens,
        colons, and letters — none of which the helper transforms — so this
        is a no-op in normal use and a defense-in-depth boundary for
        adversarial input.
        """
        escaped_id = escape_applescript_string(identifier)
        script = (
            'tell application "Contacts"\n'
            f'  set p to first person whose id is "{escaped_id}"\n'
            "  if note of p is missing value then\n"
            '    return ""\n'
            "  else\n"
            "    return note of p\n"
            "  end if\n"
            "end tell"
        )
        try:
            return self._run_applescript(script)
        except ContactsAppleScriptError as exc:
            if _is_not_found_error(exc):
                raise ContactsNotFoundError(
                    f"Contact not found: {identifier!r}"
                ) from exc
            raise

    def _run_applescript_write_note(
        self, identifier: str, note: str
    ) -> None:
        """Write (replace) the ``note`` field of a contact via AppleScript.

        ``note=""`` clears the note. The trailing ``save`` is load-bearing —
        without it, edits sit in Contacts.app's in-memory state and don't
        persist to disk. Raises ``ContactsNotFoundError`` if the contact
        doesn't exist. ``identifier`` must be the full ``<UUID>:ABPerson``
        CN identifier (see ``_run_applescript_read_note`` for why).
        """
        escaped_note = escape_applescript_string(note)
        escaped_id = escape_applescript_string(identifier)
        script = (
            'tell application "Contacts"\n'
            f'  set p to first person whose id is "{escaped_id}"\n'
            f'  set note of p to "{escaped_note}"\n'
            "  save\n"
            "end tell"
        )
        try:
            self._run_applescript(script)
        except ContactsAppleScriptError as exc:
            if _is_not_found_error(exc):
                raise ContactsNotFoundError(
                    f"Contact not found: {identifier!r}"
                ) from exc
            raise

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

    def _run_cn_list_containers(self) -> list[dict[str, Any]]:
        """Enumerate all CN containers (multi-account: iCloud, Gmail, etc.).

        Returns ``[{"id": str, "name": str, "type": str, "is_default":
        bool}, ...]``. ``type`` is one of ``"local"`` / ``"exchange"`` /
        ``"cardDAV"`` (mapped from the CN integer enum). ``is_default``
        flags the container that ``defaultContainerIdentifier`` points
        at — typically iCloud on macOS.

        Container enumeration is a single CN call (no N+1 like
        ``_run_cn_list_groups``). Empty store → empty list.
        """
        store = self._get_store()
        containers, err = store.containersMatchingPredicate_error_(None, None)
        if containers is None:
            raise ContactsError(f"CN containers fetch failed: {err}")

        default_id = str(store.defaultContainerIdentifier())
        out: list[dict[str, Any]] = []
        for c in containers:
            cid = str(c.identifier())
            raw_type = int(c.type())
            out.append(
                {
                    "id": cid,
                    "name": str(c.name()),
                    "type": _CN_CONTAINER_TYPE.get(
                        raw_type, f"unknown({raw_type})"
                    ),
                    "is_default": cid == default_id,
                }
            )
        return out

    def _run_cn_list_groups(self) -> list[dict[str, str]]:
        """Enumerate all groups across all containers.

        Returns ``[{"id": str, "name": str, "container_id": str}, ...]``.
        Order is not guaranteed (matches Apple's native enumeration).
        Empty store → empty list.

        Note: this is N+1 — one ``containersMatchingPredicate:`` call per
        group to resolve ``container_id``, since ``CNGroup`` has no public
        container accessor. Acceptable because typical users have <20
        groups; revisit if this becomes a bottleneck.
        """
        from Contacts import CNContainer

        store = self._get_store()
        groups, err = store.groupsMatchingPredicate_error_(None, None)
        if groups is None:
            raise ContactsError(f"CN groups fetch failed: {err}")

        out: list[dict[str, str]] = []
        for g in groups:
            cpred = CNContainer.predicateForContainerOfGroupWithIdentifier_(
                g.identifier()
            )
            containers, cerr = store.containersMatchingPredicate_error_(
                cpred, None
            )
            if containers is None:
                raise ContactsError(f"CN container fetch failed: {cerr}")
            # Defensive: Apple shouldn't return a group without a container,
            # but the API contract doesn't forbid it — fall back to "".
            container_id = (
                str(containers[0].identifier())
                if len(containers) > 0
                else ""
            )
            out.append(
                {
                    "id": str(g.identifier()),
                    "name": str(g.name()),
                    "container_id": container_id,
                }
            )
        return out

    def _run_cn_contacts_in_group(
        self, group_id: str, limit: int
    ) -> list[dict[str, str]]:
        """Return up to ``limit`` contacts whose membership includes
        ``group_id``, as 4-field basic dicts.

        No pre-flight — Apple's predicate returns ``[]`` for unknown
        ``group_id``s without error. Existence checking is the server-tool
        layer's job (``get_contacts_in_group`` calls
        ``_run_cn_fetch_group`` first to dispatch ``not_found``).
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
        pred = CNContact.predicateForContactsInGroupWithIdentifier_(group_id)
        store = self._get_store()
        results, err = store.unifiedContactsMatchingPredicate_keysToFetch_error_(
            pred, keys, None
        )
        if results is None:
            raise ContactsError(f"CN group-members fetch failed: {err}")

        out: list[dict[str, str]] = []
        for c in results:
            if len(out) >= limit:
                break
            out.append(
                {
                    "id": str(c.identifier()),
                    "given_name": str(c.givenName()),
                    "family_name": str(c.familyName()),
                    "organization": str(c.organizationName()),
                }
            )
        return out

    def _run_cn_create_contact(
        self,
        fields: dict[str, Any],
        group_identifier: str | None,
        container_identifier: str | None = None,
    ) -> str:
        """Create a contact in the specified container, optionally also
        adding it to a group, in a single atomic CNSaveRequest.

        ``container_identifier=None`` writes to the default container
        (typically iCloud); pass an explicit container UUID to target a
        specific account. No pre-flight existence check — if the
        identifier doesn't match any container, CN's
        ``executeSaveRequest:error:`` raises and we surface it as
        ``ContactsError`` (the server layer dispatches ``unknown``).
        Behavior verified empirically; see
        ``docs/research/multi-container-write-decision.md``.

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
        save_req.addContact_toContainerWithIdentifier_(
            mutable, container_identifier
        )
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

    def _run_cn_export_vcard(self, identifiers: list[str]) -> str:
        """Export one or more contacts as vCard 3.0 text.

        Atomic: any single missing identifier raises ``ContactsNotFoundError``
        before serialization runs. Returned text is exactly what Apple's
        serializer emits — vCard 3.0 with Apple's specific quirks
        (lowercase ``type=`` parameters, ``X-APPLE-OMIT-YEAR=1604`` for
        year-less BDAYs, NOTE field omitted because it's entitlement-gated).
        See ``docs/research/vcard-version-decision.md`` for the full
        rationale.
        """
        from Contacts import CNContactVCardSerialization

        descriptor = CNContactVCardSerialization.descriptorForRequiredKeys()
        store = self._get_store()
        contacts = []
        for ident in identifiers:
            c, _err = store.unifiedContactWithIdentifier_keysToFetch_error_(
                ident, [descriptor], None
            )
            if c is None:
                raise ContactsNotFoundError(
                    f"Contact not found: {ident!r}"
                )
            contacts.append(c)
        data, err = CNContactVCardSerialization.dataWithContacts_error_(
            contacts, None
        )
        if data is None:
            raise ContactsError(f"vCard serialization failed: {err}")
        return bytes(data).decode("utf-8")

    def _run_cn_import_vcard(
        self, vcard_text: str, group_identifier: str | None
    ) -> list[str]:
        """Parse a vCard payload (3.0 or 4.0 input both accepted) and persist
        as new contacts via a single CNSaveRequest. Optionally adds each
        new contact to a group. Returns the list of new CN identifiers
        in input order.

        Atomic: parse failure, empty input, group-not-found, or save
        failure aborts the whole operation. A multi-contact vCard is
        committed as one unit.
        """
        from Contacts import CNContactVCardSerialization, CNSaveRequest

        data = vcard_text.encode("utf-8")
        parsed, err = CNContactVCardSerialization.contactsWithData_error_(
            data, None
        )
        if parsed is None:
            raise ContactsError(f"vCard parse failed: {err}")
        if len(parsed) == 0:
            raise ContactsError("No vCards found in input")

        group = None
        if group_identifier is not None:
            group = self._run_cn_fetch_group(group_identifier)
            if group is None:
                raise ContactsNotFoundError(
                    f"Group not found: {group_identifier!r}"
                )

        save_req = CNSaveRequest.alloc().init()
        mutables = []
        for c in parsed:
            m = c.mutableCopy()
            save_req.addContact_toContainerWithIdentifier_(m, None)
            if group is not None:
                save_req.addMember_toGroup_(m, group)
            mutables.append(m)

        store = self._get_store()
        ok, err = store.executeSaveRequest_error_(save_req, None)
        if not ok:
            raise ContactsError(f"CN save failed: {err}")
        return [str(m.identifier()) for m in mutables]

    def _load_contact_and_group(
        self, contact_identifier: str, group_identifier: str
    ) -> tuple[Any, Any]:
        """Fetch live ``(mutable_contact, group)`` pair for membership writes.

        Raises ``ContactsNotFoundError`` with disambiguating text — "Contact
        not found: ..." vs "Group not found: ..." — so the server-tool layer
        can dispatch ``not_found`` without inspecting the message.

        Contact is fetched with the minimal key set (just identifier) since
        we only need identity for the save request.
        """
        from Contacts import CNContactIdentifierKey

        store = self._get_store()
        contact, _err = store.unifiedContactWithIdentifier_keysToFetch_error_(
            contact_identifier, [CNContactIdentifierKey], None
        )
        if contact is None:
            raise ContactsNotFoundError(
                f"Contact not found: {contact_identifier!r}"
            )
        mutable = contact.mutableCopy()

        group = self._run_cn_fetch_group(group_identifier)
        if group is None:
            raise ContactsNotFoundError(
                f"Group not found: {group_identifier!r}"
            )
        return mutable, group

    def _run_cn_add_contact_to_group(
        self, contact_identifier: str, group_identifier: str
    ) -> None:
        """Add an existing contact to an existing group.

        Empirical behavior on duplicate-add is documented by the
        integration probe in ``tests/integration/test_group_membership.py``.
        """
        from Contacts import CNSaveRequest

        mutable, group = self._load_contact_and_group(
            contact_identifier, group_identifier
        )
        save_req = CNSaveRequest.alloc().init()
        save_req.addMember_toGroup_(mutable, group)
        store = self._get_store()
        ok, err = store.executeSaveRequest_error_(save_req, None)
        if not ok:
            raise ContactsError(f"CN add-member failed: {err}")

    def _run_applescript_remove_contact_from_group(
        self, contact_identifier: str, group_identifier: str
    ) -> None:
        """Remove a contact from a group via AppleScript.

        **AppleScript fallback (not CN):** ``CNSaveRequest.removeMember:
        fromGroup:`` silently no-ops despite reporting ``ok=True`` —
        empirically verified during #18. AppleScript's ``remove p from
        g`` actually persists. Asymmetric with
        ``_run_cn_add_contact_to_group`` for that reason.

        Pre-flights both entities via ``_load_contact_and_group`` so the
        server layer gets clean ``ContactsNotFoundError``s with
        disambiguating text. AppleScript would otherwise produce a
        generic "Can't get …" error that we'd have to parse.

        Identifiers are escaped via ``escape_applescript_string`` as
        defense-in-depth — CN-issued identifiers contain only hex digits,
        hyphens, colons, and letters, so the helper is a no-op for
        legitimate input but blocks AppleScript injection on adversarial
        input. The ``save`` is load-bearing (same reason as ``write_note``).
        """
        # Pre-flight: raises ContactsNotFoundError("Contact not found: ...")
        # or ContactsNotFoundError("Group not found: ...") as appropriate.
        # We discard the returned objects; AppleScript operates on ids.
        self._load_contact_and_group(contact_identifier, group_identifier)

        escaped_contact_id = escape_applescript_string(contact_identifier)
        escaped_group_id = escape_applescript_string(group_identifier)
        script = (
            'tell application "Contacts"\n'
            f'  set p to first person whose id is "{escaped_contact_id}"\n'
            f'  set g to first group whose id is "{escaped_group_id}"\n'
            "  remove p from g\n"
            "  save\n"
            "end tell"
        )
        try:
            self._run_applescript(script)
        except ContactsAppleScriptError as exc:
            # Pre-flight already covers not-found; if AppleScript surfaces
            # one anyway (e.g., race), translate to ContactsNotFoundError
            # so the server layer can dispatch `not_found` cleanly.
            if _is_not_found_error(exc):
                raise ContactsNotFoundError(
                    f"Contact or group not found "
                    f"(contact={contact_identifier!r}, "
                    f"group={group_identifier!r})"
                ) from exc
            raise

    def _run_cn_search_contacts(
        self, *, field: SearchField, value: str, limit: int
    ) -> list[dict[str, str]]:
        """Search contacts by ``field`` predicate, returning basic-field dicts.

        ``field`` selects the predicate:

        - ``"name"``: ``predicateForContactsMatchingName:`` (substring,
          case-insensitive across given/family/organization names).
        - ``"phone"``: ``predicateForContactsMatchingPhoneNumber:`` after
          wrapping ``value`` in a ``CNPhoneNumber``. Apple's matcher is
          format-tolerant — punctuation and country-code variants normalize.
        - ``"email"``: ``predicateForContactsMatchingEmailAddress:``.
        - ``"organization"``: hand-rolled ``NSPredicate`` with
          ``CONTAINS[cd]`` on ``organizationName`` (case- and
          diacritic-insensitive substring), to mirror name-mode semantics
          since Apple ships no built-in organization predicate.

        Predicate execution is offloaded to the framework — fast even on
        large address books. We slice to ``limit`` *while serializing*
        so we don't pay full O(N_matches) for entries we'll throw away.
        """
        from Contacts import (
            CNContact,
            CNContactEmailAddressesKey,
            CNContactFamilyNameKey,
            CNContactGivenNameKey,
            CNContactIdentifierKey,
            CNContactOrganizationNameKey,
            CNContactPhoneNumbersKey,
        )

        keys = [
            CNContactIdentifierKey,
            CNContactGivenNameKey,
            CNContactFamilyNameKey,
            CNContactOrganizationNameKey,
        ]
        match field:
            case "name":
                pred = CNContact.predicateForContactsMatchingName_(value)
            case "phone":
                from Contacts import CNPhoneNumber

                # Apple's phone predicate silently returns zero results if
                # CNContactPhoneNumbersKey isn't in keysToFetch — the
                # unification step skips contacts whose matching field
                # wasn't requested. Empirically verified; not documented.
                keys.append(CNContactPhoneNumbersKey)
                pred = CNContact.predicateForContactsMatchingPhoneNumber_(
                    CNPhoneNumber.phoneNumberWithStringValue_(value)
                )
            case "email":
                # Email predicate currently matches without
                # CNContactEmailAddressesKey, but include it for symmetry
                # with phone in case Apple tightens the unification step.
                keys.append(CNContactEmailAddressesKey)
                pred = CNContact.predicateForContactsMatchingEmailAddress_(
                    value
                )
            case "organization":
                from Foundation import NSPredicate

                pred = NSPredicate.predicateWithFormat_(
                    "organizationName CONTAINS[cd] %@", value
                )
            case _:
                raise ContactsError(f"Unknown search field: {field!r}")

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
                    label_to_apple_token(p.get("label", "")),
                    CNPhoneNumber.phoneNumberWithStringValue_(p["value"]),
                )
                for p in phones
            ]
        )
    if emails := fields.get("emails"):
        c.setEmailAddresses_(
            [
                CNLabeledValue.labeledValueWithLabel_value_(
                    label_to_apple_token(e.get("label", "")), e["value"]
                )
                for e in emails
            ]
        )
    if urls := fields.get("urls"):
        c.setUrlAddresses_(
            [
                CNLabeledValue.labeledValueWithLabel_value_(
                    label_to_apple_token(u.get("label", "")), u["value"]
                )
                for u in urls
            ]
        )
    if postal := fields.get("postal_addresses"):
        c.setPostalAddresses_(
            [
                CNLabeledValue.labeledValueWithLabel_value_(
                    label_to_apple_token(a.get("label", "")),
                    _build_mutable_postal_address(a),
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
                    label_to_apple_token(p.get("label", "")),
                    CNPhoneNumber.phoneNumberWithStringValue_(p["value"]),
                )
                for p in fields["phones"]
            ]
        )
    if "emails" in fields:
        mutable.setEmailAddresses_(
            [
                CNLabeledValue.labeledValueWithLabel_value_(
                    label_to_apple_token(e.get("label", "")), e["value"]
                )
                for e in fields["emails"]
            ]
        )
    if "urls" in fields:
        mutable.setUrlAddresses_(
            [
                CNLabeledValue.labeledValueWithLabel_value_(
                    label_to_apple_token(u.get("label", "")), u["value"]
                )
                for u in fields["urls"]
            ]
        )
    if "postal_addresses" in fields:
        mutable.setPostalAddresses_(
            [
                CNLabeledValue.labeledValueWithLabel_value_(
                    label_to_apple_token(a.get("label", "")),
                    _build_mutable_postal_address(a),
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
