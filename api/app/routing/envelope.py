"""A fax's route envelope at dispatch: the decision its sending rules made, and the route each attempt took.

The rules decide once, when a fax is accepted (``rules.evaluate.decide``), and
the decision is stored with the fax (``fax_job_rule_decisions``; the highest
sequence is current). At each attempt the planner, the transport, the delivery
store and the fallback read it here and never leave it:

- calling accounts are allowed only when the envelope lists them;
- delivery inside Faxbot (own numbers) and direct delivery to a verified
  partner stay live checks at dispatch, exactly as without rules; the envelope
  only says when a rule forbids them;
- a fax whose route a rule chose falls back to its next account only after a
  call that ended before any fax data (``strict_fallback``).

Each attempt records the account it was given, its place in the envelope and
what was skipped (``delivery_rule_choices``), before anything is sent. A fax
accepted before rules existed has no decision: everything here then answers as
Faxbot did before (``Pinned`` is None).
"""
from dataclasses import dataclass
import json
from weakref import WeakKeyDictionary

import sqlalchemy as sa

from ..rules import model


TABLES = ('fax_job_rule_decisions', 'delivery_rule_choices', 'outbound_holds')
RELAY = 'relay'
# Why an allowed account was not used for one attempt (``delivery_rule_choices.skipped``).
# ``needs_patient``: a FHIR server that needs the patient's details, for a fax without them (digital/routes.py).
SKIPS = ('turned_off', 'not_ready', 'busy', 'over_cap', 'unknown_cost', 'spending_limit', 'unavailable', 'tried',
         'needs_patient', 'not_served', 'digits')


_REFLECTED = WeakKeyDictionary()


def _table(connection, name):
    """Reflect one rules table through ``connection``; cached per engine and name."""
    cache = _REFLECTED.setdefault(connection.engine, {})
    found = cache.get(name)
    if found is None:
        found = cache[name] = sa.Table(name, sa.MetaData(), autoload_with=connection)
    return found


def tables(connection):
    """The rules tables, or None before they exist (an older database)."""
    try:
        return {name: _table(connection, name) for name in TABLES}
    except sa.exc.NoSuchTableError:
        return None


def is_relay(key):
    return isinstance(key, str) and (key == RELAY or key.startswith(RELAY + ':') or key.startswith(RELAY + '.'))


def is_digital(key):
    """A digital route key in its rule form (``dsm:<id>``, ``fhir:<id>``) or its ledger form (``dsm.<id>``)."""
    return isinstance(key, str) and model.DIGITAL_KEY.fullmatch(key.replace('.', ':', 1)) is not None


@dataclass(frozen=True)
class Pinned:
    """The current decision for one fax, as dispatch reads it."""
    decision_id: str
    sequence: int
    decision: model.Decision
    facts: model.Facts

    @property
    def envelope(self):
        return self.decision.envelope

    @property
    def automatic(self):
        return self.envelope.mode == 'automatic'

    @property
    def strict(self):
        """Fall back only after a call that ended before any fax data (a rule chose the route)."""
        return bool(self.envelope.strict_fallback)

    def _never(self, key):
        return any(item.account == key and item.why == 'never' for item in self.decision.excluded)

    def local_allowed(self):
        """Whether a rule leaves delivery inside Faxbot open; dispatch still checks the number is one of yours."""
        if self.envelope.local:
            return True
        if self._never(model.LOCAL):
            return False
        # An own number at acceptance that the envelope still keeps on the phone: a limit asked for a real call.
        return not (self.facts.own_number and not self.facts.by_call)

    def direct_allowed(self):
        """Whether a rule leaves direct delivery open; dispatch still checks the partner is verified and ready."""
        return bool(self.envelope.direct)

    def allows(self, key):
        """Whether an attempt may use ``key``: an account, ``local``, ``direct`` or a partner relay."""
        if key == model.LOCAL:
            return self.local_allowed()
        if key == model.DIRECT:
            return self.direct_allowed()
        if is_digital(key):
            # A recipient's Direct address or FHIR endpoint (digital/): named by its key or by the group "digital".
            named = key.replace('.', ':', 1)
            if self.envelope.require_direct or self._never(model.DIGITAL) or self._never(named):
                return False
            return self.automatic or named in self.envelope.accounts or model.DIGITAL in self.envelope.accounts
        if is_relay(key):
            if self.envelope.require_direct or self.envelope.require_encryption or self._never(RELAY):
                return False
            named = key.replace('.', ':', 1)
            if self._never(named):
                return False
            return self.automatic or named in self.envelope.accounts
        return key in self.envelope.accounts

    def scope(self):
        """``(scope kind, rule id)`` of what chose the route, for the attempt's record."""
        route = self.decision.route
        if route.kind == 'rule':
            return route.scope, route.rule_id
        return None, None


def _pinned(row):
    try:
        decision = model.Decision.from_json(row['decision'])
        facts = model.Facts.from_json(row['facts'])
    except (TypeError, ValueError):
        return None
    return Pinned(row['id'], row['sequence'], decision, facts)


class UnreadableDecision(RuntimeError):
    """A stored decision could not be read; dispatch then treats the fax as having no allowed route."""


def load_on(connection, job_id):
    """The fax's current decision, or None for a fax accepted without one. Raises UnreadableDecision."""
    t = tables(connection)
    if t is None:
        return None
    decisions = t['fax_job_rule_decisions']
    row = connection.execute(sa.select(decisions).where(decisions.c.job_id == job_id)
                             .order_by(decisions.c.sequence.desc()).limit(1)).mappings().one_or_none()
    if row is None:
        return None
    pinned = _pinned(row)
    if pinned is None:
        raise UnreadableDecision('The stored routing decision for this fax cannot be read.')
    return pinned


def load(engine, job_id):
    with engine.connect() as connection:
        return load_on(connection, job_id)


def choice_on(connection, attempt_id):
    """The recorded route choice of one attempt, or None."""
    t = tables(connection)
    if t is None:
        return None
    choices = t['delivery_rule_choices']
    row = connection.execute(sa.select(choices).where(choices.c.id == attempt_id)).mappings().one_or_none()
    return dict(row) if row is not None else None


def record_choice_on(connection, *, attempt_id, job_id, pinned, account_key, place, skipped=(), unreliable=(),
                     dialed=None, now):
    """Record the account one attempt was given, before anything is sent; a second call keeps the first."""
    t = tables(connection)
    if t is None or pinned is None:
        return False
    choices = t['delivery_rule_choices']
    if connection.execute(sa.select(choices.c.id).where(choices.c.id == attempt_id)).first() is not None:
        return False
    scope, rule_id = pinned.scope()
    details = {}
    if skipped:
        details['skipped'] = [{'account': key, 'why': why} for key, why in skipped]
    if unreliable:
        details['unreliable'] = list(unreliable)
    connection.execute(choices.insert().values(
        id=attempt_id, job_id=job_id, decision_id=pinned.decision_id, scope_kind=scope,
        rule_id=(rule_id or None) and str(rule_id)[:64], account_key=str(account_key)[:64], mode=pinned.envelope.mode,
        place=max(0, int(place)), dialed_number=dialed,
        skipped=json.dumps(details, sort_keys=True, separators=(',', ':')) if details else None, created_at=now))
    return True


def skipped_of(row):
    """``[(account, why)]`` and the unreliable accounts from a choice row."""
    if not row or not row.get('skipped'):
        return [], []
    try:
        details = json.loads(row['skipped'])
    except ValueError:
        return [], []
    return ([(item.get('account'), item.get('why')) for item in details.get('skipped') or ()],
            list(details.get('unreliable') or ()))
