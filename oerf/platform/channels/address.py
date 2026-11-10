"""Party address for one engagement send.

Mocked channels read the contact registry. Live channels read one SSM prefix
for every role. Drivers are ``{prefix}/{tenant}/driver/{contact_id}``. Other
roles are ``{prefix}/{tenant}/{role}``. The value is never logged.

The runtime prefix is ``DEMO_NUMBERS_PREFIX`` when that variable is set, and
``/oerf/demo-numbers`` when it is not. Operator scripts pass the stage prefix
instead, and treat ``DEMO_NUMBERS_PREFIX`` as an explicit override.
"""

from __future__ import annotations

import os
from typing import Protocol

from oerf.platform.channels.registry import PhoneRegistry

DEMO_NUMBERS_PREFIX = "/oerf/demo-numbers"


def demo_numbers_prefix() -> str:
    """SSM prefix for live party numbers. Unset keeps the main-stage path."""

    raw = os.environ.get("DEMO_NUMBERS_PREFIX", DEMO_NUMBERS_PREFIX).strip()
    return raw or DEMO_NUMBERS_PREFIX


def _numbers_root(prefix: str | None) -> str:
    raw = demo_numbers_prefix() if prefix is None else prefix
    root = raw.strip().rstrip("/")
    if not root.startswith("/"):
        raise AddressError("a demo-number prefix must be an absolute SSM path")
    return root


class AddressError(ValueError):
    """The party address cannot be resolved. The message names no number."""


class ParameterReader(Protocol):
    def get(self, name: str) -> str:
        """Return one parameter value. Callers do not log it."""


def channels_are_live(mock_whatsapp: str | None = None) -> bool:
    """True only when WhatsApp mock mode is explicitly off."""

    flag = os.environ.get("USE_MOCK_WHATSAPP", "true") if mock_whatsapp is None else mock_whatsapp
    return flag.strip().lower() == "false"


def demo_number_name(tenant_id: str, role: str, *, prefix: str | None = None) -> str:
    """The SSM name for one tenant role. The value is not part of the name.

    ``prefix`` omitted uses :func:`demo_numbers_prefix`. Drivers use
    :func:`driver_demo_number_name` so the contact id stays in the path.
    """

    tenant = tenant_id.strip()
    party = role.strip()
    if not tenant or not party:
        raise AddressError("a demo-number parameter needs a tenant and a role")
    return f"{_numbers_root(prefix)}/{tenant}/{party}"


def driver_demo_number_name(
    tenant_id: str,
    contact_id: str,
    *,
    prefix: str | None = None,
) -> str:
    """The SSM name for one driver's number. The number itself is not included.

    ``prefix`` omitted uses :func:`demo_numbers_prefix`, the same root as
    every other role. Callers that know the stage pass that stage's prefix.
    """

    tenant = tenant_id.strip()
    contact = contact_id.strip()
    if not tenant or not contact:
        raise AddressError("a driver demo-number parameter needs a tenant and contact")
    return f"{_numbers_root(prefix)}/{tenant}/driver/{contact}"


def resolve_party_address(
    tenant_id: str,
    role: str,
    contact_id: str,
    *,
    registry: PhoneRegistry | None,
    parameters: ParameterReader | None,
    live: bool,
    prefix: str | None = None,
) -> str:
    """The address to send to. Live reads SSM. Mocked reads the registry.

    ``prefix`` omitted uses :func:`demo_numbers_prefix` for every role.
    """

    if live:
        if parameters is None:
            raise AddressError("live channels need the demo-numbers parameter")
        root = prefix if prefix is not None else demo_numbers_prefix()
        parameter_name = (
            driver_demo_number_name(tenant_id, contact_id, prefix=root)
            if role.strip() == "driver"
            else demo_number_name(tenant_id, role, prefix=root)
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
