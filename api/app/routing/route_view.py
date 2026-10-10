"""Why a sent fax took its route, and applying the current rules to faxes still waiting to go.

``fax_route`` answers "Why this route" (``GET /routing/faxes/{id}/route``,
``faxbot sent route``) from what Faxbot stored, never from today's state: the
decision made when the fax was accepted (or since), and for each attempt the
account it was given, what it skipped and how it ended. Amounts are the
estimates recorded at the time, labelled as estimates; a fax a monthly plan
carried reads "In your plan".

``apply_to_waiting`` re-decides faxes that have not been sent at all under the
rules active now (``POST /routing/rules/apply-to-waiting``). It never touches a
fax with a submitted or uncertain attempt; each changed fax gets a new decision
with who applied it and why, and the change is audited.
"""
from datetime import datetime
import json
import uuid

import sqlalchemy as sa

from ..rules import model, text
from . import envelope as envelopes, holds as hold_store


REAPPLY_LIMIT = 1000


def _when(value):
    return value.isoformat(timespec='seconds') if isinstance(value, datetime) else value


def _money(micros, currency):
    from .costs import format_amount
    return None if micros is None or not currency else {'currency': currency, 'amount': format_amount(micros)}


def _labels(values):
    try:
        from ..accounts import sending_accounts
        return sending_accounts(values)
    except Exception:
        return ()


def _scope_names(engine):
    mailboxes = sa.table('mailboxes', sa.column('id'), sa.column('label'))
    try:
        with engine.connect() as connection:
            return {row[0]: row[1] for row in connection.execute(sa.select(mailboxes.c.id, mailboxes.c.label))}
    except sa.exc.SQLAlchemyError:
        return {}


def attempt_sentence(account_label, choice, decision, cost_row, attempt, next_label=None, zone=None, label=None):
    """One sentence for one attempt: what chose its account, what it skipped, and how it ended."""
    parts = []
    reason = cost_row.get('route_reason') if cost_row else None
    if choice is not None and decision is not None and choice.get('place', 0) == 0 and decision.route.kind == 'rule':
        parts.append(f'Sent by {account_label} because {text.rule_text(decision.route)} matched.')
    elif reason:
        from .plan import decided_text
        found = decided_text(cost_row.get('route'), reason)
        if found:
            parts.append(f'{account_label}: {found[:1].lower() + found[1:]}' if not found.startswith('Included')
                         else found)
    if not parts:
        parts.append(f'Sent by {account_label}.')
    skipped, unreliable = envelopes.skipped_of(choice)
    why = {'turned_off': 'it was turned off', 'not_ready': 'it was not ready', 'busy': 'it had no free line',
           'over_cap': "it was over the rule's cost cap", 'unknown_cost': "its price was unknown under a cost cap",
           'spending_limit': 'it had reached its daily spending limit', 'unavailable': 'it could not take the fax',
           'tried': 'it was already tried',
           'needs_patient': "its server needs the patient's details, which this fax does not have",
           'not_served': "it does not send faxes to this number's country",
           'pin': 'it did not show the caller ID and station ID this recipient has registered'}
    for key, reason_code in skipped:
        if key and reason_code in why:
            parts.append(f'{label(key) if label else key} was skipped: {why[reason_code]}.')
    if account_label and choice is not None and choice.get('account_key') in unreliable:
        parts.append('Faxes to this number often failed on it, but your rule lists it here.')
    phase = attempt.get('phase')
    if phase == 'failed' and next_label:
        if attempt.get('ended_before_data') == 1:
            parts.append(f'The call ended before any page was sent, so Faxbot used {next_label} next.')
        else:
            parts.append(f'It failed, so Faxbot used {next_label} next.')
    elif phase == 'failed':
        parts.append('It failed.')
    elif phase == 'uncertain':
        parts.append('Faxbot does not know whether it arrived, so it was not sent again.')
    elif phase == 'success':
        parts.append('Delivered.')
    return ' '.join(parts)


