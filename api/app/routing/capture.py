"""Estimate finished outbound attempts after the worker and status readers settle them."""
import logging


class CostRecorder:
    def __init__(self, store, *, observed_seconds=None, batch=100):
        """``observed_seconds(target)`` may return a measured connected duration, or None."""
        self.store = store
        self.observed_seconds = observed_seconds
        self.batch = batch

    def _observed(self, target):
        if self.observed_seconds is None or target.phase == 'uncertain':
            return None
        try:
            seconds = self.observed_seconds(target)
        except Exception:
            logging.getLogger(__name__).warning('A measured call duration could not be read; using Faxbot timing.')
            return None
        return seconds if type(seconds) is int and 0 <= seconds <= 86_400 else None

    def step(self):
        """Estimate one batch; True when more finished attempts may be waiting."""
        targets = self.store.pending_captures(limit=self.batch)
        for target in targets:
            self.store.capture(target, observed_seconds=self._observed(target))
        return len(targets) == self.batch
