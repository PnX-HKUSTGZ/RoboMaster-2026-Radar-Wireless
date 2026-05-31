"""RM2026 wireless-link decoding and referee integration."""

__all__ = [
    "SequentialWirelessReceiver",
    "TEAM_BLUE",
    "TEAM_RED",
    "WirelessLinkConfig",
    "WirelessLinkStatus",
]


def __getattr__(name: str):
    if name in ("TEAM_BLUE", "TEAM_RED"):
        from . import protocol

        return getattr(protocol, name)
    if name in ("SequentialWirelessReceiver", "WirelessLinkConfig", "WirelessLinkStatus"):
        from . import sequential_receiver

        return getattr(sequential_receiver, name)
    raise AttributeError(name)