def fax_route(engine, configuration, job_id, *, rules=None, hold_view=None):
    """RD's FaxRoute for one sent fax: the sentence, each attempt, any open hold, and the replayed trace."""
    try:
        pinned = envelopes.load(engine, job_id)
    except envelopes.UnreadableDecision:
        pinned = None
    try:
        revision, _ = configuration.outbound_context(job_id)
        values = revision.values
    except Exception:
        values = None
    accounts = _labels(values) if values is not None else ()
    zone = getattr(values, 'time_zone', '') or None if values is not None else None
    names = _scope_names(engine)
    label = lambda key: text.account_label(key, accounts)  # noqa: E731
    attempts_t = sa.Table('outbound_attempts', sa.MetaData(), autoload_with=engine)
    costs_t = sa.Table('delivery_attempt_costs', sa.MetaData(), autoload_with=engine)
    with engine.connect() as connection:
        t = envelopes.tables(connection)
        rows = [dict(row) for row in connection.execute(
            sa.select(attempts_t).where(attempts_t.c.job_id == job_id, attempts_t.c.submitted_at.is_not(None))
            .order_by(attempts_t.c.sequence)).mappings()]
        costs = {row['id']: dict(row) for row in connection.execute(
            sa.select(costs_t).where(costs_t.c.job_id == job_id)).mappings()}
        choices = {}
        if t is not None:
            choices = {row['id']: dict(row) for row in connection.execute(
                sa.select(t['delivery_rule_choices']).where(t['delivery_rule_choices'].c.job_id == job_id)).mappings()}
    decision = pinned.decision if pinned is not None else None
    from .store import RouteStore
    try:
        cards = RouteStore(engine)
    except Exception:
        cards = None
    attempts = []
    for index, attempt in enumerate(rows):
        cost = costs.get(attempt['id']) or {}
        choice = choices.get(attempt['id'])
        key = (choice or {}).get('account_key') or cost.get('route')
        following = rows[index + 1] if index + 1 < len(rows) else None
        next_key = None
        if following is not None:
            next_key = (choices.get(following['id']) or {}).get('account_key') or (
                costs.get(following['id']) or {}).get('route')
        estimate = _money(cost.get('estimated_cost_micros'), cost.get('currency'))
        in_plan = cost.get('route_reason') == 'included' or _plan_fax(cards, key, cost)
        if in_plan:
            estimate = None  # a monthly plan's own fax reads "In your plan", never $0.00
        dialed = attempt.get('dialed_number')
        attempts.append({
            'number': index + 1, 'account': key, 'account_label': label(key) if key else 'Your outbound provider',
            'dialed_number': dialed if dialed and dialed != (pinned.facts.destination if pinned else None) else None,
            'alternate': _alternate(pinned, dialed),
            'page_layout': (pinned.envelope.page_layout if pinned is not None else None),
            'sentence': attempt_sentence(label(key) if key else 'your outbound provider', choice, decision, cost,
                                         attempt, label(next_key) if next_key else None, zone, label=label),
            'estimate': estimate, 'estimate_text': 'In your plan' if in_plan else None,
            'outcome': attempt.get('phase')})
    if decision is not None:
        sentence = text.decision_sentence(decision, accounts, names, zone)
        if decision.revisions:
            sentence += ' ' + '; '.join(_version_text(ref, names) for ref in decision.revisions) + '.'
    elif attempts:
        sentence = attempts[0]['sentence']
    else:
        sentence = None
    trace = _trace(engine, pinned, accounts, names, rules)
    return {'job_id': job_id, 'sentence': sentence, 'attempts': attempts, 'hold': hold_view, 'trace': trace,
            'page_layout': pinned.envelope.page_layout if pinned is not None else None,
            'dial': text.dial_sentence(decision, pinned.facts.destination) if decision is not None else None}


