"""FastMCP server for Apple Contacts integration."""

from __future__ import annotations

import logging
from typing import Any

from fastmcp import FastMCP

from .contacts_connector import ContactsConnector
from .exceptions import ContactsNotFoundError, ContactsTimeoutError
from .security import check_test_mode_safety, require_test_mode_for

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

mcp: FastMCP = FastMCP("apple-contacts")
connector = ContactsConnector()


_LIST_CONTACTS_MAX = 200
_SEARCH_CONTACTS_MAX = 200


_AUTH_REMEDIATION: dict[str, str] = {
    "notDetermined": (
        "Contacts access has not been requested yet. Run a data tool "
        "(e.g. list_contacts) to trigger the system permission prompt, "
        "or grant access manually in System Settings → Privacy & Security "
        "→ Contacts."
    ),
    "denied": (
        "Contacts access was denied. Open System Settings → Privacy & "
        "Security → Contacts and enable access for this server "
        "(macOS will not re-prompt automatically)."
    ),
    "restricted": (
        "Contacts access is locked by parental controls or device "
        "management. Contact your administrator."
    ),
}


@mcp.tool()
def check_authorization() -> dict[str, Any]:
    """Report current TCC authorization status for Contacts access.

    Use this proactively before other tools, and again after any tool
    returns error_type='authorization_denied'. Does not trigger the
    system permission prompt; call list_contacts (or any data tool)
    to do that.

    Returns:
        Always success: True. The status field is one of:
          - "authorized": full access (proceed with any tool)
          - "limited":    macOS 14+ partial access (proceed; some
                          contacts may be hidden)
          - "notDetermined": permission not yet requested
          - "denied":     user explicitly denied
          - "restricted": locked by MDM / parental controls
        When status is not authorized/limited, the response also
        includes a remediation field with copy you can show the user.
    """
    try:
        status = connector._run_cn_authorization_status()
    except Exception as exc:
        logger.error("check_authorization failed: %s", exc)
        return {
            "success": False,
            "error": f"Failed to read TCC status: {exc}",
            "error_type": "unknown",
        }

    response: dict[str, Any] = {"success": True, "status": status}
    if status not in ("authorized", "limited"):
        response["remediation"] = _AUTH_REMEDIATION[status]
    return response


def _require_contacts_authorization() -> dict[str, Any] | None:
    """Returns None if access is granted; otherwise an error dict to return.

    Reused by every data tool. Triggers the system permission prompt the
    first time it sees ``notDetermined``.
    """
    try:
        status = connector._run_cn_authorization_status()
        if status == "notDetermined":
            try:
                connector._run_cn_request_access()
            except ContactsTimeoutError:
                return {
                    "success": False,
                    "error": (
                        "Contacts permission prompt is awaiting your "
                        "response. Grant access in the system dialog "
                        "and retry."
                    ),
                    "error_type": "authorization_denied",
                    "status": "notDetermined",
                }
            status = connector._run_cn_authorization_status()
        if status in ("authorized", "limited"):
            return None
        return {
            "success": False,
            "status": status,
            "error": f"Contacts access not granted (status={status}).",
            "error_type": "authorization_denied",
            "remediation": _AUTH_REMEDIATION.get(
                status,
                "Open System Settings → Privacy & Security → Contacts.",
            ),
        }
    except Exception as exc:
        logger.error("authorization check failed: %s", exc)
        return {
            "success": False,
            "error": f"Failed to check TCC status: {exc}",
            "error_type": "unknown",
        }


