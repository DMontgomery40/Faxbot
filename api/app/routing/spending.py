"""What faxing cost: carrier charges where reported, rate-card estimates where not.

Reported (provider- or carrier-reported) amounts and Faxbot's estimates stay
separate. An estimate is only summed for faxes that have no reported charge,
so the two never count the same fax twice. "Waiting for the carrier's bill"
means Faxbot can read this carrier's charges and is still asking; a call it
could not match to exactly one carrier record is counted as unmatched.

A carrier record that fits no call Faxbot recorded is still money spent: it is
included in the charged total of its direction and counted separately as
"billed but not recorded by Faxbot".
"""
from datetime import timedelta

import sqlalchemy as sa

from .carriers import GIVE_UP, carrier_label
from .costs import attempt_cost, billed_seconds, money_list_text, plan_fee_for_days, plan_fee_text
from .database import read_connection, utcnow
from .plan import route_label


# Trunk presets whose carrier charges Faxbot reads, and the providers it asks itself.
CARRIER_PRESETS = ('telnyx',)
REPORTING_PROVIDERS = ('signalwire',)


def _add(bucket, currency, micros):
    if currency and micros is not None:
        bucket[currency] = bucket.get(currency, 0) + int(micros)


def _plural(count, one, many=None):
    return f'{count} {one if count == 1 else (many or one + "s")}'


