"""Party address for one engagement send.

Mocked channels read the contact registry. Live channels read
``/oerf/demo-numbers/{tenant}/{role}`` from SSM. The value is never logged.
"""

from __future__ import annotations

import os
from typing import Protocol

from oerf.platform.channels.registry import PhoneRegistry

DEMO_NUMBERS_PREFIX = "/oerf/demo-numbers"


class AddressError(ValueError):
    """The party address cannot be resolved. The message names no number."""


class ParameterReader(Protocol):
    def get(self, name: str) -> str:
        """Return one parameter value. Callers do not log it."""


def channels_are_live(mock_whatsapp: str | None = None) -> bool:
    """True only when WhatsApp mock mode is explicitly off."""

    flag = os.environ.get("USE_MOCK_WHATSAPP", "true") if mock_whatsapp is None else mock_whatsapp
    return flag.strip().lower() == "false"


def demo_number_name(tenant_id: str, role: str) -> str:
    """The SSM name for one tenant role. The value is not part of the name."""

    tenant = tenant_id.strip()
    party = role.strip()
    if not tenant or not party:
        raise AddressError("a demo-number parameter needs a tenant and a role")
    return f"{DEMO_NUMBERS_PREFIX}/{tenant}/{party}"


def resolve_party_address(
    tenant_id: str,
    role: str,
    contact_id: str,
    *,
    registry: PhoneRegistry | None,
    parameters: ParameterReader | None,
    live: bool,
) -> str:
    """The address to send to. Live reads SSM. Mocked reads the registry."""

    if live:
        if parameters is None:
            raise AddressError("live channels need the demo-numbers parameter")
        value = parameters.get(demo_number_name(tenant_id, role)).strip()
        if not value:
            raise AddressError("the demo-numbers parameter is empty")
        return value
    if registry is None:
        raise AddressError("mocked channels need the contact registry")
    contact = registry.get_contact(tenant_id, contact_id)
    if contact is None or not contact.e164.strip():
        raise AddressError("the contact registry has no address for this party")
    return contact.e164