@mcp.tool()
def list_contacts(offset: int = 0, limit: int = 50) -> dict[str, Any]:
    """List contacts (paged), each with id, given_name, family_name, organization.

    Use ``get_contact(id)`` to fetch full details for a specific contact.
    Use ``search_contacts(query)`` to filter by name. Order is not
    guaranteed.

    Args:
        offset: Number of contacts to skip. Default 0. Must be >= 0.
        limit:  Max contacts to return. Default 50. Capped at 200.

    Returns:
        On success: ``{"success": True, "contacts": [...], "count": N,
        "offset": offset, "limit": effective_limit}``.
        On TCC denial: ``{"success": False, "error_type":
        "authorization_denied", "status": ..., "error": ..., "remediation":
        ...}``.
        On bad input: ``{"success": False, "error_type":
        "validation_error", "error": ...}``.
        On unexpected failure: ``{"success": False, "error_type":
        "unknown", "error": ...}``.
    """
    if offset < 0:
        return {
            "success": False,
            "error": "offset must be >= 0",
            "error_type": "validation_error",
        }
    if limit < 1:
        return {
            "success": False,
            "error": "limit must be >= 1",
            "error_type": "validation_error",
        }

    effective_limit = min(limit, _LIST_CONTACTS_MAX)

    auth_err = _require_contacts_authorization()
    if auth_err is not None:
        return auth_err

    try:
        contacts = connector._run_cn_enumerate_contacts(
            offset=offset, limit=effective_limit
        )
    except Exception as exc:
        logger.error("list_contacts fetch failed: %s", exc)
        return {
            "success": False,
            "error": f"Fetch failed: {exc}",
            "error_type": "unknown",
        }

    return {
        "success": True,
        "contacts": contacts,
        "count": len(contacts),
        "offset": offset,
        "limit": effective_limit,
    }


@mcp.tool()
def get_contact(identifier: str) -> dict[str, Any]:
    """Fetch a single contact by its CN identifier with all P1 fields.

    Get an identifier from ``list_contacts`` or ``search_contacts``, then
    call this for the full record (name parts, organization, phones,
    emails, postal addresses, urls, birthday).

    Args:
        identifier: The contact's CN identifier (UUID-shaped string).

    Returns:
        On success: ``{"success": True, "contact": {...full P1 fields...}}``.
        On missing identifier (no such contact): ``{"success": False,
        "error_type": "not_found", "error": ...}``.
        On bad input (empty string): ``{"success": False, "error_type":
        "validation_error", "error": ...}``.
        On TCC denial: same shape as ``list_contacts`` (status,
        remediation).
        On unexpected failure: ``{"success": False, "error_type":
        "unknown", "error": ...}``.

    Each entry in the labeled-value families (phones, emails, urls,
    postal_addresses) carries both ``label_raw`` (the
    ``_$!<...>!$_`` token) and ``label`` (the human string).
    """
    if not identifier or not identifier.strip():
        return {
            "success": False,
            "error": "identifier must be a non-empty string",
            "error_type": "validation_error",
        }

    auth_err = _require_contacts_authorization()
    if auth_err is not None:
        return auth_err

    try:
        contact = connector._run_cn_unified_contact(identifier)
    except Exception as exc:
        logger.error("get_contact fetch failed: %s", exc)
        return {
            "success": False,
            "error": f"Fetch failed: {exc}",
            "error_type": "unknown",
        }

    if contact is None:
        return {
            "success": False,
            "error": f"No contact found with identifier {identifier!r}",
            "error_type": "not_found",
        }

    return {"success": True, "contact": contact}


@mcp.tool()
def search_contacts(query: str) -> dict[str, Any]:
    """Find contacts whose name matches `query` (substring, case-insensitive).

    Matches given/family/organization names via Apple's built-in
    ``predicateForContactsMatchingName:``. Returns up to 200 results
    (hard cap). Use ``list_contacts`` for unfiltered iteration;
    ``get_contact(id)`` for full details on a specific result. Order
    is not guaranteed.

    Args:
        query: Substring to match. Must be a non-empty string.

    Returns:
        On success: ``{"success": True, "contacts": [...], "count": N,
        "query": query, "limit": 200}``. ``count == limit`` indicates
        the cap was hit and there may be more matches.
        On bad input: ``{"success": False, "error_type":
        "validation_error", "error": ...}``.
        On TCC denial: same shape as ``list_contacts`` (status,
        remediation).
        On unexpected failure: ``{"success": False, "error_type":
        "unknown", "error": ...}``.
    """
    if not query or not query.strip():
        return {
            "success": False,
            "error": "query must be a non-empty string",
            "error_type": "validation_error",
        }

    auth_err = _require_contacts_authorization()
    if auth_err is not None:
        return auth_err

    try:
        contacts = connector._run_cn_search_contacts(
            query=query, limit=_SEARCH_CONTACTS_MAX
        )
    except Exception as exc:
        logger.error("search_contacts fetch failed: %s", exc)
        return {
            "success": False,
            "error": f"Search failed: {exc}",
            "error_type": "unknown",
        }

    return {
        "success": True,
        "contacts": contacts,
        "count": len(contacts),
        "query": query,
        "limit": _SEARCH_CONTACTS_MAX,
    }


