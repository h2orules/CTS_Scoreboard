"""State key namespacing tests."""
from __future__ import annotations

from app.state import MeetKeys


def test_meet_keys_are_namespaced() -> None:
    k = MeetKeys(meet_id="abc123XYZ7890ab")
    assert k.state == "meet:abc123XYZ7890ab:state"
    assert k.metadata == "meet:abc123XYZ7890ab:metadata"
    assert k.fragment("qualifying_info", "abc123def456") == (
        "meet:abc123XYZ7890ab:fragment:qualifying_info:abc123def456"
    )
    assert k.template("d3adb33fcafe") == "meet:abc123XYZ7890ab:template:d3adb33fcafe"


def test_two_meets_do_not_collide() -> None:
    a = MeetKeys(meet_id="meetAAAAAAAAAAA")
    b = MeetKeys(meet_id="meetBBBBBBBBBBB")
    assert a.state != b.state
    assert a.fragment("x", "k1") != b.fragment("x", "k1")


def test_fragment_key_changes_with_content_hash() -> None:
    k = MeetKeys(meet_id="meetCCCCCCCCCCC")
    assert k.fragment("qt", "hashA") != k.fragment("qt", "hashB")
