"""Web PubSub for Socket.IO transport adapter (placeholder).

Phase 1 scaffold. Phase 3 wires this up to Azure Web PubSub for Socket.IO so
that browser connections terminate at Azure (not at the Container Apps
replicas) for fanout scaling.

Until then, ``socketio.AsyncServer`` runs in-process so unit tests work
without external dependencies.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import socketio


def attach_webpubsub_adapter(sio: socketio.AsyncServer, connection_string: str, hub: str) -> None:
    """Attach Web PubSub for Socket.IO server adapter to the local sio.

    Phase 1: no-op. Phase 3: real implementation.
    """
    # TODO(phase-3): use the Web PubSub for Socket.IO server SDK to upgrade
    # `sio` to a clustered configuration. The wire protocol is unchanged so
    # browsers and the Pi keep using the existing socket.io clients.
    return None