def _validate_create_contact_input(
    fields: dict[str, Any],
) -> dict[str, Any] | None:
    """Validate create_contact's parsed input. Returns None if OK, error
    dict otherwise."""
    if not (
        (fields.get("given_name") or "").strip()
        or (fields.get("family_name") or "").strip()
        or (fields.get("organization") or "").strip()
    ):
        return _validation_error(
            "At least one of given_name, family_name, or organization "
            "must be set."
        )

    for i, p in enumerate(fields.get("phones") or []):
        if not (p.get("value") or "").strip():
            return _validation_error(f"phones[{i}].value must be non-empty")

    for i, e in enumerate(fields.get("emails") or []):
        v = (e.get("value") or "").strip()
        if not v:
            return _validation_error(f"emails[{i}].value must be non-empty")
        if "@" not in v:
            return _validation_error(f"emails[{i}].value must contain '@'")

    for i, u in enumerate(fields.get("urls") or []):
        if not (u.get("value") or "").strip():
            return _validation_error(f"urls[{i}].value must be non-empty")

    for i, a in enumerate(fields.get("postal_addresses") or []):
        if not any(
            (a.get(k) or "").strip()
            for k in ("street", "city", "state", "postal_code", "country")
        ):
            return _validation_error(
                f"postal_addresses[{i}] must set at least one of "
                f"street/city/state/postal_code/country"
            )

    bday = fields.get("birthday")
    if bday is not None:
        m = bday.get("month")
        d = bday.get("day")
        y = bday.get("year")
        if m is not None and not (1 <= m <= 12):
            return _validation_error("birthday.month must be 1-12")
        if d is not None and not (1 <= d <= 31):
            return _validation_error("birthday.day must be 1-31")
        if y is not None and y <= 0:
            return _validation_error("birthday.year must be > 0 if set")

    return None


def _validation_error(msg: str) -> dict[str, Any]:
    return {
        "success": False,
        "error": msg,
        "error_type": "validation_error",
    }


@mcp.tool()
def create_contact(
    given_name: str = "",
    family_name: str = "",
    middle_name: str = "",
    name_prefix: str = "",
    name_suffix: str = "",
    nickname: str = "",
    organization: str = "",
    job_title: str = "",
    department: str = "",
    phones: list[dict[str, str]] | None = None,
    emails: list[dict[str, str]] | None = None,
    urls: list[dict[str, str]] | None = None,
    postal_addresses: list[dict[str, str]] | None = None,
    birthday: dict[str, int] | None = None,
    group_identifier: str | None = None,
) -> dict[str, Any]:
    """Create a new contact in the user's default container.

    Pass any subset of the P1 fields. At least one of ``given_name``,
    ``family_name``, or ``organization`` must be non-empty. Labeled-value
    entries (phones, emails, urls, postal_addresses) carry ``label_raw``
    (an Apple token like ``_$!<Mobile>!$_``, or any custom string) plus
    their type-specific value field(s).

    In test mode (``CONTACTS_TEST_MODE=true``), ``group_identifier`` must
    be provided and must match ``CONTACTS_TEST_GROUP``. The new contact
    is added to that group atomically with creation, so the test
    harness can clean it up.

    Args:
        given_name, family_name, middle_name, name_prefix, name_suffix,
        nickname: Name parts; default "".
        organization, job_title, department: Org triplet; default "".
        phones / emails / urls: Lists of ``{label_raw, value}`` dicts.
        postal_addresses: List of ``{label_raw, street, sub_locality,
            city, sub_administrative_area, state, postal_code, country,
            iso_country_code}`` dicts (any subset; at least one address
            field must be non-empty).
        birthday: ``{year?, month?, day?}`` (any subset).
        group_identifier: If set, the new contact is added to this group
            in the same CNSaveRequest. Required in test mode.

    Returns:
        On success: ``{"success": True, "identifier": "...",
        "group_id": "..."}``. ``group_id`` is omitted when
        ``group_identifier`` was None.
        On bad input: ``{"success": False, "error_type":
        "validation_error", "error": ...}``.
        On TCC denial: same shape as ``list_contacts``.
        On test-mode safety violation: ``{"success": False,
        "error_type": "safety_violation", "error": ...}``.
        On group not found: ``{"success": False, "error_type":
        "not_found", "error": ...}``.
        On CN save failure: ``{"success": False, "error_type":
        "unknown", "error": ...}``.
    """
    fields: dict[str, Any] = {
        "given_name": given_name,
        "family_name": family_name,
        "middle_name": middle_name,
        "name_prefix": name_prefix,
        "name_suffix": name_suffix,
        "nickname": nickname,
        "organization": organization,
        "job_title": job_title,
        "department": department,
        "phones": phones or [],
        "emails": emails or [],
        "urls": urls or [],
        "postal_addresses": postal_addresses or [],
        "birthday": birthday,
    }

    validation_err = _validate_create_contact_input(fields)
    if validation_err is not None:
        return validation_err

    auth_err = _require_contacts_authorization()
    if auth_err is not None:
        return auth_err

    safety_err = check_test_mode_safety(
        "create_contact", group=group_identifier
    )
    if safety_err is not None:
        return safety_err

    try:
        identifier = connector._run_cn_create_contact(
            fields=fields, group_identifier=group_identifier
        )
    except ContactsNotFoundError as exc:
        return {
            "success": False,
            "error": str(exc),
            "error_type": "not_found",
        }
    except Exception as exc:
        logger.error("create_contact failed: %s", exc)
        return {
            "success": False,
            "error": f"Create failed: {exc}",
            "error_type": "unknown",
        }

    response: dict[str, Any] = {"success": True, "identifier": identifier}
    if group_identifier is not None:
        response["group_id"] = group_identifier
    return response


