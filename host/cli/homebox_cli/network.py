"""Detect the local Devbox IP address."""

from __future__ import annotations

import socket


def get_local_ip() -> str:
    """Return the LAN IP of this machine by opening a UDP socket to a
    non-routable address.  No traffic is actually sent."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
