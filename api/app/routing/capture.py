"""Estimate finished outbound attempts after the worker and status readers settle them."""


class CostRecorder:
    def __init__(self, store, *, batch=100):
        self.store = store
        self.batch = batch

    def step(self):
        """Estimate one batch; True when more finished attempts may be waiting."""
        targets = self.store.pending_captures(limit=self.batch)
        for target in targets:
            self.store.capture(target)
        return len(targets) == self.batch
