"""Gather a fax's facts the way acceptance does, and answer "which route would this fax take, and why".

``FactsReader`` is the one place facts are read, so the dry run and the real
decision at acceptance (WP-C calls the same reader) cannot drift apart. The dry
run sends nothing and writes nothing.
"""
from datetime import datetime, timezone

import sqlalchemy as sa

from . import model, text
from .evaluate import decide


def accounts_from_values(values):
    """The sending accounts of a configuration: provider accounts' ``sending_accounts`` (WP-B).

    The default sending provider and ``FAX_OUTBOUND_ROUTES`` are the automatic accounts, keyed by provider
    id, in configured order, exactly as the route planner builds its candidates today; every other sending
    account follows, for rules to name.
    """
    from ..accounts import sending_accounts
    return sending_accounts(values)


def _accounts_before_provider_accounts(values):
    """The list this module built before provider accounts existed; kept for the parity test."""
    from ..routing.plan import extra_routes, route_label
    bound = getattr(values, 'effective_outbound', None)
    bound = bound() if callable(bound) else bound
    bound = bound or getattr(values, 'outbound_backend', None) or getattr(values, 'fax_backend', None)
    found = []
    if bound and model.ACCOUNT_KEY.fullmatch(bound):
        found.append(model.Account(bound, bound, route_label(bound), default=True, automatic=True,
                                   sslfax=bound == 'sip' and bool(getattr(values, 'sip_sslfax_enabled', False))))
    for key in extra_routes(values, bound):
        if model.ACCOUNT_KEY.fullmatch(key) and key not in model.RESERVED_KEYS:
            found.append(model.Account(key, key, route_label(key), automatic=True))
    return tuple(found)


def country_of(number):
    try:
        import phonenumbers
        return phonenumbers.region_code_for_number(phonenumbers.parse(number)) or None
    except Exception:
        return None


class FactsReader:
    """Reads the facts of one fax from the installation's records, at one moment.

    ``alternates`` is AD's ``approved_alternate(number)`` (returning a ``model.Alternate`` or None); until it
    lands it is absent and no fax has an approved alternate.
    """

    def __init__(self, engine, values, route_store, *, alternates=None, own_numbers=None, direct_ready=None):
        self.engine = engine
        self.values = values
        self.routes = route_store
        self.alternates = alternates
        self.own_numbers = own_numbers
        self.direct_ready = direct_ready

    def _own(self):
        if self.own_numbers is not None:
            return set(self.own_numbers)
        from ..routing.own_numbers import receiving_numbers
        try:
            return set(receiving_numbers(self.values))
        except Exception:
            return set()

    def groups(self, connection, principal_id):
        if not principal_id:
            return ()
        memberships = sa.table('access_memberships', sa.column('group_id'), sa.column('principal_id'))
        return tuple(sorted(set(connection.execute(sa.select(memberships.c.group_id).where(
            memberships.c.principal_id == principal_id)).scalars())))

    def sslfax_seen(self, connection, number):
        observations = sa.table('sslfax_observations', sa.column('number'), sa.column('accepts'),
                                sa.column('observed_at'), sa.column('id'))
        newest = connection.execute(sa.select(observations.c.accepts).where(observations.c.number == number)
                                    .order_by(observations.c.observed_at.desc(), observations.c.id.desc())
                                    .limit(1)).scalar_one_or_none()
        return newest == 1

    def quotes(self, accounts, pages, alternate, destination=None, moment=None):
        """A quote per account from the shared predictor and each plan's budget (``routing.pricing``): the same
        figures dispatch ranks by and checks caps against. Origin-rated rows arrive with WP-T."""
        from ..routing.pricing import quotes_for
        return quotes_for(self.routes, self.values, accounts, destination, max(int(pages or 1), 1), alternate,
                          now=moment)

    def read(self, *, to_number, accounts, pages=1, size_bytes=0, principal_id=None, sender_kind=None,
             key_id=None, mailbox_id=None, workflow=None, labels=(), urgent=False, by_call=False, case_packet=False,
             accepted_at=None, connection=None):
        """The facts of one fax. Pass ``connection`` to read inside the caller's transaction (acceptance)."""
        if connection is None:
            with self.engine.connect() as own:
                return self.read(to_number=to_number, accounts=accounts, pages=pages, size_bytes=size_bytes,
                                 principal_id=principal_id, sender_kind=sender_kind, key_id=key_id,
                                 mailbox_id=mailbox_id, workflow=workflow, labels=labels, urgent=urgent,
                                 by_call=by_call, case_packet=case_packet, accepted_at=accepted_at, connection=own)
        from ..routing.numbers import normalize_number
        destination = normalize_number(to_number, country=getattr(self.values, 'fax_default_country', 'US'))
        moment = accepted_at or datetime.now(timezone.utc).replace(tzinfo=None)
        if isinstance(moment, str):
            moment = datetime.fromisoformat(moment)
        row = self.routes.get_destination(destination, connection=connection)
        direct_on = getattr(self.values, 'direct_delivery_enabled', False) and (
            self.direct_ready() if self.direct_ready is not None else True)
        partner = bool(direct_on and self.routes.verified_peer(destination, connection=connection) is not None)
        alternate = self.alternates(destination) if self.alternates is not None else None
        groups = self.groups(connection, principal_id)
        seen = self.sslfax_seen(connection, destination)
        return model.Facts(
            destination=destination, accepted_at=moment.replace(microsecond=0).isoformat(),
            country=country_of(destination), recipient_id=row['id'] if row else None,
            preferred_route=row['preferred_route'] if row else None, partner=partner,
            own_number=destination in self._own(), sslfax_seen=seen, alternate=alternate,
            sender=model.Sender(principal_id, sender_kind, key_id, groups), mailbox_id=mailbox_id, workflow=workflow,
            labels=tuple(sorted(set(labels or ()))), pages=max(int(pages or 0), 0),
            size_bytes=max(int(size_bytes or 0), 0), case_packet=case_packet, urgent=urgent, by_call=by_call,
            time_zone=getattr(self.values, 'time_zone', '') or '',
            quotes=self.quotes(accounts, int(pages or 1), alternate, destination, moment))


