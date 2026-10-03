"""Reconcile provider charges after delivery has finished.

Delivery status stops changing at a final result, but a provider can price a
fax later or correct a price. This task keeps asking, records each charge once
per provider charge identity, and never touches the delivery itself. An
unknown price stays unknown; Faxbot's estimate is never copied into it.
"""
from datetime import timedelta
import logging

from .database import utcnow


class BillingReconciler:
    def __init__(self, routes, sources, *, settle_after=timedelta(hours=24), retry=timedelta(minutes=10),
                 give_up=timedelta(days=7)):
        self.routes, self.sources = routes, dict(sources)
        self.settle_after, self.retry, self.give_up = settle_after, retry, give_up

    def step(self, *, now=None):
        now = now or utcnow()
        if not self.sources:
            return False
        for row in self.routes.billing_due(set(self.sources), now=now, retry=self.retry, give_up=self.give_up):
            try:
                observations = self.sources[row['provider_id']](row)
            except Exception:
                logging.getLogger(__name__).warning('A provider charge could not be read; Faxbot will ask again later.')
                observations = None
            # A price seen a day after the call ended is treated as the settled amount.
            final = row['ended_at'] is not None and now - row['ended_at'] >= self.settle_after
            for charge in observations or ():
                self.routes.ingest_charge(row['id'], provider_id=row['provider_id'], charge_id=charge['charge_id'],
                                          amount_micros=charge['amount_micros'], currency=charge['currency'],
                                          billed_seconds=charge.get('billed_seconds'), final=final, now=now)
            self.routes.mark_billing_checked(row['id'], now=now)
        return False