def _plan_fax(cards, key, cost):
    """Whether a monthly plan carried this attempt: its account's card has a monthly fee and the fax added nothing."""
    if cards is None or not key or cost.get('estimated_cost_micros') not in (0, None):
        return False
    try:
        card = cards.card_for_route(key, cost.get('provider_id') or key)
    except Exception:
        return False
    if card is None or not card.monthly_fee_micros:
        return False
    # A flat plan includes every fax; an allowance plan or bundle only when this fax cost nothing extra.
    return card.flat_plan or cost.get('estimated_cost_micros') == 0


def _version_text(ref, names):
    if ref.scope == model.ORGANIZATION:
        return f'Organization rules version {ref.number}'
    name = names.get(ref.scope_id) or ref.scope_id
    return f'{name} rules version {ref.number}'


def _alternate(pinned, dialed):
    if pinned is None or pinned.envelope.dial is None or not dialed or dialed != pinned.envelope.dial.number:
        return None
    dial = pinned.envelope.dial
    return {'original_number': pinned.facts.destination, 'approved_by': dial.approved_by,
            'approved_on': (dial.approved_at or '')[:10] or None, 'note': dial.note,
            'recipient_pays': text.toll_free(dial.number)}


def _trace(engine, pinned, accounts, names, rules):
    """Every rule, from replaying the fax's stored facts under the revisions it was decided under."""
    if pinned is None:
        return []
    from ..rules.evaluate import decide
    from ..rules.explain import _step_view
    from ..rules.store import compile_scopes
    steps = pinned.decision.trace
    if rules is not None and pinned.decision.revisions:
        try:
            ids = [ref.revision_id for ref in pinned.decision.revisions]
            with engine.connect() as connection:
                found = connection.execute(sa.select(rules.revisions).where(rules.revisions.c.id.in_(ids))
                                           ).mappings().all()
            rows = {model.scope_name(row['scope_kind'], row['scope_id']): dict(row) for row in found}
            if len(rows) == len(ids):
                steps = decide(compile_scopes(rows), pinned.facts, accounts).trace
        except Exception:
            steps = pinned.decision.trace
    return [_step_view(step, names) for step in steps]


# Apply the current rules to waiting faxes -----------------------------------------------------------------------

def _same(before, after):
    keep = lambda decision: (decision.outcome, decision.reason, model.canonical(decision.envelope),  # noqa: E731
                             model.canonical(decision.route))
    return keep(before) == keep(after)


