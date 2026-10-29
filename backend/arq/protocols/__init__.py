"""Go-Back-N and Selective Repeat state machines."""

from __future__ import annotations

from ..config import SimulationConfig
from .base import Receiver, Sender
from .gbn import GbnReceiver, GbnSender
from .sr import SrReceiver, SrSender

_CLASSES = {"gbn": (GbnSender, GbnReceiver), "sr": (SrSender, SrReceiver)}


def create_protocol(config: SimulationConfig) -> tuple[Sender, Receiver]:
    """A fresh sender and receiver for the config's protocol."""
    sender_class, receiver_class = _CLASSES[config.protocol]
    return sender_class(config), receiver_class(config)


__all__ = [
    "GbnReceiver", "GbnSender", "Receiver", "Sender",
    "SrReceiver", "SrSender", "create_protocol",
]
