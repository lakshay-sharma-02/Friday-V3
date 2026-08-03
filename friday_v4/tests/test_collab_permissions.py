"""Tests for collab.permissions — the ACL module."""

from __future__ import annotations

from friday_v4.collab.permissions import PermissionManager, normalize_role


class TestPermissionManager:
    def test_owner_created(self):
        perms = PermissionManager(workspace="ws", owner_peer_id="a")
        assert perms.role_of("a") == "owner"
        assert perms.can("a", "admin")

    def test_roles_gate_actions(self):
        perms = PermissionManager(owner_peer_id="owner")
        perms.add_member("member", "member")
        perms.add_member("reader", "reader")
        # owner can do everything
        assert perms.can("owner", "admin")
        assert perms.can("owner", "share")
        assert perms.can("owner", "read")
        # member can share + read, not admin
        assert perms.can("member", "share")
        assert perms.can("member", "read")
        assert not perms.can("member", "admin")
        # reader can read only
        assert perms.can("reader", "read")
        assert not perms.can("reader", "share")

    def test_unknown_denied_by_default(self):
        perms = PermissionManager(owner_peer_id="owner")
        assert not perms.can("stranger", "read")
        assert not perms.can("stranger", "share")
        # discovery is public metadata
        assert perms.can("stranger", "discover")

    def test_allow_unknown_read(self):
        perms = PermissionManager(owner_peer_id="owner",
                                  allow_unknown_read=True)
        assert perms.can("stranger", "read")
        assert not perms.can("stranger", "share")

    def test_normalize_role_degrades_typos(self):
        assert normalize_role("owner") == "owner"
        assert normalize_role("Member") == "member"
        assert normalize_role(" owner ") == "owner"
        assert normalize_role("superuser") == "reader"  # unknown → reader
        # Escalation attempts degrade to the least-privileged known role.
        assert normalize_role("ADMIN") == "reader"
        assert normalize_role(None) == "reader"

    def test_remove_member(self):
        perms = PermissionManager(owner_peer_id="a")
        perms.add_member("b", "member")
        assert perms.remove_member("b") is True
        assert perms.role_of("b") is None
        assert perms.remove_member("b") is False

    def test_serialize_roundtrip(self):
        perms = PermissionManager(workspace="ws", owner_peer_id="a")
        perms.add_member("b", "member")
        perms.add_member("c", "reader")
        clone = PermissionManager.from_dict(perms.serialize())
        assert clone.role_of("a") == "owner"
        assert clone.role_of("b") == "member"
        assert clone.role_of("c") == "reader"
        assert clone.workspace == "ws"

    def test_merge_owner_wins(self):
        a = PermissionManager(workspace="ws", owner_peer_id="a")
        a.add_member("b", "reader")
        b = PermissionManager(workspace="ws", owner_peer_id="b")
        b.add_member("c", "member")
        a.merge(b)
        # The merged ACL adopts b as an owner (owner-wins) and gains c.
        assert a.role_of("b") == "owner"
        assert a.owner_peer_id == "b"
        assert a.role_of("c") == "member"
        assert a.role_of("a") == "owner"  # both owners coexist
