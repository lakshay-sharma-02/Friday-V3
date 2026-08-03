"""Permission manager — simple ACLs for the collaboration layer.

Teams share observations while keeping local control (Wave 5 goal). This
module answers one question: *may a peer perform an action?* against a
small, rank-ordered role model:

    owner (3) > member (2) > reader (1) > unknown (0)

Actions:
    discover — presence beacons are public; always allowed.
    read     — may this peer see workspace observations?
    share    — may this peer push observations into the workspace?
    admin    — may this peer manage members / workspace settings?

ACLs serialize to plain dicts so they travel with the sync protocol and
merge owner-wins (an owner's view is authoritative; role conflicts
resolve to the higher rank).

Pure stdlib; no dependencies.
"""

from __future__ import annotations

from typing import Optional

#: Role name -> rank (higher outranks lower).
_ROLE_RANK = {"owner": 3, "member": 2, "reader": 1}
_KNOWN_ROLES = frozenset(_ROLE_RANK)

#: Actions a role may perform, as a minimum-rank table.
_ACTION_MIN_RANK = {
    "discover": 0,
    "read": 1,
    "share": 2,
    "admin": 3,
}


def normalize_role(role: Optional[str]) -> str:
    """Lowercase + validate a role name; unknown roles become ``reader``.

    Refusing to invent higher privileges keeps the default safe: a typo
    like "ADMIN" or "owner " degrades to the least-privileged known role
    instead of silently escalating.
    """
    if role and role.strip().lower() in _KNOWN_ROLES:
        return role.strip().lower()
    return "reader"


class PermissionManager:
    """Rank-ordered ACL for one collaborative workspace.

    Usage:
        perms = PermissionManager(workspace="friday", owner_peer_id="a")
        perms.add_member("b", "member")
        perms.can("b", "share")   # True
        perms.can("stranger", "read")  # False
    """

    def __init__(self, workspace: str = "default",
                 owner_peer_id: Optional[str] = None,
                 allow_unknown_read: bool = False):
        self.workspace = workspace
        self.owner_peer_id = owner_peer_id
        #: Unknown peers may read (but not write) if True.
        self.allow_unknown_read = allow_unknown_read
        self._members: dict[str, str] = {}
        if owner_peer_id:
            self._members[owner_peer_id] = "owner"

    # ── Member management ──────────────────────────────────────────

    def add_member(self, peer_id: str, role: str = "member") -> None:
        if not peer_id:
            return
        role = normalize_role(role)
        self._members[peer_id] = role

    def remove_member(self, peer_id: str) -> bool:
        return self._members.pop(peer_id, None) is not None

    def role_of(self, peer_id: str) -> Optional[str]:
        return self._members.get(peer_id)

    def members(self) -> dict[str, str]:
        return dict(self._members)

    def rank_of(self, peer_id: str) -> int:
        return _ROLE_RANK.get(self.role_of(peer_id) or "", 0)

    # ── Authorization ──────────────────────────────────────────────

    def can(self, peer_id: str, action: str) -> bool:
        """Whether ``peer_id`` may perform ``action`` in this workspace."""
        if action == "discover":
            return True  # presence beacons are public metadata
        if action == "read" and self.allow_unknown_read:
            return True
        return self.rank_of(peer_id) >= _ACTION_MIN_RANK.get(action, 99)

    def can_read(self, peer_id: str) -> bool:
        return self.can(peer_id, "read")

    def can_share(self, peer_id: str) -> bool:
        return self.can(peer_id, "share")

    def can_admin(self, peer_id: str) -> bool:
        return self.can(peer_id, "admin")

    # ── Sync / merge ───────────────────────────────────────────────

    def merge(self, other: "PermissionManager") -> int:
        """Apply another workspace's ACL; owner wins conflicts.

        Returns the number of role assignments that changed.
        """
        changes = 0
        for peer_id, role in other.members().items():
            if self.rank_of(peer_id) < _ROLE_RANK[role]:
                self._members[peer_id] = role
                changes += 1
        if other.owner_peer_id and self.owner_peer_id != other.owner_peer_id:
            # The explicitly-declared owner is authoritative.
            self.owner_peer_id = other.owner_peer_id
            self._members[other.owner_peer_id] = "owner"
            changes += 1
        if other.allow_unknown_read:
            self.allow_unknown_read = True
            changes += 1
        return changes

    def serialize(self) -> dict:
        return {
            "workspace": self.workspace,
            "owner_peer_id": self.owner_peer_id,
            "allow_unknown_read": self.allow_unknown_read,
            "members": dict(self._members),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PermissionManager":
        perms = cls(
            workspace=str(data.get("workspace", "default")),
            owner_peer_id=data.get("owner_peer_id"),
            allow_unknown_read=bool(data.get("allow_unknown_read")),
        )
        for peer_id, role in (data.get("members") or {}).items():
            perms.add_member(str(peer_id), str(role))
        return perms

    def __repr__(self) -> str:
        return (f"<PermissionManager workspace={self.workspace} "
                f"members={len(self._members)}>")
