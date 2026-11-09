"""Party address for one engagement send.

Mocked channels read the contact registry. Live channels read
``{DEMO_NUMBERS_PREFIX}/{tenant}/{role}`` from SSM. The value is never logged.
The prefix defaults to ``/oerf/demo-numbers`` and can be set with
``DEMO_NUMBERS_PREFIX``.
"""

from __future__ import annotations

import os
from typing import Protocol

from oerf.platform.channels.registry import PhoneRegistry

DEMO_NUMBERS_PREFIX = "/oerf/demo-numbers"
DRIVER_DEMO_NUMBERS_PREFIX = "/oerf/dev/demo-numbers"


def demo_numbers_prefix() -> str:
    """SSM prefix for live party numbers. Unset keeps the main-stage path."""

    raw = os.environ.get("DEMO_NUMBERS_PREFIX", DEMO_NUMBERS_PREFIX).strip()
    return raw or DEMO_NUMBERS_PREFIX


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
    return f"{demo_numbers_prefix()}/{tenant}/{party}"


def driver_demo_number_name(
    tenant_id: str,
    contact_id: str,
    *,
    prefix: str = DRIVER_DEMO_NUMBERS_PREFIX,
) -> str:
    """The SSM name for one driver's number; never the number itself.

    Operator tooling defaults to the dev prefix. The runtime passes its
    stage-specific demo-number prefix so the main stage never reads dev SSM.
    """

    tenant = tenant_id.strip()
    contact = contact_id.strip()
    if not tenant or not contact:
        raise AddressError("a driver demo-number parameter needs a tenant and contact")
    return f"{prefix.rstrip('/')}/{tenant}/driver/{contact}"


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
        parameter_name = (
            driver_demo_number_name(tenant_id, contact_id, prefix=demo_numbers_prefix())
            if role.strip() == "driver"
            else demo_number_name(tenant_id, role)
        )
        value = parameters.get(parameter_name).strip()
        if not value:
            raise AddressError("the demo-numbers parameter is empty")
        return value
    if registry is None:
        raise AddressError("mocked channels need the contact registry")
    contact = registry.get_contact(tenant_id, contact_id)
    if contact is None or not contact.e164.strip():
        raise AddressError("the contact registry has no address for this party")
    return contact.e164
