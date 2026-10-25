"""Provider protocol shared by the fake and the live adapters."""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict


class ProviderResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    delivered: bool
    provider_msg_id: str | None = None
    reason: str | None = None
    simulated: bool = False


class MessageProvider(Protocol):
    def send(
        self,
        recipient: str,
        body: str,
        template_id: str,
        parameters: tuple[str, ...] = (),
    ) -> ProviderResult:
        """Deliver one already-rendered body. The router chooses the channel."""
