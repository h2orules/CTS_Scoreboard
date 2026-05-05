"""Hot-state store (Redis) for live meet data.

Phase 1 scaffold defines the key namespace contract; Phase 3 implements the
operations. Keys are always namespaced by meet_id so multi-meet hosting on a
single relay deployment stays clean.

Key scheme:
    meet:{id}:state              JSON of latest update_scoreboard payload
    meet:{id}:metadata           JSON: {host_team_name, opened_at, last_heartbeat,
                                         protocol_version, status, template_bundle_id}
    meet:{id}:fragment:{name}    JSON: {key: <12-char sha>, html: <str>}
    meet:{id}:template:{bid}     JSON: {files: [{path, content_b64}]}
    pi:{account_id}:meet_id      Reverse index: which meet ID a Pi identity owns
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

MeetStatus = Literal["live", "degraded", "closed", "expired_id_rotated"]


@dataclass(frozen=True)
class MeetKeys:
    """Helper to build the Redis key namespace for one meet."""

    meet_id: str

    @property
    def state(self) -> str:
        return f"meet:{self.meet_id}:state"

    @property
    def metadata(self) -> str:
        return f"meet:{self.meet_id}:metadata"

    def fragment(self, name: str) -> str:
        return f"meet:{self.meet_id}:fragment:{name}"

    def template(self, bundle_id: str) -> str:
        return f"meet:{self.meet_id}:template:{bundle_id}"


# TODO(phase-3): connection pool, get/set helpers, pub/sub for cross-replica
# fanout when not using Web PubSub for Socket.IO clustered mode.
