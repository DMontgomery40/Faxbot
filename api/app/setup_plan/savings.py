"""What a suggested rule would have saved over the last 30 days, from the shared pre-dial predictor (AH).

For each fax delivered in the window that a rule would have sent another way,
the predictor prices it on the route it took and on the route the rule names
(``routing.predict.predict``), with the same fax shape the scheduler uses for
one try (``routing.schedule.attempt_price``). The difference, summed, is the
predicted monthly saving. An unknown price on either side makes the whole
saving unknown, never zero, and amounts in different currencies are never
added together.
"""
from dataclasses import dataclass

from ..routing.delivered import short_money_text
from ..routing.costs import format_amount


@dataclass(frozen=True)
class Saving:
    faxes: int                    # faxes the rule would have sent another way
    micros: int | None            # None: unknown
    currency: str | None
    unknown: str | None = None    # why it is unknown, as a clause

    @property
    def known(self):
        return self.micros is not None

    def view(self):
        if not self.known:
            return None
        return {'amount': format_amount(self.micros), 'currency': self.currency, 'faxes': self.faxes,
                'estimate': True}

    def text(self):
        """'about $1.20 a month' or None."""
        if not self.known:
            return None
        return f'about {short_money_text(self.micros, self.currency)} a month'


def predicted_cost(route, destination, pages, *, now=None):
    """The predictor's cost of one fax (``Money``), or None when it is unknown."""
    from ..routing.predict import Shape, predict
    prediction = predict(route, destination, Shape(max(1, min(int(pages or 1), 10_000)), None, 'fine', 'normal'),
                         now=now)
    return prediction.cost


def monthly_saving(faxes, route, *, price=predicted_cost, now=None):
    """The saving of sending ``faxes`` ((destination, route used, pages)) by ``route`` instead."""
    total, currency, counted = 0, None, 0
    for destination, used, pages in faxes:
        if used == route:
            continue
        counted += 1
        before, after = price(used, destination, pages, now=now), price(route, destination, pages, now=now)
        if before is None or after is None:
            return Saving(counted, None, None, 'Faxbot has no price for some of these faxes')
        if before.currency != after.currency or (currency is not None and before.currency != currency):
            return Saving(counted, None, None, 'these faxes are priced in different currencies')
        currency = before.currency
        total += before.micros - after.micros
    if not counted:
        return Saving(0, 0, currency or None)
    return Saving(counted, total, currency)
