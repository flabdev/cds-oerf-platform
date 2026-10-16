"""Engagement correlation and opt-out. Provider calls belong to the channel router."""

from oerf.platform.channels.engagement import Engagement, open_engagement
from oerf.platform.channels.optout import handle_reply, is_opt_out
from oerf.platform.channels.registry import resolve_sender

__all__ = [
    "Engagement",
    "handle_reply",
    "is_opt_out",
    "open_engagement",
    "resolve_sender",
]
