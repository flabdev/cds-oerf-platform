"""Resource ARNs for End User Messaging Social.

``get_linked_whatsapp_business_account`` returns
``arn:aws:social-messaging:{region}:{account}:waba/{id}`` and
``...:phone-number-id/{id}``. The resource segment is the id without the API
prefix. Context may still hold ``waba-...`` or ``phone-number-id-...``.
API calls keep that prefixed id.
"""

from __future__ import annotations

_WABA_PREFIX = "waba-"
_PHONE_PREFIX = "phone-number-id-"


def _resource_id(value: str, prefix: str) -> str:
    """Drop a leading API prefix. An already-bare id is unchanged."""
    if value.startswith(prefix):
        return value[len(prefix):]
    return value


def waba_arn(region: str, account: str, waba_id: str) -> str:
    """``arn:aws:social-messaging:{region}:{account}:waba/{id}``."""
    resource_id = _resource_id(waba_id, _WABA_PREFIX)
    return f"arn:aws:social-messaging:{region}:{account}:waba/{resource_id}"


def phone_number_arn(region: str, account: str, phone_number_id: str) -> str:
    """``arn:aws:social-messaging:{region}:{account}:phone-number-id/{id}``."""
    resource_id = _resource_id(phone_number_id, _PHONE_PREFIX)
    return f"arn:aws:social-messaging:{region}:{account}:phone-number-id/{resource_id}"