def _validate_update_contact_input(
    identifier: str, fields: dict[str, Any]
) -> dict[str, Any] | None:
    """Validate update_contact's parsed input. Returns None if OK, error
    dict otherwise."""
    if not identifier or not identifier.strip():
        return _validation_error("identifier must be a non-empty string")

    if len(fields) == 0:
        return _validation_error(
            "At least one field must be supplied to update."
        )

    for i, p in enumerate(fields.get("phones") or []):
        if not (p.get("value") or "").strip():
            return _validation_error(f"phones[{i}].value must be non-empty")

    for i, e in enumerate(fields.get("emails") or []):
        v = (e.get("value") or "").strip()
        if not v:
            return _validation_error(f"emails[{i}].value must be non-empty")
        if "@" not in v:
            return _validation_error(f"emails[{i}].value must contain '@'")

    for i, u in enumerate(fields.get("urls") or []):
        if not (u.get("value") or "").strip():
            return _validation_error(f"urls[{i}].value must be non-empty")

    for i, a in enumerate(fields.get("postal_addresses") or []):
        if not any(
            (a.get(k) or "").strip()
            for k in ("street", "city", "state", "postal_code", "country")
        ):
            return _validation_error(
                f"postal_addresses[{i}] must set at least one of "
                f"street/city/state/postal_code/country"
            )

    bday = fields.get("birthday")
    if bday is not None:
        m = bday.get("month")
        d = bday.get("day")
        y = bday.get("year")
        if m is not None and not (1 <= m <= 12):
            return _validation_error("birthday.month must be 1-12")
        if d is not None and not (1 <= d <= 31):
            return _validation_error("birthday.day must be 1-31")
        if y is not None and y <= 0:
            return _validation_error("birthday.year must be > 0 if set")

    return None


