"""Deterministic provider for tests and local demo runs. No credentials."""

from __future__ import annotations

from oerf.platform.channels.providers.base import ProviderResult


class FakeProvider:
    """Records every call and returns a stable id when delivery is allowed."""

    def __init__(self, channel: str, *, succeed: bool = True, simulated: bool = False) -> None:
        self.channel = channel
        self.succeed = succeed
        self.simulated = simulated
        self.calls: list[tuple[str, str, str]] = []
        self._count = 0

    def send(self, recipient: str, body: str, template_id: str) -> ProviderResult:
        self.calls.append((recipient, body, template_id))
        self._count += 1
        if self.simulated:
            return ProviderResult(
                delivered=False,
                provider_msg_id=f"{self.channel}-{self._count}",
                reason="simulated",
                simulated=True,
            )
        if not self.succeed:
            return ProviderResult(delivered=False, reason="delivery_failed")
        return ProviderResult(
            delivered=True,
            provider_msg_id=f"{self.channel}-{self._count}",
        )
