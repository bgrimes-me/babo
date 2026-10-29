"""Message bus module for decoupled channel-agent communication."""

from babo.bus.events import InboundMessage, OutboundMessage
from babo.bus.queue import MessageBus

__all__ = ["MessageBus", "InboundMessage", "OutboundMessage"]