@mcp.tool()
def update_contact(
    identifier: str,
    given_name: str | None = None,
    family_name: str | None = None,
    middle_name: str | None = None,
    name_prefix: str | None = None,
    name_suffix: str | None = None,
    nickname: str | None = None,
    organization: str | None = None,
    job_title: str | None = None,
    department: str | None = None,
    phones: list[dict[str, str]] | None = None,
    emails: list[dict[str, str]] | None = None,
    urls: list[dict[str, str]] | None = None,
    postal_addresses: list[dict[str, str]] | None = None,
    birthday: dict[str, int] | None = None,
    group_identifier: str | None = None,
) -> dict[str, Any]:
    """Update an existing contact by identifier with partial-field semantics.

    Every field defaults to ``None`` meaning "don't touch". To explicitly
    clear a string field, pass ``""``. To replace a multi-valued list
    (phones / emails / urls / postal_addresses), pass the new list — the
    existing list is replaced entirely (REST-PUT semantics, not append).
    Pass ``[]`` to clear all entries of a list. At least one field must
    be supplied.

    In test mode (``CONTACTS_TEST_MODE=true``), ``group_identifier``
    must match ``CONTACTS_TEST_GROUP``. The connector does not consult
    ``group_identifier`` — it's only used for the test-mode safety
    assertion.

    Args:
        identifier: The contact's CN identifier.
        ...all P1 fields (None = don't touch; "" = clear)...
        group_identifier: Test-mode safety assertion. Required in test
            mode. Ignored otherwise.

    Returns:
        On success: ``{"success": True, "identifier": identifier}``.
        Use ``get_contact(identifier)`` to read back the updated record.
        On bad input: ``{"success": False, "error_type":
        "validation_error", ...}``.
        On TCC denial: same shape as ``list_contacts``.
        On safety violation: ``{"success": False, "error_type":
        "safety_violation", ...}``.
        On contact not found: ``{"success": False, "error_type":
        "not_found", ...}``.
        On CN save failure: ``{"success": False, "error_type":
        "unknown", ...}``.
    """
    fields: dict[str, Any] = {}
    for key, value in (
        ("given_name", given_name),
        ("family_name", family_name),
        ("middle_name", middle_name),
        ("name_prefix", name_prefix),
        ("name_suffix", name_suffix),
        ("nickname", nickname),
        ("organization", organization),
        ("job_title", job_title),
        ("department", department),
        ("phones", phones),
        ("emails", emails),
        ("urls", urls),
        ("postal_addresses", postal_addresses),
        ("birthday", birthday),
    ):
        if value is not None:
            fields[key] = value

    validation_err = _validate_update_contact_input(identifier, fields)
    if validation_err is not None:
        return validation_err

    auth_err = _require_contacts_authorization()
    if auth_err is not None:
        return auth_err

    safety_err = check_test_mode_safety(
        "update_contact", group=group_identifier
    )
    if safety_err is not None:
        return safety_err

    try:
        connector._run_cn_update_contact(identifier=identifier, fields=fields)
    except ContactsNotFoundError as exc:
        return {
            "success": False,
            "error": str(exc),
            "error_type": "not_found",
        }
    except Exception as exc:
        logger.error("update_contact failed: %s", exc)
        return {
            "success": False,
            "error": f"Update failed: {exc}",
            "error_type": "unknown",
        }

    return {"success": True, "identifier": identifier}


@mcp.tool()
def delete_contact(
    identifier: str,
    group_identifier: str | None = None,
) -> dict[str, Any]:
    """Delete an existing contact by identifier.

    **v0.1.0 only allows delete in test mode** — the full destructive
    UX (with confirmation prompts) ships in v0.4.0 (#24). Outside test
    mode this returns a safety_violation error.

    In test mode (``CONTACTS_TEST_MODE=true``), ``group_identifier``
    must match ``CONTACTS_TEST_GROUP`` to ensure the deleted contact
    is one the test harness created.

    Args:
        identifier: The contact's CN identifier.
        group_identifier: Test-mode safety assertion. Required in test
            mode (no other use).

    Returns:
        On success: ``{"success": True, "identifier": identifier}``.
        On bad input: ``{"success": False, "error_type":
        "validation_error", ...}``.
        On test mode off: ``{"success": False, "error_type":
        "safety_violation", ...}``.
        On TCC denial: same shape as ``list_contacts``.
        On contact not found: ``{"success": False, "error_type":
        "not_found", ...}``.
        On CN save failure: ``{"success": False, "error_type":
        "unknown", ...}``.
    """
    if not identifier or not identifier.strip():
        return _validation_error("identifier must be a non-empty string")

    require_err = require_test_mode_for("delete_contact")
    if require_err is not None:
        return require_err

    auth_err = _require_contacts_authorization()
    if auth_err is not None:
        return auth_err

    safety_err = check_test_mode_safety(
        "delete_contact", group=group_identifier
    )
    if safety_err is not None:
        return safety_err

    try:
        connector._run_cn_delete_contact(identifier=identifier)
    except ContactsNotFoundError as exc:
        return {
            "success": False,
            "error": str(exc),
            "error_type": "not_found",
        }
    except Exception as exc:
        logger.error("delete_contact failed: %s", exc)
        return {
            "success": False,
            "error": f"Delete failed: {exc}",
            "error_type": "unknown",
        }

    return {"success": True, "identifier": identifier}


def main() -> None:
    """Start the MCP server."""
    mcp.run()


if __name__ == "__main__":
    main()