def apply_to_waiting(delivery, rules, *, actor=None, actor_name=None, access_store=None, now=None):
    """Re-decide every fax not yet sent under the active rules. ``{'checked', 'changed', 'sentence', 'faxes'}``."""
    from ..accounts import sending_accounts
    from ..rules.evaluate import decide
    from .rules_acceptance import compiled_on
    configuration = delivery.configuration
    deliveries, attempts, jobs = delivery.deliveries, delivery.attempts, configuration.jobs
    checked, changed = 0, []
    name = actor_name or 'someone with Change settings'
    with configuration._locked() as connection:
        now = now or datetime.utcnow().replace(microsecond=0)
        t = envelopes.tables(connection)
        if t is None:
            return {'checked': 0, 'changed': 0, 'faxes': [], 'sentence': 'No fax is waiting to be sent.'}
        decisions = t['fax_job_rule_decisions']
        sent = sa.select(attempts.c.job_id).where(attempts.c.submitted_at.is_not(None))
        waiting = connection.execute(
            sa.select(deliveries.c.id, jobs.c.to_number).select_from(deliveries.join(jobs, jobs.c.id == deliveries.c.id))
            .where(deliveries.c.state == 'ready', deliveries.c.dispatch_mode == 'normal', deliveries.c.id.not_in(sent),
                   deliveries.c.id.in_(sa.select(decisions.c.job_id)))
            .order_by(deliveries.c.created_at, deliveries.c.id).limit(REAPPLY_LIMIT)).all()
        compiled = compiled_on(connection, rules)
        for job_id, to_number in waiting:
            try:
                pinned = envelopes.load_on(connection, job_id)
            except envelopes.UnreadableDecision:
                continue
            if pinned is None:
                continue
            checked += 1
            revision, _ = configuration._outbound_context(connection, job_id)
            accounts = sending_accounts(revision.values)
            decision = decide(compiled, pinned.facts, accounts)
            if _same(pinned.decision, decision):
                continue
            identity = uuid.uuid4().hex
            envelope = decision.envelope
            connection.execute(decisions.insert().values(
                id=identity, job_id=job_id, sequence=pinned.sequence + 1,
                revisions=json.dumps(decision.revision_ids, sort_keys=True, separators=(',', ':')),
                facts=pinned.facts.to_json(), facts_digest=decision.facts_digest,
                decision=decision.compact().to_json(), dial_number=envelope.dial.number if envelope.dial else None,
                approval_id=envelope.dial.approval_id if envelope.dial else None, page_layout=envelope.page_layout,
                outcome=decision.outcome, reason=f'The current rules were applied by {name}.'[:200],
                actor_principal_id=getattr(actor, 'principal_id', None), created_at=now))
            # The old decision's open holds end; the new decision's holds start now.
            holds = t['outbound_holds']
            connection.execute(holds.update().where(holds.c.job_id == job_id, holds.c.state == 'open').values(
                state='released', decided_by_principal_id=getattr(actor, 'principal_id', None),
                decided_by_name=(actor_name or None) and actor_name[:200], decided_at=now,
                reason='The current rules were applied to this fax.', version=holds.c.version + 1, updated_at=now))
            fresh = envelopes.Pinned(identity, pinned.sequence + 1, decision, pinned.facts)
            if decision.outcome != 'route':
                # A new approval binds to the document as it is now, like one made at acceptance.
                from pathlib import Path
                document = hold_store.document_sha256(Path(revision.values.fax_data_dir) / f'{job_id}.pdf')
                digest = hold_store.digest_for(fresh, job_id, pinned.facts.destination, document) if document else None
                hold_store.holds_for_decision_on(connection, t, job_id=job_id, pinned=fresh, now=now,
                                                 requested_by=pinned.facts.sender.principal_id, digest=digest,
                                                 accounts=accounts,
                                                 zone_name=getattr(revision.values, 'time_zone', '') or None)
                from ..batching.store import separate_on
                separate_on(connection, delivery._batching(connection), job_id, now)
            changed.append({'job_id': job_id, 'to_number': to_number,
                            'before': text.decision_sentence(pinned.decision, accounts),
                            'after': text.decision_sentence(decision, accounts)})
        if access_store is not None and changed:
            version = access_store.lock_on(connection)
            credential = getattr(actor, 'credential', None)
            connection.execute(access_store.tables['access_audit'].insert().values(
                id=uuid.uuid4().hex, actor_principal_id=getattr(actor, 'principal_id', None),
                actor_key_binding_id=getattr(credential, 'binding_id', None),
                actor_session_id=getattr(credential, 'session_id', None), operation='routing.rules_applied_to_waiting',
                target_kind='installation', target_id='routing_rules', policy_version_before=version,
                policy_version_after=version, outcome='allowed',
                details=json.dumps({'checked': checked, 'changed': len(changed),
                                    'faxes': [item['job_id'] for item in changed[:200]]},
                                   sort_keys=True, separators=(',', ':')), created_at=now))
    if changed:
        from ..outbound_wake import wake
        wake.notify()
    if not checked:
        sentence = 'No fax is waiting to be sent, so nothing changed.'
    elif not changed:
        sentence = (f'Checked {checked} waiting fax{"es" if checked != 1 else ""}; the current rules route '
                    f'{"them" if checked != 1 else "it"} the same way.')
    else:
        sentence = (f'Checked {checked} waiting fax{"es" if checked != 1 else ""}; {len(changed)} now '
                    f'{"go" if len(changed) != 1 else "goes"} by the current rules. Faxes already sent, or whose '
                    'outcome is uncertain, were left as they were.')
    return {'checked': checked, 'changed': len(changed), 'faxes': changed, 'sentence': sentence}