class Spending:
    def __init__(self, routes, carriers, *, carrier_presets=CARRIER_PRESETS, reporting=REPORTING_PROVIDERS):
        self.routes, self.carriers = routes, carriers
        self.carrier_presets, self.reporting = tuple(carrier_presets), tuple(reporting)

    # Shared lookups -------------------------------------------------------------
    def _outbound_calls(self, connection, condition):
        """{attempt id: (trunk preset, check state, call ended)} for attempts matching ``condition``."""
        calls, checks, costs = self.carriers.calls, self.carriers.checks, self.routes.costs
        rows = connection.execute(
            sa.select(calls.c.attempt_id, calls.c.trunk_preset, checks.c.state, calls.c.ended_at)
            .select_from(costs.join(calls, calls.c.attempt_id == costs.c.id)
                         .outerjoin(checks, checks.c.id == calls.c.id))
            .where(calls.c.direction == 'outbound', condition)
            .order_by(calls.c.started_at)).all()
        return {attempt: (preset, state, ended) for attempt, preset, state, ended in rows}

    def _reported_charges(self, connection, condition):
        """{attempt id: (carrier, billed seconds or None)} from recorded provider charges."""
        charges, costs = self.routes.charges, self.routes.costs
        rows = connection.execute(
            sa.select(charges.c.attempt_id, charges.c.provider_id, charges.c.billed_seconds)
            .select_from(charges.join(costs, costs.c.id == charges.c.attempt_id)).where(condition)).all()
        result = {}
        for attempt, carrier, seconds in rows:
            _, total = result.get(attempt, (carrier, None))
            result[attempt] = (carrier, seconds if total is None else total + (seconds or 0))
        return result

    def _awaiting(self, row, call, now):
        """True while Faxbot can still expect a charge report for this finished attempt."""
        if row['reported_cost_micros'] is not None or row['outcome'] not in ('success', 'failed'):
            return False
        if row['provider_id'] in self.reporting:
            return bool(row['provider_sid']) and row['created_at'] >= now - GIVE_UP
        if call is None:
            return False
        preset, state, ended = call
        return (preset in self.carrier_presets and state not in ('ambiguous', 'unreported')
                and (ended is None or ended >= now - GIVE_UP))

    # Spending ----------------------------------------------------------------
    def outbound(self, since, *, now=None):
        """Per provider: faxes, billed minutes, reported charges, estimates for the rest and open counts."""
        now = now or utcnow()
        c = self.routes.costs
        window = sa.and_(c.c.created_at >= since, c.c.outcome != 'pending')
        with read_connection(self.routes.engine) as connection:
            rows = connection.execute(sa.select(c).where(window)).mappings().all()
            calls = self._outbound_calls(connection, window)
            reported = self._reported_charges(connection, window)
        cards = {card.provider_id: card for card in self.routes.current_cards() if card.direction == 'outbound'}
        totals = {}
        for row in rows:
            entry = totals.setdefault(row['provider_id'], {
                'provider_id': row['provider_id'], 'attempts': 0, 'successes': 0, 'failures': 0, 'uncertain': 0,
                'billed_seconds': 0, 'billed_pages': 0, 'cost_micros': {}, 'reported_cost_micros': {},
                'settled_cost_micros': {}, 'unreported': 0, 'reported': 0, 'unreported_estimate_micros': {},
                'awaiting': 0, 'unmatched': 0, 'carriers': set()})
            entry['attempts'] += 1
            entry['successes'] += row['outcome'] == 'success'
            entry['failures'] += row['outcome'] == 'failed'
            entry['uncertain'] += row['outcome'] == 'uncertain'
            carrier, seconds = reported.get(row['id'], (None, None))
            entry['billed_seconds'] += int(seconds if seconds is not None else row['billed_seconds'] or 0)
            entry['billed_pages'] += int(row['billed_pages'] or 0)
            _add(entry['cost_micros'], row['currency'], row['estimated_cost_micros'])
            _add(entry['reported_cost_micros'], row['reported_currency'], row['reported_cost_micros'])
            _add(entry['settled_cost_micros'], row['reported_currency'], row['settled_cost_micros'])
            call = calls.get(row['id'])
            if carrier:
                entry['carriers'].add(carrier)
            elif call is not None and call[0] in self.carrier_presets:
                entry['carriers'].add(call[0])
            if row['reported_cost_micros'] is None:
                entry['unreported'] += 1
                _add(entry['unreported_estimate_micros'], row['currency'], row['estimated_cost_micros'])
                entry['awaiting'] += self._awaiting(row, call, now)
                entry['unmatched'] += call is not None and call[1] == 'ambiguous'
            else:
                entry['reported'] += 1
        unrecorded = [row for row in self.carriers.unrecorded_in_effect(since=since) if row['direction'] == 'outbound']
        if unrecorded:
            entry = totals.setdefault('sip', self._empty_outbound('sip'))
            self._add_unrecorded(entry, unrecorded)
            entry['carriers'].update(row['provider_id'] for row in unrecorded)
        days = max(1, round((now - since).total_seconds() / 86_400))
        for entry in totals.values():
            card = cards.get(entry['provider_id'])
            entry['plan_card'] = card if card is not None and card.flat_plan else None
            entry['plan_fee_micros'] = plan_fee_for_days(card, days) if entry['plan_card'] is not None else 0
            entry['plan_days'] = days
            entry['has_card'] = self.routes.card_for(entry['provider_id']) is not None
            entry['total_micros'] = self.total(entry)
        return sorted(totals.values(), key=lambda entry: entry['provider_id'])

    @staticmethod
    def total(entry):
        """What a card cost: charges, estimates only for faxes not billed yet, and a plan's fee for the period."""
        total = {}
        for bucket in (entry['reported_cost_micros'], entry['unreported_estimate_micros']):
            for currency, micros in bucket.items():
                _add(total, currency, micros)
        card = entry.get('plan_card')
        if card is not None:
            _add(total, card.currency, entry.get('plan_fee_micros', 0))
        return total

    @staticmethod
    def _empty_outbound(provider_id):
        return {'provider_id': provider_id, 'attempts': 0, 'successes': 0, 'failures': 0, 'uncertain': 0,
                'billed_seconds': 0, 'billed_pages': 0, 'cost_micros': {}, 'reported_cost_micros': {},
                'settled_cost_micros': {}, 'unreported': 0, 'reported': 0, 'unreported_estimate_micros': {},
                'awaiting': 0, 'unmatched': 0, 'carriers': set()}

    @staticmethod
    def _add_unrecorded(entry, rows):
        """Fold carrier records Faxbot has no call for into a card's charged total, and count them."""
        entry.setdefault('unrecorded', 0)
        entry.setdefault('unrecorded_micros', {})
        entry.setdefault('unrecorded_attached', 0)
        entry.setdefault('unrecorded_unattached_micros', {})
        for row in rows:
            entry['unrecorded'] += 1
            entry['unrecorded_attached'] += row['inbound_fax_id'] is not None
            _add(entry['unrecorded_micros'], row['currency'], row['amount_micros'])
            if row['inbound_fax_id'] is None:
                _add(entry['unrecorded_unattached_micros'], row['currency'], row['amount_micros'])
            _add(entry['reported_cost_micros'], row['currency'], row['amount_micros'])
            entry['billed_seconds'] += int(row['billed_seconds'] or 0)

    def received(self, since, *, now=None):
        """Per trunk carrier: received calls, faxes, billed minutes, charges and estimates."""
        now = now or utcnow()
        calls, checks = self.carriers.calls, self.carriers.checks
        with read_connection(self.routes.engine) as connection:
            rows = connection.execute(
                sa.select(calls, checks.c.state).select_from(calls.outerjoin(checks, checks.c.id == calls.c.id))
                .where(calls.c.direction == 'inbound', calls.c.started_at >= since)).mappings().all()
            effective = self.carriers.in_effect([row['id'] for row in rows], connection)
        cards = {(card.provider_id, card.direction): card for card in self.routes.current_cards()}
        totals = {}
        for row in rows:
            preset = row['trunk_preset'] or ''
            entry = totals.setdefault(preset, {
                'provider_id': 'sip', 'carrier': preset or None, 'calls': 0, 'faxes': 0, 'billed_seconds': 0,
                'cost_micros': {}, 'reported_cost_micros': {}, 'unreported_estimate_micros': {}, 'reported': 0,
                'unreported': 0, 'awaiting': 0, 'unmatched': 0})
            card = cards.get((f'sip-{preset}', 'inbound')) or cards.get(('sip', 'inbound'))
            seconds = row['connected_seconds']
            estimate = (attempt_cost(card, seconds=seconds, pages=row['pages'], delivered=row['job_id'] is not None)
                        if card is not None and row['ended_at'] is not None else None)
            entry['calls'] += 1
            entry['faxes'] += row['job_id'] is not None
            _add(entry['cost_micros'], card.currency if card else None, estimate)
            charges = effective.get(row['id'], [])
            if charges:
                entry['reported'] += 1
                for charge in charges:
                    _add(entry['reported_cost_micros'], charge['currency'], charge['amount_micros'])
                carrier_seconds = [charge['billed_seconds'] for charge in charges if charge['billed_seconds'] is not None]
                entry['billed_seconds'] += sum(carrier_seconds) if carrier_seconds else (
                    billed_seconds(card, seconds) if card else 0)
                continue
            entry['unreported'] += 1
            entry['billed_seconds'] += billed_seconds(card, seconds) if card else 0
            _add(entry['unreported_estimate_micros'], card.currency if card else None, estimate)
            entry['unmatched'] += row['state'] == 'ambiguous'
            entry['awaiting'] += (preset in self.carrier_presets and row['ended_at'] is not None
                                  and row['state'] not in ('ambiguous', 'unreported')
                                  and row['ended_at'] >= now - GIVE_UP)
        for row in self.carriers.unrecorded_in_effect(since=since):
            if row['direction'] != 'inbound':
                continue
            entry = totals.setdefault(row['provider_id'], {
                'provider_id': 'sip', 'carrier': row['provider_id'], 'calls': 0, 'faxes': 0, 'billed_seconds': 0,
                'cost_micros': {}, 'reported_cost_micros': {}, 'unreported_estimate_micros': {}, 'reported': 0,
                'unreported': 0, 'awaiting': 0, 'unmatched': 0})
            self._add_unrecorded(entry, [row])
        for entry in totals.values():
            entry.setdefault('unrecorded', 0)
            entry.setdefault('unrecorded_micros', {})
            entry.setdefault('unrecorded_attached', 0)
            entry.setdefault('unrecorded_unattached_micros', {})
            entry['total_micros'] = self.total(entry)
        return [totals[key] for key in sorted(totals)]

    # One fax ---------------------------------------------------------------------
    def _shares(self, connection, job_id):
        """{call attempt: (this fax's pages, all the call's pages)} for calls this fax shared with other faxes.

        A call that carried several faxes is costed once, on the attempt that
        placed it; each fax's part of it is split by pages, each fax counting
        its separator page, as the fax's sending-together details say.
        """
        members = self.routes.batch_members()
        if members is None:
            return {}
        together = members.c.state == 'together'
        result = {}
        for batch_id, pages in connection.execute(sa.select(members.c.batch_id, members.c.pages).where(
                members.c.id == job_id, together, members.c.batch_id.is_not(None))).all():
            count, total = connection.execute(sa.select(sa.func.count(), sa.func.sum(members.c.pages + 1)).where(
                members.c.batch_id == batch_id, together)).one()
            if count > 1 and total:
                result[batch_id] = (pages + 1, int(total))
        return result

    @staticmethod
    def _part(micros, share):
        if micros is None:
            return None
        part, total = share
        return (int(micros) * part + total // 2) // total

    def job(self, job_id, *, now=None):
        """The cost of one sent fax across all its attempts, including charged failures.

        A fax sent together with others in one call costs its share of that call
        (an estimate until the carrier reports the call), whichever fax placed it;
        the whole call is counted once only in the spending totals.
        """
        now = now or utcnow()
        c = self.routes.costs
        with read_connection(self.routes.engine) as connection:
            shares = self._shares(connection, job_id)
            mine = sa.or_(c.c.job_id == job_id, c.c.id.in_(list(shares))) if shares else c.c.job_id == job_id
            found = connection.execute(sa.select(c).where(mine, c.c.outcome.in_(('success', 'failed', 'uncertain')))
                                       .order_by(c.c.created_at, c.c.id)).mappings().all()
            calls = self._outbound_calls(connection, mine)
            reported = self._reported_charges(connection, mine)
            # The route of an attempt still in progress, known as soon as Faxbot chose it.
            pending = connection.execute(sa.select(c.c.provider_id).where(mine, c.c.outcome == 'pending').order_by(
                c.c.created_at.desc(), c.c.id.desc()).limit(1)).scalar()
        rows = []
        for row in found:
            share = shares.get(row['id'])
            rows.append(dict(row) if share is None else {
                **row, 'estimated_cost_micros': self._part(row['estimated_cost_micros'], share),
                'reported_cost_micros': self._part(row['reported_cost_micros'], share)})
        shared = any(row['id'] in shares for row in rows)
        if not rows:
            card = self.routes.card_for(pending) if pending else None
            if card is not None and card.flat_plan:
                # A fax through a flat plan costs nothing more, whatever happens to the call.
                fee = plan_fee_text(card.monthly_fee_micros, card.currency)
                return {'state': 'included', 'summary': f'Included in your {route_label(pending)} plan ({fee} a month).',
                        'reported_cost': {}, 'estimated_cost': {}, 'attempts': 0}
            return {'state': 'none', 'summary': None, 'reported_cost': {}, 'estimated_cost': {}, 'attempts': 0}
        reported_total, estimated_total, carriers = {}, {}, set()
        waiting = unmatched = done = 0
        for row in rows:
            _add(estimated_total, row['currency'], row['estimated_cost_micros'])
            if row['reported_cost_micros'] is not None:
                done += 1
                _add(reported_total, row['reported_currency'], row['reported_cost_micros'])
                carriers.add(reported.get(row['id'], (row['provider_id'], None))[0])
                continue
            call = calls.get(row['id'])
            unmatched += call is not None and call[1] == 'ambiguous'
            waiting += 1
            if call is not None and call[0]:
                carriers.add(call[0])
        sip = all(row['provider_id'] == 'sip' for row in rows)
        unit = 'call' if sip else 'attempt'
        who = carrier_label(next(iter(carriers))) if len(carriers) == 1 else 'Your providers'
        if done and not waiting:
            if len(rows) == 1 and shared:
                summary = f"{who} charged {money_list_text(reported_total)} for this fax's share of the call."
            elif len(rows) == 1:
                summary = f'{who} charged {money_list_text(reported_total)} for this {"call" if sip else "fax"}.'
            else:
                summary = f'{who} charged {money_list_text(reported_total)} for {_plural(len(rows), unit)}.'
            state = 'reported'
        elif done:
            summary = (f'{who} charged {money_list_text(reported_total)} so far; the cost of '
                       f'{_plural(waiting, "more " + unit)} is not reported yet.')
            state = 'partial'
        elif unmatched:
            summary = (f'Faxbot could not match this call to one {who} record, so its cost is unknown.'
                       if len(rows) == 1 else
                       f'Faxbot could not match {_plural(unmatched, unit)} to one {who} record, so the cost is unknown.')
            state = 'unmatched'
        else:
            card = self.routes.card_for(rows[-1]['provider_id'])
            if card is not None and card.flat_plan:
                fee = plan_fee_text(card.monthly_fee_micros, card.currency)
                summary, state = f'Included in your {route_label(rows[-1]["provider_id"])} plan ({fee} a month).', 'included'
            else:
                summary, state = 'Cost not reported yet.', 'waiting'
        return {'state': state, 'summary': summary, 'reported_cost': reported_total,
                'estimated_cost': estimated_total, 'attempts': len(rows)}

    def inbound(self, inbound_id):
        """The carrier's charge for the call that brought in one received fax."""
        calls, checks = self.carriers.calls, self.carriers.checks
        with read_connection(self.routes.engine) as connection:
            rows = connection.execute(
                sa.select(calls.c.id, calls.c.trunk_preset, checks.c.state)
                .select_from(calls.outerjoin(checks, checks.c.id == calls.c.id))
                .where(calls.c.direction == 'inbound', calls.c.job_id == inbound_id)).all()
            effective = self.carriers.in_effect([row.id for row in rows], connection)
            unrecorded = self.carriers.unrecorded_in_effect(inbound_fax_ids=[inbound_id], connection=connection)
            backend = connection.execute(sa.select(self.carriers.faxes.c.backend).where(
                self.carriers.faxes.c.id == inbound_id)).scalar_one_or_none()
        total, carriers = {}, set()
        for row in unrecorded:
            # The carrier's record matched this fax by number and time; Faxbot kept no call record of it.
            _add(total, row['currency'], row['amount_micros'])
            carriers.add(row['provider_id'])
        if not rows:
            if total:
                return {'state': 'reported', 'reported_cost': total,
                        'summary': f'{carrier_label(next(iter(carriers)))} charged {money_list_text(total)} for this call.'}
            if backend == 'sip':
                return {'state': 'waiting', 'summary': 'Cost not reported yet.', 'reported_cost': {}}
            return {'state': 'none', 'summary': None, 'reported_cost': {}}
        for row in rows:
            for charge in effective.get(row.id, []):
                _add(total, charge['currency'], charge['amount_micros'])
                carriers.add(charge['provider_id'])
        preset = rows[0].trunk_preset
        who = carrier_label(next(iter(carriers)) if carriers else (preset or ''))
        if total:
            return {'state': 'reported', 'summary': f'{who} charged {money_list_text(total)} for this call.',
                    'reported_cost': total}
        if any(row.state == 'ambiguous' for row in rows):
            return {'state': 'unmatched', 'reported_cost': {},
                    'summary': f'Faxbot could not match this call to one {who} record, so its cost is unknown.'}
        return {'state': 'waiting', 'summary': 'Cost not reported yet.', 'reported_cost': {}}

    @staticmethod
    def since_default(now=None, days=30):
        return (now or utcnow()) - timedelta(days=days)
