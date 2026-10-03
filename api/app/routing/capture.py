"""Price finished outbound attempts after the worker and status readers settle them."""


class CostRecorder:
    def __init__(self, store, *, reporter=None, batch=100):
        self.store = store
        self.reporter = reporter
        self.batch = batch

    def step(self):
        """Capture one batch; True when more finished attempts may be waiting."""
        targets = self.store.pending_captures(limit=self.batch)
        for target in targets:
            reported = None
            if self.reporter is not None and target.phase != 'uncertain':
                try:
                    reported = self.reporter(target)
                except Exception:
                    reported = None  # Provider charges are optional evidence.
            micros, currency, seconds = reported if reported else (None, None, None)
            self.store.capture(target, reported_cost_micros=micros, reported_currency=currency,
                               reported_seconds=seconds)
        return len(targets) == self.batch
