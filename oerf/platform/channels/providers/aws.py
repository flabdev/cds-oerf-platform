"""Live adapters. SDKs are constructed inside the existing router senders."""

from __future__ import annotations

from oerf.platform.channels.providers.base import ProviderResult


def _from_legacy(result: dict) -> ProviderResult:
    if result.get("status") == "SIMULATED":
        return ProviderResult(
            delivered=False,
            provider_msg_id=result.get("message_id"),
            reason="simulated",
            simulated=True,
        )
    if result.get("success"):
        return ProviderResult(delivered=True, provider_msg_id=result.get("message_id"))
    return ProviderResult(delivered=False, reason=str(result.get("error") or "delivery_failed"))


class WhatsAppProvider:
    def send(self, recipient: str, body: str, template_id: str) -> ProviderResult:
        from oerf.platform.channels.router import send_whatsapp

        return _from_legacy(send_whatsapp(recipient, body))


class SmsProvider:
    def send(self, recipient: str, body: str, template_id: str) -> ProviderResult:
        from oerf.platform.channels.router import send_sms

        return _from_legacy(send_sms(recipient, body))


class EmailProvider:
    def send(self, recipient: str, body: str, template_id: str) -> ProviderResult:
        from oerf.platform.channels.router import send_email

        return _from_legacy(send_email(recipient, template_id, body))


class VoiceProvider:
    """Voice is one-way. A live voice SDK is not configured in this environment."""

    def send(self, recipient: str, body: str, template_id: str) -> ProviderResult:
        return ProviderResult(delivered=False, reason="voice_provider_not_configured")