def _money(micros, currency, plan=None):
    """An amount for the API; a plan's own fax has none ("In your plan", never "$0.00"), nor has an unknown price."""
    from ..routing.costs import format_amount
    return None if micros is None or plan is not None else {'currency': currency, 'amount': format_amount(micros)}


def _quote_text(quote):
    from ..routing.pricing import plan_text
    if quote is None:
        return plan_text(None, None)
    return plan_text(quote.plan, quote.micros)


def _step_view(step, scope_names=None):
    names = scope_names or {}
    scope = 'organization' if step.scope == model.ORGANIZATION else (
        f'{step.scope}:{step.scope_id}' if step.kind != 'preferred' else 'organization')
    if step.kind == 'preferred':
        name = f'Your preferred route for this number: {text.account_label(step.rule_id)}'
    else:
        name = step.rule_name or step.rule_id
    return {'scope': scope, 'scope_name': names.get(step.scope_id) if step.scope != model.ORGANIZATION else None,
            'rule_id': step.rule_id, 'name': name, 'kind': step.kind, 'matched': step.result == 'matched',
            'result': step.result, 'note': step.note, 'failed': text.failed_words(step)}


def explanation(decision, facts, accounts, *, scope_names=None):
    """RD's ExplainResult: outcome, one sentence, ranked routes with quotes, holds, dialed number, layout, trace."""
    envelope = decision.envelope
    zone = facts.time_zone or None
    quoted = 'alternate' if envelope.dial is not None else 'original'
    by_key = {account.key: account for account in accounts}
    routes = []
    if envelope.local:
        routes.append({'account': model.LOCAL, 'label': text.account_label(model.LOCAL), 'usable': True,
                       'sentence': 'Delivered straight into Received, with no phone call.', 'quote': None,
                       'quote_text': 'No charge', 'origin': None})
    if envelope.direct and facts.partner:
        routes.append({'account': model.DIRECT, 'label': text.account_label(model.DIRECT), 'usable': True,
                       'sentence': 'Delivered straight to the verified partner, with no fax call.', 'quote': None,
                       'quote_text': 'No charge', 'origin': None})
    for position, key in enumerate(envelope.accounts):
        quote = next((q for q in facts.quotes if q.account == key and q.number == quoted), None)
        if envelope.mode == 'ordered':
            sentence = 'First in the rule’s list.' if position == 0 else 'Used if the accounts above it are not available.'
        elif envelope.mode == 'one':
            sentence = 'The only account the rule allows.'
        else:
            sentence = 'Ranked by cost and reliability when the fax is sent.'
        if envelope.sslfax and by_key.get(key) is not None and by_key[key].sslfax:
            sentence += ' The call offers SSL Fax. ' + text.SSL_FAX
        routes.append({'account': key, 'label': text.account_label(key, accounts), 'usable': True,
                       'sentence': sentence, 'quote': _money(quote.micros, quote.currency, quote.plan) if quote else None,
                       'quote_text': _quote_text(quote), 'origin': quote.origin if quote else None})
    seen = {route['account'] for route in routes}
    for item in decision.excluded:
        if item.account in seen:
            continue
        seen.add(item.account)
        quote = next((q for q in facts.quotes if q.account == item.account and q.number == quoted), None)
        routes.append({'account': item.account, 'label': text.account_label(item.account, accounts), 'usable': False,
                       'sentence': text.excluded_sentence(item, accounts, facts, envelope.caps),
                       'quote': _money(quote.micros, quote.currency, quote.plan) if quote else None,
                       'quote_text': _quote_text(quote), 'origin': quote.origin if quote else None,
                       'soft': item.soft})
    return {
        'outcome': decision.outcome,
        'sentence': text.decision_sentence(decision, accounts, scope_names, zone),
        'route_sentence': text.route_sentence(decision, accounts, scope_names),
        'routes': routes,
        'holds': [text.hold_sentence(hold, zone) for hold in envelope.holds],
        'dial': (None if envelope.dial is None else
                 {'number': envelope.dial.number, 'sentence': text.dial_sentence(decision, facts.destination)}),
        'page_layout': envelope.page_layout,
        'subaddress': envelope.subaddress,
        'site': decision.site,
        'workflow': decision.workflow,
        'trace': [_step_view(step, scope_names) for step in decision.trace],
        'revisions': [{'scope': ref.name, 'number': ref.number} for ref in decision.revisions],
    }


def explain(compiled, facts, accounts, *, scope_names=None):
    """Decide (pure) and explain. Returns ``(decision, explanation)``."""
    decision = decide(compiled, facts, accounts)
    return decision, explanation(decision, facts, accounts, scope_names=scope_names)
