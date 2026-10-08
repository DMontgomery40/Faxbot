"""Background work: give each received document an item, fold duplicates from the direct path, then escalate.

A restart changes nothing already done: items and deadlines are stored, and an
escalation is recorded once per item. The step is safe to run from several
workers at once; the unique indexes and version checks decide.
"""
from ..routing.database import utcnow


class WorkWorker:
    def __init__(self, store, *, control, values):
        """``control`` returns the AccessControl; ``values`` the active configuration values."""
        self.store, self.control, self.values = store, control, values

    def step(self, *, now=None):
        now = now or utcnow()
        hours = getattr(self.values(), 'work_acknowledge_hours', 0) or 0
        created = self.store.feed(installation_hours=hours, now=now)
        # A partner's notice fax and a repaired call's first pages close into their document's item.
        from .duplicates import fold
        fold(self.store, self.control(), now=now)
        escalated = self.store.escalate(self.control(), now=now)
        return created >= 100 or escalated >= 100  # A full batch: run again at once.
