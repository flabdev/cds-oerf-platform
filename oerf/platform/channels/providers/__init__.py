"""Outbound provider boundary. The router is the only caller."""

from oerf.platform.channels.providers.base import MessageProvider, ProviderResult

__all__ = ["MessageProvider", "ProviderResult"]
