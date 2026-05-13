"""FastMCP server for Apple Contacts integration."""

from __future__ import annotations

import base64
import binascii
import logging
from typing import Any, cast

from fastmcp import FastMCP
from fastmcp.server.context import Context

from .contacts_connector import ContactsConnector, SearchField
from .exceptions import ContactsError, ContactsNotFoundError, ContactsTimeoutError
from .security import (
    _confirm_destructive,
    _is_test_mode_enabled,
    check_test_mode_safety,
)
from .utils import detect_image_format

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

mcp: FastMCP = FastMCP("apple-contacts")
connector = ContactsConnector()


_LIST_CONTACTS_MAX = 200
_SEARCH_CONTACTS_MAX = 200
_LIST_GROUPS_MAX = 200
_LIST_CONTAINERS_MAX = 10


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
def get_contact(
    identifier: str, include_niche: bool = False
) -> dict[str, Any]:
    """Fetch a single contact by its CN identifier with all P1 fields.

    Get an identifier from ``list_contacts`` or ``search_contacts``, then
    call this for the full record (name parts, organization, phones,
    emails, postal addresses, urls, birthday).

    Args:
        identifier: The contact's CN identifier (UUID-shaped string).
        include_niche: When True, also fetch the P3 niche families
            (``dates``, ``social_profiles``, ``relations``,
            ``instant_messages``). Off by default — most contacts won't
            have these populated and including them grows responses.

    Returns:
        On success: ``{"success": True, "contact": {...full P1 fields...}}``.
        When ``include_niche=True``, the contact dict also contains
        ``dates`` / ``social_profiles`` / ``relations`` / ``instant_messages``
        keys (possibly empty lists). When False (default), those keys are
        absent.
        On missing identifier (no such contact): ``{"success": False,
        "error_type": "not_found", "error": ...}``.
        On bad input (empty string): ``{"success": False, "error_type":
        "validation_error", "error": ...}``.
        On TCC denial: same shape as ``list_contacts`` (status,
        remediation).
        On unexpected failure: ``{"success": False, "error_type":
        "unknown", "error": ...}``.

    Each entry in the labeled-value families (phones, emails, urls,
    postal_addresses, and the niche families when included) carries both
    ``label_raw`` (the ``_$!<...>!$_`` token) and ``label`` (the human
    string).
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
        contact = connector._run_cn_unified_contact(
            identifier, include_niche=include_niche
        )
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
def search_contacts(
    name: str = "",
    phone: str = "",
    email: str = "",
    organization: str = "",
) -> dict[str, Any]:
    """Find contacts by name, phone, email, or organization (pick one).

    Exactly one of the four parameters must be set (non-empty after
    stripping); whitespace-only values count as unset. Returns up to
    200 results (hard cap). Use ``list_contacts`` for unfiltered
    iteration; ``get_contact(id)`` for full details on a result.
    Order is not guaranteed.

    Match semantics:

    - ``name``: substring + case-insensitive across given/family/
      organization names (Apple's ``predicateForContactsMatchingName:``).
    - ``phone``: format-tolerant match via Apple's
      ``predicateForContactsMatchingPhoneNumber:``. Punctuation,
      spacing, and country-code variants normalize automatically; pass
      whatever the user typed.
    - ``email``: ``predicateForContactsMatchingEmailAddress:``.
    - ``organization``: substring, case- and diacritic-insensitive
      (custom ``NSPredicate`` with ``CONTAINS[cd]``), to mirror
      name-mode behavior since Apple ships no built-in organization
      predicate.

    Args:
        name: Substring to match against contact names.
        phone: Phone number to match (any format).
        email: Email address to match.
        organization: Substring to match against organization name.

    Returns:
        On success: ``{"success": True, "contacts": [...], "count": N,
        "search_field": "<name|phone|email|organization>",
        "search_value": "<stripped value>", "limit": 200}``.
        ``count == limit`` indicates the cap was hit and there may be
        more matches.
        On bad input (zero or multiple fields set): ``{"success":
        False, "error_type": "validation_error", "error": ...}``.
        On TCC denial: same shape as ``list_contacts`` (status,
        remediation).
        On unexpected failure: ``{"success": False, "error_type":
        "unknown", "error": ...}``.
    """
    candidates = {
        "name": name,
        "phone": phone,
        "email": email,
        "organization": organization,
    }
    provided = {k: v.strip() for k, v in candidates.items() if v.strip()}
    if len(provided) == 0:
        return _validation_error(
            "Exactly one of name, phone, email, organization must be set."
        )
    if len(provided) > 1:
        return _validation_error(
            f"Exactly one search field allowed; got {sorted(provided)}."
        )
    [(field, value)] = provided.items()

    auth_err = _require_contacts_authorization()
    if auth_err is not None:
        return auth_err

    try:
        contacts = connector._run_cn_search_contacts(
            field=cast(SearchField, field),
            value=value,
            limit=_SEARCH_CONTACTS_MAX,
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
        "search_field": field,
        "search_value": value,
        "limit": _SEARCH_CONTACTS_MAX,
    }


@mcp.tool()
def list_containers() -> dict[str, Any]:
    """List all contact containers (accounts).

    A "container" in `Contacts.framework` is an account: iCloud, a Google
    CardDAV account, Exchange, the legacy "On My Mac" local store, etc.
    Each entry has ``id`` (the CN identifier), ``name`` (user-visible),
    ``type`` (one of ``"local"``, ``"exchange"``, ``"cardDAV"``), and
    ``is_default`` (True for the container new contacts go into when no
    ``container_identifier`` is specified). Use the ``id`` with
    ``create_contact(..., container_identifier=...)`` to target a
    specific account.

    Hard cap at 10 (containers per user are typically <5).

    Returns:
        On success: ``{"success": True, "containers": [...], "count": N,
        "limit": 10}``. ``count == limit`` indicates the cap was hit.
        On TCC denial: same shape as ``list_contacts`` (status,
        remediation).
        On unexpected failure: ``{"success": False, "error_type":
        "unknown", "error": ...}``.
    """
    auth_err = _require_contacts_authorization()
    if auth_err is not None:
        return auth_err

    try:
        containers = connector._run_cn_list_containers()
    except Exception as exc:
        logger.error("list_containers failed: %s", exc)
        return {
            "success": False,
            "error": f"list_containers failed: {exc}",
            "error_type": "unknown",
        }

    capped = containers[:_LIST_CONTAINERS_MAX]
    return {
        "success": True,
        "containers": capped,
        "count": len(capped),
        "limit": _LIST_CONTAINERS_MAX,
    }


@mcp.tool()
def list_groups() -> dict[str, Any]:
    """List all contact groups across all containers.

    Each entry has ``id``, ``name``, and ``container_id``. Use the ``id``
    with ``get_contacts_in_group(identifier)`` to enumerate the group's
    members. Returns up to 200 groups (hard cap; nearly nobody has more).
    Order is not guaranteed.

    Returns:
        On success: ``{"success": True, "groups": [...], "count": N,
        "limit": 200}``. ``count == limit`` indicates the cap was hit.
        On TCC denial: same shape as ``list_contacts`` (status,
        remediation).
        On unexpected failure: ``{"success": False, "error_type":
        "unknown", "error": ...}``.
    """
    auth_err = _require_contacts_authorization()
    if auth_err is not None:
        return auth_err

    try:
        groups = connector._run_cn_list_groups()
    except Exception as exc:
        logger.error("list_groups failed: %s", exc)
        return {
            "success": False,
            "error": f"list_groups failed: {exc}",
            "error_type": "unknown",
        }

    capped = groups[:_LIST_GROUPS_MAX]
    return {
        "success": True,
        "groups": capped,
        "count": len(capped),
        "limit": _LIST_GROUPS_MAX,
    }


@mcp.tool()
def get_contacts_in_group(identifier: str) -> dict[str, Any]:
    """List contacts whose membership includes the given group.

    Returns the same 4-field shape as ``list_contacts``. Use
    ``get_contact(id)`` to fetch full details for a result. Hard cap of
    200; ``count == limit`` indicates the cap was hit.

    Pre-flights existence via ``_run_cn_fetch_group``: an unknown
    ``identifier`` returns ``not_found`` distinctly from a real-but-empty
    group.

    Args:
        identifier: The group's CN identifier.

    Returns:
        On success: ``{"success": True, "group_identifier": ...,
        "contacts": [...], "count": N, "limit": 200}``.
        On bad input: ``validation_error``.
        On TCC denial: ``authorization_denied``.
        On unknown identifier: ``not_found``.
        On unexpected failure: ``unknown``.
    """
    if not identifier or not identifier.strip():
        return _validation_error("identifier must be a non-empty string")

    auth_err = _require_contacts_authorization()
    if auth_err is not None:
        return auth_err

    try:
        group = connector._run_cn_fetch_group(identifier)
        if group is None:
            return {
                "success": False,
                "error": f"No group found with identifier {identifier!r}",
                "error_type": "not_found",
            }
        contacts = connector._run_cn_contacts_in_group(
            identifier, _SEARCH_CONTACTS_MAX
        )
    except Exception as exc:
        logger.error("get_contacts_in_group failed: %s", exc)
        return {
            "success": False,
            "error": f"get_contacts_in_group failed: {exc}",
            "error_type": "unknown",
        }

    return {
        "success": True,
        "group_identifier": identifier,
        "contacts": contacts,
        "count": len(contacts),
        "limit": _SEARCH_CONTACTS_MAX,
    }


def _validate_phones(
    phones: list[dict[str, str]] | None,
) -> dict[str, Any] | None:
    for i, p in enumerate(phones or []):
        if not (p.get("value") or "").strip():
            return _validation_error(f"phones[{i}].value must be non-empty")
    return None


def _validate_emails(
    emails: list[dict[str, str]] | None,
) -> dict[str, Any] | None:
    for i, e in enumerate(emails or []):
        v = (e.get("value") or "").strip()
        if not v:
            return _validation_error(f"emails[{i}].value must be non-empty")
        if "@" not in v:
            return _validation_error(f"emails[{i}].value must contain '@'")
    return None


def _validate_urls(
    urls: list[dict[str, str]] | None,
) -> dict[str, Any] | None:
    for i, u in enumerate(urls or []):
        if not (u.get("value") or "").strip():
            return _validation_error(f"urls[{i}].value must be non-empty")
    return None


def _validate_postal_addresses(
    addrs: list[dict[str, str]] | None,
) -> dict[str, Any] | None:
    for i, a in enumerate(addrs or []):
        if not any(
            (a.get(k) or "").strip()
            for k in ("street", "city", "state", "postal_code", "country")
        ):
            return _validation_error(
                f"postal_addresses[{i}] must set at least one of "
                f"street/city/state/postal_code/country"
            )
    return None


def _validate_birthday(
    bday: dict[str, int] | None,
) -> dict[str, Any] | None:
    if bday is None:
        return None
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


def _validate_dates(
    dates: list[dict[str, Any]] | None,
) -> dict[str, Any] | None:
    for i, d in enumerate(dates or []):
        m = d.get("month")
        day = d.get("day")
        y = d.get("year")
        if m is None and day is None and y is None:
            return _validation_error(
                f"dates[{i}] must set at least one of year/month/day"
            )
        if m is not None and not (1 <= m <= 12):
            return _validation_error(f"dates[{i}].month must be 1-12")
        if day is not None and not (1 <= day <= 31):
            return _validation_error(f"dates[{i}].day must be 1-31")
        if y is not None and y <= 0:
            return _validation_error(
                f"dates[{i}].year must be > 0 if set"
            )
    return None


def _validate_social_profiles(
    profiles: list[dict[str, str]] | None,
) -> dict[str, Any] | None:
    for i, p in enumerate(profiles or []):
        username = (p.get("username") or "").strip()
        url = (p.get("url") or "").strip()
        if not username and not url:
            return _validation_error(
                f"social_profiles[{i}] must set at least one of "
                f"username/url"
            )
    return None


def _validate_relations(
    relations: list[dict[str, str]] | None,
) -> dict[str, Any] | None:
    for i, r in enumerate(relations or []):
        if not (r.get("name") or "").strip():
            return _validation_error(
                f"relations[{i}].name must be non-empty"
            )
    return None


def _validate_instant_messages(
    messages: list[dict[str, str]] | None,
) -> dict[str, Any] | None:
    for i, m in enumerate(messages or []):
        if not (m.get("username") or "").strip():
            return _validation_error(
                f"instant_messages[{i}].username must be non-empty"
            )
    return None


def _validate_labeled_value_fields(
    fields: dict[str, Any],
) -> dict[str, Any] | None:
    """Run the per-field-type validators shared by create_contact and
    update_contact. First failure short-circuits."""
    return (
        _validate_phones(fields.get("phones"))
        or _validate_emails(fields.get("emails"))
        or _validate_urls(fields.get("urls"))
        or _validate_postal_addresses(fields.get("postal_addresses"))
        or _validate_birthday(fields.get("birthday"))
        or _validate_dates(fields.get("dates"))
        or _validate_social_profiles(fields.get("social_profiles"))
        or _validate_relations(fields.get("relations"))
        or _validate_instant_messages(fields.get("instant_messages"))
    )


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
    return _validate_labeled_value_fields(fields)


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
    dates: list[dict[str, Any]] | None = None,
    social_profiles: list[dict[str, str]] | None = None,
    relations: list[dict[str, str]] | None = None,
    instant_messages: list[dict[str, str]] | None = None,
    group_identifier: str | None = None,
    container_identifier: str | None = None,
) -> dict[str, Any]:
    """Create a new contact, optionally in a non-default container.

    Pass any subset of the P1 fields. At least one of ``given_name``,
    ``family_name``, or ``organization`` must be non-empty. Labeled-value
    entries (phones, emails, urls, postal_addresses) carry a ``label``
    field plus their type-specific value field(s). The ``label`` accepts
    three forms (case-insensitive): an English human form (``"mobile"``,
    ``"home fax"``, ``"iPhone"``), Apple's raw token
    (``"_$!<Mobile>!$_"``), or any custom string (``"Spotify"``).
    See ``docs/research/label-translation-decision.md``.

    Without ``container_identifier``, the new contact lands in the user's
    default container (typically iCloud). Pass a specific container UUID
    from ``list_containers`` to write to a non-default account (e.g.,
    Gmail/CardDAV). See ``docs/research/multi-container-write-decision.md``.

    In test mode (``CONTACTS_TEST_MODE=true``), ``group_identifier`` must
    be provided and must match ``CONTACTS_TEST_GROUP``. The new contact
    is added to that group atomically with creation, so the test
    harness can clean it up.

    Args:
        given_name, family_name, middle_name, name_prefix, name_suffix,
        nickname: Name parts; default "".
        organization, job_title, department: Org triplet; default "".
        phones / emails / urls: Lists of ``{label, value}`` dicts.
        postal_addresses: List of ``{label, street, sub_locality,
            city, sub_administrative_area, state, postal_code, country,
            iso_country_code}`` dicts (any subset; at least one address
            field must be non-empty).
        birthday: ``{year?, month?, day?}`` (any subset).
        group_identifier: If set, the new contact is added to this group
            in the same CNSaveRequest. Required in test mode.
        container_identifier: If set, target this container instead of
            the default. Use ``list_containers`` to discover UUIDs. CN
            validates existence at save time; an unknown identifier
            surfaces as ``unknown``.

    Returns:
        On success: ``{"success": True, "identifier": "...",
        "group_id": ..., "container_id": ...}``. Both id-echo keys are
        ``null`` when the corresponding input was not supplied.
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
        "dates": dates or [],
        "social_profiles": social_profiles or [],
        "relations": relations or [],
        "instant_messages": instant_messages or [],
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
            fields=fields,
            group_identifier=group_identifier,
            container_identifier=container_identifier,
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

    return {
        "success": True,
        "identifier": identifier,
        "group_id": group_identifier,
        "container_id": container_identifier,
    }


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
    return _validate_labeled_value_fields(fields)


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
    dates: list[dict[str, Any]] | None = None,
    social_profiles: list[dict[str, str]] | None = None,
    relations: list[dict[str, str]] | None = None,
    instant_messages: list[dict[str, str]] | None = None,
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
        ("dates", dates),
        ("social_profiles", social_profiles),
        ("relations", relations),
        ("instant_messages", instant_messages),
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
async def delete_contact(
    ctx: Context,
    identifier: str,
    group_identifier: str | None = None,
) -> dict[str, Any]:
    """Delete an existing contact by identifier.

    Outside test mode, this tool requires explicit user confirmation via
    FastMCP elicitation: the client renders "Delete contact 'X' (id)?
    This cannot be undone." with Yes/No buttons before the delete runs.
    Declined or cancelled prompts return ``user_declined``.

    If the client doesn't support elicitation, the tool refuses with
    ``safety_violation`` pointing at ``CONTACTS_TEST_MODE`` as the bypass
    for test-harness use.

    In test mode (``CONTACTS_TEST_MODE=true``), confirmation is skipped
    and the existing test-group safety gate applies: ``group_identifier``
    must match ``CONTACTS_TEST_GROUP``.

    Args:
        identifier: The contact's CN identifier.
        group_identifier: Test-mode safety assertion. Required in test
            mode (no other use).

    Returns:
        On success: ``{"success": True, "identifier": identifier}``.
        On bad input: ``{"success": False, "error_type":
        "validation_error", ...}``.
        On test-mode safety gate refusal or elicit-unsupported client:
        ``safety_violation``.
        On user declining the confirmation: ``user_declined``.
        On TCC denial: same shape as ``list_contacts``.
        On contact not found: ``not_found``.
        On CN save failure: ``unknown``.
    """
    if not identifier or not identifier.strip():
        return _validation_error("identifier must be a non-empty string")

    auth_err = _require_contacts_authorization()
    if auth_err is not None:
        return auth_err

    if _is_test_mode_enabled():
        safety_err = check_test_mode_safety(
            "delete_contact", group=group_identifier
        )
        if safety_err is not None:
            return safety_err
    else:
        confirm_err = await _confirm_destructive(
            ctx,
            operation="delete_contact",
            entity_kind="contact",
            identifier=identifier,
            preview_lookup=lambda: connector._run_cn_unified_contact(identifier),
            describe=lambda c: (
                f"{c.get('given_name', '')} {c.get('family_name', '')}".strip()
                or c.get("organization")
                or "(no name)"
            ),
        )
        if confirm_err is not None:
            return confirm_err

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


@mcp.tool()
def export_vcard(identifiers: list[str]) -> dict[str, Any]:
    """Export one or more contacts as a single vCard 3.0 payload.

    Atomic: any single missing identifier aborts the whole call with
    ``not_found``. The returned vCard text is exactly what Apple's
    serializer emits — vCard 3.0 verbatim. See
    ``docs/research/vcard-version-decision.md`` for the rationale and
    Apple's specific quirks.

    Args:
        identifiers: A non-empty list of contact CN identifiers (the
            suffixed ``<UUID>:ABPerson`` form returned by other tools).
            Single-contact callers pass ``[id]``.

    Returns:
        On success: ``{"success": True, "vcard": <text>, "count": N,
        "notes": [<limitations>...]}``. The ``notes`` list calls out
        the documented limitations (NOTE field omitted; year-less
        birthdays use Apple's ``X-APPLE-OMIT-YEAR=1604`` hack that
        corrupts to "1604" for non-Apple consumers).
        On bad input: ``validation_error``.
        On TCC denial: ``authorization_denied``.
        On unknown identifier: ``not_found`` (the message names the
        offending id).
        On unexpected failure: ``unknown``.
    """
    if not isinstance(identifiers, list) or len(identifiers) == 0:
        return _validation_error(
            "identifiers must be a non-empty list of strings"
        )
    for i, ident in enumerate(identifiers):
        if not isinstance(ident, str) or not ident.strip():
            return _validation_error(
                f"identifiers[{i}] must be a non-empty string"
            )

    auth_err = _require_contacts_authorization()
    if auth_err is not None:
        return auth_err

    try:
        vcard = connector._run_cn_export_vcard(identifiers)
    except ContactsNotFoundError as exc:
        return {
            "success": False,
            "error": str(exc),
            "error_type": "not_found",
        }
    except Exception as exc:
        logger.error("export_vcard failed: %s", exc)
        return {
            "success": False,
            "error": f"export_vcard failed: {exc}",
            "error_type": "unknown",
        }

    return {
        "success": True,
        "vcard": vcard,
        "count": len(identifiers),
        "notes": [
            "NOTE field is omitted (entitlement-gated). Use read_note() and merge separately if needed.",
            "Year-less birthdays use Apple's X-APPLE-OMIT-YEAR=1604 hack; non-Apple consumers see 1604 as the literal year.",
        ],
    }


@mcp.tool()
def import_vcard(
    vcard_text: str,
    group_identifier: str | None = None,
) -> dict[str, Any]:
    """Parse a vCard payload and persist as new contacts.

    ``vcard_text`` may contain one or more BEGIN:VCARD blocks. Both
    vCard 3.0 and 4.0 input are accepted (Apple's parser handles both).
    Atomic: parse failure, empty input, group-not-found, or save failure
    aborts the whole call. Test-mode gated like ``create_contact``.

    Args:
        vcard_text: The vCard text. Non-empty after stripping.
        group_identifier: Optional. If provided, every imported contact
            is added to the group atomically. Required in test mode for
            the safety gate (must match ``CONTACTS_TEST_GROUP``).

    Returns:
        On success: ``{"success": True, "identifiers": [...],
        "count": N, "group_id": <group_identifier>}``. Identifiers are
        returned in input order.
        On bad input (empty text or malformed vCard): ``validation_error``.
        On TCC denial: ``authorization_denied``.
        On test-mode mismatch: ``safety_violation``.
        On unknown ``group_identifier``: ``not_found``.
        On CN save failure: ``unknown``.
    """
    if not isinstance(vcard_text, str) or not vcard_text.strip():
        return _validation_error(
            "vcard_text must be a non-empty string"
        )

    auth_err = _require_contacts_authorization()
    if auth_err is not None:
        return auth_err

    safety_err = check_test_mode_safety(
        "import_vcard", group=group_identifier
    )
    if safety_err is not None:
        return safety_err

    try:
        identifiers = connector._run_cn_import_vcard(
            vcard_text, group_identifier
        )
    except ContactsNotFoundError as exc:
        return {
            "success": False,
            "error": str(exc),
            "error_type": "not_found",
        }
    except ContactsError as exc:
        # ContactsError covers both parse failures and save failures.
        # Per the plan, parse failures surface as validation_error
        # (caller's input was malformed); other ContactsErrors as
        # unknown.
        msg = str(exc)
        is_parse_failure = msg.startswith(
            "vCard parse failed"
        ) or msg == "No vCards found in input"
        if is_parse_failure:
            return {
                "success": False,
                "error": msg,
                "error_type": "validation_error",
            }
        logger.error("import_vcard failed: %s", exc)
        return {
            "success": False,
            "error": msg,
            "error_type": "unknown",
        }
    except Exception as exc:
        logger.error("import_vcard failed: %s", exc)
        return {
            "success": False,
            "error": f"import_vcard failed: {exc}",
            "error_type": "unknown",
        }

    return {
        "success": True,
        "identifiers": identifiers,
        "count": len(identifiers),
        "group_id": group_identifier,
    }


@mcp.tool()
def read_note(identifier: str) -> dict[str, Any]:
    """Read a contact's note via AppleScript.

    The ``note`` field is entitlement-gated in ``Contacts.framework``
    (only App Store apps with the ``com.apple.developer.contacts.notes``
    entitlement can read it through the framework). We're unbundled, so
    this tool routes through ``osascript`` against Contacts.app.

    Args:
        identifier: The contact's full CN identifier including the
            ``:ABPerson`` suffix (e.g., ``"BD0B...:ABPerson"``). Bare UUIDs
            are not accepted — AppleScript's ``id of person`` includes the
            suffix and won't match without it.

    Returns:
        On success: ``{"success": True, "identifier": ..., "note": ...}``.
        ``note == ""`` indicates the contact has no note set.
        On bad input: ``{"success": False, "error_type":
        "validation_error", ...}``.
        On TCC denial: ``authorization_denied``.
        On unknown identifier: ``not_found``.
        On unexpected failure: ``unknown``.
    """
    if not identifier or not identifier.strip():
        return _validation_error("identifier must be a non-empty string")

    auth_err = _require_contacts_authorization()
    if auth_err is not None:
        return auth_err

    try:
        note = connector._run_applescript_read_note(identifier)
    except ContactsNotFoundError as exc:
        return {
            "success": False,
            "error": str(exc),
            "error_type": "not_found",
        }
    except Exception as exc:
        logger.error("read_note failed: %s", exc)
        return {
            "success": False,
            "error": f"read_note failed: {exc}",
            "error_type": "unknown",
        }

    return {"success": True, "identifier": identifier, "note": note}


@mcp.tool()
def write_note(
    identifier: str,
    note: str,
    group_identifier: str | None = None,
) -> dict[str, Any]:
    """Write a contact's note via AppleScript. ``note=""`` clears the note.

    Destructive: overwrites any existing note in full (no append/diff
    semantics). Test-mode gated like ``update_contact`` — when
    ``CONTACTS_TEST_MODE=true``, the contact must belong to
    ``CONTACTS_TEST_GROUP`` (caller asserts this via the
    ``group_identifier`` argument).

    Args:
        identifier: The contact's CN identifier.
        note: The new note text. Empty string clears the note.
        group_identifier: Optional group name or CN identifier — required
            in test mode for the safety gate.

    Returns:
        On success: ``{"success": True, "identifier": ...}``.
        On bad input: ``validation_error``.
        On TCC denial: ``authorization_denied``.
        On test-mode mismatch: ``safety_violation``.
        On unknown identifier: ``not_found``.
        On unexpected failure: ``unknown``.
    """
    if not identifier or not identifier.strip():
        return _validation_error("identifier must be a non-empty string")

    auth_err = _require_contacts_authorization()
    if auth_err is not None:
        return auth_err

    safety_err = check_test_mode_safety("write_note", group=group_identifier)
    if safety_err is not None:
        return safety_err

    try:
        connector._run_applescript_write_note(identifier, note)
    except ContactsNotFoundError as exc:
        return {
            "success": False,
            "error": str(exc),
            "error_type": "not_found",
        }
    except Exception as exc:
        logger.error("write_note failed: %s", exc)
        return {
            "success": False,
            "error": f"write_note failed: {exc}",
            "error_type": "unknown",
        }

    return {"success": True, "identifier": identifier}


@mcp.tool()
def read_photo(identifier: str) -> dict[str, Any]:
    """Read a contact's photo. Returns base64-encoded bytes + detected format.

    The photo is transported as base64-encoded text — JSON-safe over the
    MCP wire. Callers decode via ``base64.b64decode(image_data)``.

    The contact-exists-but-no-photo case is a SUCCESS (``image_data:
    null``, ``format: null``, ``size_bytes: 0``) — distinct from
    ``not_found``, which means the identifier didn't match any contact.

    Args:
        identifier: The contact's CN identifier.

    Returns:
        On success (photo set): ``{"success": True, "identifier": ...,
        "image_data": "<base64>", "format": "jpeg" | "png" | "heic" |
        "gif" | "unknown", "size_bytes": <int>}``.
        On success (no photo set): ``{"success": True, "identifier":
        ..., "image_data": null, "format": null, "size_bytes": 0}``.
        On bad input: ``validation_error``.
        On TCC denial: ``authorization_denied``.
        On unknown identifier: ``not_found``.
        On unexpected failure: ``unknown``.
    """
    if not identifier or not identifier.strip():
        return _validation_error("identifier must be a non-empty string")

    auth_err = _require_contacts_authorization()
    if auth_err is not None:
        return auth_err

    try:
        photo = connector._run_cn_read_photo(identifier)
    except Exception as exc:
        logger.error("read_photo failed: %s", exc)
        return {
            "success": False,
            "error": f"read_photo failed: {exc}",
            "error_type": "unknown",
        }

    if photo is None:
        return {
            "success": False,
            "error": f"Contact not found: {identifier!r}",
            "error_type": "not_found",
        }

    if not photo["available"]:
        return {
            "success": True,
            "identifier": identifier,
            "image_data": None,
            "format": None,
            "size_bytes": 0,
        }

    raw = photo["image_data"]
    return {
        "success": True,
        "identifier": identifier,
        "image_data": base64.b64encode(raw).decode("ascii"),
        "format": detect_image_format(raw),
        "size_bytes": len(raw),
    }


@mcp.tool()
def write_photo(
    identifier: str,
    image_data: str | None,
    group_identifier: str | None = None,
) -> dict[str, Any]:
    """Set or clear a contact's photo.

    ``image_data`` is **base64-encoded** input — JSON-safe transport for
    binary. ``image_data=None`` clears the photo. Apple is the authority
    on accepted formats; bytes that Apple rejects surface as ``unknown``
    rather than ``validation_error``.

    Destructive (test-mode gated): in test mode,
    ``group_identifier`` must match ``CONTACTS_TEST_GROUP``.

    Args:
        identifier: The contact's CN identifier.
        image_data: Base64-encoded image bytes (JPEG/PNG/HEIC). ``None``
            clears the existing photo.
        group_identifier: Test-mode safety assertion. Required in test
            mode; ignored otherwise.

    Returns:
        On success: ``{"success": True, "identifier": identifier}``.
        On bad input (empty identifier or bad base64): ``validation_error``.
        On TCC denial: ``authorization_denied``.
        On test-mode mismatch: ``safety_violation``.
        On unknown identifier: ``not_found``.
        On CN save failure: ``unknown``.
    """
    if not identifier or not identifier.strip():
        return _validation_error("identifier must be a non-empty string")

    decoded: bytes | None
    if image_data is None:
        decoded = None
    else:
        if not isinstance(image_data, str):
            return _validation_error(
                "image_data must be a base64-encoded string or null"
            )
        try:
            decoded = base64.b64decode(image_data, validate=True)
        except (binascii.Error, ValueError) as exc:
            return _validation_error(
                f"image_data is not valid base64: {exc}"
            )

    auth_err = _require_contacts_authorization()
    if auth_err is not None:
        return auth_err

    safety_err = check_test_mode_safety(
        "write_photo", group=group_identifier
    )
    if safety_err is not None:
        return safety_err

    try:
        connector._run_cn_write_photo(
            identifier=identifier, image_data=decoded
        )
    except ContactsNotFoundError as exc:
        return {
            "success": False,
            "error": str(exc),
            "error_type": "not_found",
        }
    except Exception as exc:
        logger.error("write_photo failed: %s", exc)
        return {
            "success": False,
            "error": f"write_photo failed: {exc}",
            "error_type": "unknown",
        }

    return {"success": True, "identifier": identifier}


@mcp.tool()
def add_contact_to_group(
    contact_identifier: str,
    group_identifier: str,
) -> dict[str, Any]:
    """Add an existing contact to an existing group.

    Destructive (test-mode gated): the contact's group membership is
    mutated in place. The same contact may belong to multiple groups; this
    tool adds membership without disturbing existing memberships.

    Args:
        contact_identifier: The contact's CN identifier (the suffixed
            ``<UUID>:ABPerson`` form returned by other tools).
        group_identifier: The group's CN identifier (the ``id`` field
            from ``list_groups``). Required for the test-mode safety
            gate — must match ``CONTACTS_TEST_GROUP`` when
            ``CONTACTS_TEST_MODE=true``.

    Returns:
        On success: ``{"success": True, "contact_identifier": ...,
        "group_identifier": ...}``.
        On bad input: ``validation_error``.
        On TCC denial: ``authorization_denied``.
        On test-mode mismatch: ``safety_violation``.
        On unknown contact or group: ``not_found`` (the message indicates
        which entity was missing).
        On unexpected failure (including cross-container pairs):
        ``unknown`` with Apple's NSError text preserved.
    """
    if not contact_identifier or not contact_identifier.strip():
        return _validation_error(
            "contact_identifier must be a non-empty string"
        )
    if not group_identifier or not group_identifier.strip():
        return _validation_error(
            "group_identifier must be a non-empty string"
        )

    auth_err = _require_contacts_authorization()
    if auth_err is not None:
        return auth_err

    safety_err = check_test_mode_safety(
        "add_contact_to_group", group=group_identifier
    )
    if safety_err is not None:
        return safety_err

    try:
        connector._run_cn_add_contact_to_group(
            contact_identifier, group_identifier
        )
    except ContactsNotFoundError as exc:
        return {
            "success": False,
            "error": str(exc),
            "error_type": "not_found",
        }
    except Exception as exc:
        logger.error("add_contact_to_group failed: %s", exc)
        return {
            "success": False,
            "error": f"add_contact_to_group failed: {exc}",
            "error_type": "unknown",
        }

    return {
        "success": True,
        "contact_identifier": contact_identifier,
        "group_identifier": group_identifier,
    }


@mcp.tool()
def remove_contact_from_group(
    contact_identifier: str,
    group_identifier: str,
) -> dict[str, Any]:
    """Remove an existing contact from an existing group.

    Destructive (test-mode gated): the contact's group membership is
    mutated in place. The contact itself is not deleted; only the
    membership edge is removed.

    Args:
        contact_identifier: The contact's CN identifier.
        group_identifier: The group's CN identifier. Required for the
            test-mode safety gate.

    Returns:
        On success: ``{"success": True, "contact_identifier": ...,
        "group_identifier": ...}``.
        On bad input: ``validation_error``.
        On TCC denial: ``authorization_denied``.
        On test-mode mismatch: ``safety_violation``.
        On unknown contact or group: ``not_found``.
        On unexpected failure: ``unknown``.
    """
    if not contact_identifier or not contact_identifier.strip():
        return _validation_error(
            "contact_identifier must be a non-empty string"
        )
    if not group_identifier or not group_identifier.strip():
        return _validation_error(
            "group_identifier must be a non-empty string"
        )

    auth_err = _require_contacts_authorization()
    if auth_err is not None:
        return auth_err

    safety_err = check_test_mode_safety(
        "remove_contact_from_group", group=group_identifier
    )
    if safety_err is not None:
        return safety_err

    try:
        connector._run_applescript_remove_contact_from_group(
            contact_identifier, group_identifier
        )
    except ContactsNotFoundError as exc:
        return {
            "success": False,
            "error": str(exc),
            "error_type": "not_found",
        }
    except Exception as exc:
        logger.error("remove_contact_from_group failed: %s", exc)
        return {
            "success": False,
            "error": f"remove_contact_from_group failed: {exc}",
            "error_type": "unknown",
        }

    return {
        "success": True,
        "contact_identifier": contact_identifier,
        "group_identifier": group_identifier,
    }


@mcp.tool()
def create_group(
    name: str,
    container_identifier: str | None = None,
    group_identifier: str | None = None,
) -> dict[str, Any]:
    """Create a new contact group.

    Without ``container_identifier``, the group lands in the user's
    default container (typically iCloud). Pass a specific container
    UUID from ``list_containers`` to target a non-default account.

    In test mode (``CONTACTS_TEST_MODE=true``), ``group_identifier``
    must equal ``CONTACTS_TEST_GROUP`` — the assertion is "I'm
    operating within the test-group scope," same posture as
    ``create_contact``.

    Args:
        name: The new group's name. Must be non-empty after stripping.
        container_identifier: If set, write to this container instead
            of the default. CN raises on unknown identifiers; surfaces
            as ``unknown``.
        group_identifier: Test-mode safety assertion. Required in test
            mode; ignored otherwise.

    Returns:
        On success: ``{"success": True, "group": {"id": ..., "name":
        ..., "container_id": ...}}``.
        On bad input: ``validation_error``.
        On TCC denial: ``authorization_denied``.
        On test-mode mismatch: ``safety_violation``.
        On CN save failure (including unknown container): ``unknown``.
    """
    if not name or not name.strip():
        return _validation_error("name must be a non-empty string")

    auth_err = _require_contacts_authorization()
    if auth_err is not None:
        return auth_err

    safety_err = check_test_mode_safety(
        "create_group", group=group_identifier
    )
    if safety_err is not None:
        return safety_err

    try:
        group = connector._run_cn_create_group(
            name=name, container_identifier=container_identifier
        )
    except Exception as exc:
        logger.error("create_group failed: %s", exc)
        return {
            "success": False,
            "error": f"create_group failed: {exc}",
            "error_type": "unknown",
        }

    return {"success": True, "group": group}


@mcp.tool()
def rename_group(
    identifier: str,
    new_name: str,
    group_identifier: str | None = None,
) -> dict[str, Any]:
    """Rename an existing contact group.

    In test mode (``CONTACTS_TEST_MODE=true``), ``group_identifier``
    must equal ``CONTACTS_TEST_GROUP``. The asserted scope is
    independent of the target ``identifier``: a test harness may
    rename any group as long as it's operating within test-mode scope.

    Args:
        identifier: The CN identifier of the group to rename.
        new_name: The new name. Must be non-empty after stripping.
        group_identifier: Test-mode safety assertion. Required in test
            mode; ignored otherwise.

    Returns:
        On success: ``{"success": True, "group": {"id": ..., "name":
        ..., "container_id": ...}}``. ``id`` echoes the input.
        On bad input: ``validation_error``.
        On TCC denial: ``authorization_denied``.
        On test-mode mismatch: ``safety_violation``.
        On unknown ``identifier``: ``not_found``.
        On CN save failure: ``unknown``.
    """
    if not identifier or not identifier.strip():
        return _validation_error("identifier must be a non-empty string")
    if not new_name or not new_name.strip():
        return _validation_error("new_name must be a non-empty string")

    auth_err = _require_contacts_authorization()
    if auth_err is not None:
        return auth_err

    safety_err = check_test_mode_safety(
        "rename_group", group=group_identifier
    )
    if safety_err is not None:
        return safety_err

    try:
        group = connector._run_cn_rename_group(
            identifier=identifier, new_name=new_name
        )
    except ContactsNotFoundError as exc:
        return {
            "success": False,
            "error": str(exc),
            "error_type": "not_found",
        }
    except Exception as exc:
        logger.error("rename_group failed: %s", exc)
        return {
            "success": False,
            "error": f"rename_group failed: {exc}",
            "error_type": "unknown",
        }

    return {"success": True, "group": group}


@mcp.tool()
async def delete_group(
    ctx: Context,
    identifier: str,
    group_identifier: str | None = None,
) -> dict[str, Any]:
    """Delete an existing contact group.

    Outside test mode, this tool requires explicit user confirmation
    via FastMCP elicitation: "Delete group 'X' (id)? This cannot be
    undone." with Yes/No buttons before the delete runs. Same UX as
    ``delete_contact``.

    If the client doesn't support elicitation, the tool refuses with
    ``safety_violation`` pointing at ``CONTACTS_TEST_MODE`` as the
    bypass for test-harness use.

    In test mode (``CONTACTS_TEST_MODE=true``), confirmation is skipped
    and ``group_identifier`` must equal ``CONTACTS_TEST_GROUP``.

    Member contacts are NOT deleted by this operation — they keep
    existing in the address book; they just lose membership in the
    now-removed group.

    Args:
        identifier: The CN identifier of the group to delete.
        group_identifier: Test-mode safety assertion. Required in test
            mode (no other use).

    Returns:
        On success: ``{"success": True, "identifier": identifier}``.
        On bad input: ``validation_error``.
        On test-mode safety gate refusal or elicit-unsupported client:
        ``safety_violation``.
        On user declining the confirmation: ``user_declined``.
        On TCC denial: ``authorization_denied``.
        On unknown ``identifier``: ``not_found``.
        On CN save failure: ``unknown``.
    """
    if not identifier or not identifier.strip():
        return _validation_error("identifier must be a non-empty string")

    auth_err = _require_contacts_authorization()
    if auth_err is not None:
        return auth_err

    if _is_test_mode_enabled():
        safety_err = check_test_mode_safety(
            "delete_group", group=group_identifier
        )
        if safety_err is not None:
            return safety_err
    else:
        confirm_err = await _confirm_destructive(
            ctx,
            operation="delete_group",
            entity_kind="group",
            identifier=identifier,
            preview_lookup=lambda: connector._run_cn_fetch_group(identifier),
            describe=lambda g: str(g.name()),
        )
        if confirm_err is not None:
            return confirm_err

    try:
        connector._run_cn_delete_group(identifier=identifier)
    except ContactsNotFoundError as exc:
        return {
            "success": False,
            "error": str(exc),
            "error_type": "not_found",
        }
    except Exception as exc:
        logger.error("delete_group failed: %s", exc)
        return {
            "success": False,
            "error": f"delete_group failed: {exc}",
            "error_type": "unknown",
        }

    return {"success": True, "identifier": identifier}


def main() -> None:
    """Start the MCP server."""
    mcp.run()


if __name__ == "__main__":
    main()
