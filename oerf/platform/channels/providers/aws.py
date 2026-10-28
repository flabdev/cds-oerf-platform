"""Live adapters. SDKs are constructed inside the existing router senders."""

from __future__ import annotations

from oerf.platform.channels.providers.base import ProviderResult


def _message_ids(result: dict) -> tuple[str, ...]:
    found: list[str] = []
    raw = result.get("message_ids")
    if isinstance(raw, (list, tuple)):
        for item in raw:
            if isinstance(item, str) and item and item not in found:
                found.append(item)
    primary = result.get("message_id")
    if isinstance(primary, str) and primary and primary not in found:
        found.insert(0, primary)
    return tuple(found)


def _from_legacy(result: dict) -> ProviderResult:
    ids = _message_ids(result)
    primary = ids[0] if ids else None
    if result.get("status") == "SIMULATED":
        return ProviderResult(
            delivered=False,
            provider_msg_id=primary,
            provider_msg_ids=ids,
            reason="simulated",
            simulated=True,
        )
    if result.get("success"):
        return ProviderResult(
            delivered=True,
            provider_msg_id=primary,
            provider_msg_ids=ids,
        )
    return ProviderResult(delivered=False, reason=str(result.get("error") or "delivery_failed"))


class WhatsAppProvider:
    def send(
        self,
        recipient: str,
        body: str,
        template_id: str,
        parameters: tuple[str, ...] = (),
    ) -> ProviderResult:
        from oerf.platform.channels.router import send_whatsapp

        return _from_legacy(
            send_whatsapp(recipient, body, template_id=template_id, parameters=parameters)
        )


class SmsProvider:
    def send(
        self,
        recipient: str,
        body: str,
        template_id: str,
        parameters: tuple[str, ...] = (),
    ) -> ProviderResult:
        from oerf.platform.channels.router import send_sms

        del parameters
        return _from_legacy(send_sms(recipient, body))


class EmailProvider:
    def send(
        self,
        recipient: str,
        body: str,
        template_id: str,
        parameters: tuple[str, ...] = (),
    ) -> ProviderResult:
        from oerf.platform.channels.router import send_email

        del parameters
        return _from_legacy(send_email(recipient, template_id, body))


class VoiceProvider:
    """Voice is one-way. A live voice SDK is not configured in this environment."""

    def send(
        self,
        recipient: str,
        body: str,
        template_id: str,
        parameters: tuple[str, ...] = (),
    ) -> ProviderResult:
        del recipient, body, template_id, parameters
        return ProviderResult(delivered=False, reason="voice_provider_not_configured")
