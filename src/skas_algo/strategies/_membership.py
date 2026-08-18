"""Point-in-time universe membership — the shared gate.

``membership`` is {effective_iso: [symbols]} (semi-annual index rebalances; see
data/mom50_membership.json). The gate answers ONE question: was this symbol an index
member as of this date? Strategies apply it to ENTRIES ONLY — exits are never gated, so a
name that left the index still closes by its own rules rather than being orphaned.
"""

from __future__ import annotations


class MembershipGate:
    def __init__(self, membership: dict[str, list[str]] | None):
        if membership:
            self._table = {k: frozenset(v) for k, v in sorted(membership.items())}
            self._keys = sorted(self._table)
        else:
            self._table, self._keys = {}, []

    @property
    def active(self) -> bool:
        return bool(self._keys)

    def members_asof(self, today_iso: str) -> frozenset[str]:
        """Latest rebalance effective ON OR BEFORE today; dates before the first entry use
        the first list (better than an empty scan — documented in the table's caveats)."""
        chosen = self._keys[0]
        for k in self._keys:
            if k <= today_iso:
                chosen = k
            else:
                break
        return self._table[chosen]

    def allows(self, sym: str, today_iso: str) -> bool:
        return not self.active or sym in self.members_asof(today_iso)
