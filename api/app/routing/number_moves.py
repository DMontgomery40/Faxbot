"""Your answers about each number's dependencies, and moving a number as a checked plan (CE5, international 4).

A number can be wired into more than faxing: broadband on the same line, an
alarm or lift line, an emergency use, forms and letterhead that print it.
``DependencyStore`` keeps what you answered about each (migration 0065,
append-only; the newest answer counts).

A move (``MoveStore``) is one number going from the account that carries it
to another. Faxbot shows each step's state and the evidence for it, and never
places the port order or cancels anything:

- **Dependencies:** broadband and other lines (your answers; a "yes" blocks
  the move until it is dealt with), where the number arrives now and which
  mailbox its faxes go to (read from Faxbot), and how faxes reach staff (you
  confirm).
- **Before the port:** the new account is ready, the new route was tested,
  and you placed the port order with the new provider (each recorded by you).
- **After cutover:** you record when the carrier completed the move; then
  test receipts from at least two different routes Faxbot sends by, each one
  started by you; Faxbot matches each test fax to its arrival and reconciles
  arrivals across the old and new endpoints, counting a fax that arrived at
  both once; and it forgets what it learned about the number's calls on the
  old carrier (``engine_learning.forget``) when you ask it to.

Every state comes from stored rows, so a restart changes nothing.
"""
from __future__ import annotations

from datetime import timedelta
import json
from uuid import uuid4

import sqlalchemy as sa

from .database import read_connection, reflect, utcnow, write_transaction
from .store import RoutingConflict, RoutingInputError


QUESTIONS = {
    'broadband': ('Does this line also carry broadband?',
                  'Some carriers cannot move a number off a line that carries broadband without ending the '
                  'broadband.'),
    'other_lines': ('Do an alarm, a lift or another device share this line?',
                    'Alarm, lift and other device lines often need their own replacement before the number moves.'),
    'emergency': ('Is this number kept for emergency use?',
                  'A number kept for emergencies can be quiet for a year and still be needed.'),
    'printed': ('Is it printed on forms, letterhead or a website?',
                'Name them in the note. A printed number keeps getting faxes after you stop expecting them.'),
}
ANSWERS = ('yes', 'no', 'unknown')
ANSWER_TEXT = {'yes': 'Yes', 'no': 'No', 'unknown': 'Not sure'}
# Steps you record yourself, with what doing them means.
RECORDED = {
    'delivery': 'Faxes to it reach staff (by email or another way) and you checked it.',
    'new_account_ready': 'The new account is set up in Faxbot under Providers.',
    'new_route_tested': 'A test fax reached Faxbot through the new account.',
    'port_ordered': 'You placed the port order with the new provider.',
    'cutover': 'The new provider says the move is complete.',
}
TEST_MINUTES = 30
ADVICE_ONLY = 'Faxbot never places the port order, cancels a line or changes a provider account; it checks each step.'


def _who(engine, connection, principal_id):
    if principal_id is None:
        return None, None
    principals = reflect(engine, ('access_principals',))['access_principals']
    name = connection.execute(sa.select(principals.c.display_name).where(principals.c.id == principal_id)).scalar()
    return principal_id, name


def _day(moment):
    """'4 October 2026 at 2:52 PM MDT', in the installation's time zone."""
    from ..people_time import date_and_time
    return date_and_time(moment) if moment is not None else None


# -- your answers -------------------------------------------------------------------------------------------------------

class DependencyStore:
    def __init__(self, engine):
        self.engine = engine
        self.table = reflect(engine, ('number_dependencies',))['number_dependencies']

    def answer(self, number, question, answer, *, note=None, principal_id=None, by_name=None, now=None):
        if question not in QUESTIONS:
            raise RoutingInputError('Choose broadband, other_lines, emergency or printed.')
        if answer not in ANSWERS:
            raise RoutingInputError('Answer yes, no or unknown.')
        note = (note or '').strip() or None
        if note is not None and len(note) > 2000:
            raise RoutingInputError('Keep the note under 2,000 characters.')
        with write_transaction(self.engine) as connection:
            by, name = _who(self.engine, connection, principal_id)
            connection.execute(self.table.insert().values(
                id=uuid4().hex, number=number, question=question, answer=answer, note=note, recorded_by=by,
                recorded_by_name=(name or by_name or None) and str(name or by_name)[:200], created_at=now or utcnow()))
        return self.answers(number)

    def answers(self, number):
        """{question: newest row} for one number."""
        with read_connection(self.engine) as connection:
            rows = connection.execute(sa.select(self.table).where(self.table.c.number == number).order_by(
                self.table.c.created_at, self.table.c.id)).mappings().all()
        found = {}
        for row in rows:
            found[row['question']] = dict(row)
        return found


def dependency_rows(answers):
    """The four questions with the newest answer to each, as screens show them."""
    rows = []
    for question, (label, _) in QUESTIONS.items():
        row = answers.get(question)
        rows.append({'question': question, 'label': label, 'answer': row['answer'] if row else 'unknown',
                     'answered': row is not None, 'note': row['note'] if row else None,
                     'answered_by': row['recorded_by_name'] if row else None,
                     'answered_on': _day(row['created_at']) if row else None})
    return rows


# -- moves --------------------------------------------------------------------------------------------------------------

class MoveStore:
    def __init__(self, engine):
        self.engine = engine
        tables = reflect(engine, ('number_moves', 'number_move_events'))
        self.moves, self.events = tables['number_moves'], tables['number_move_events']

    def current(self, number):
        """(move row, its events oldest first) for the number's newest move, or (None, [])."""
        with read_connection(self.engine) as connection:
            move = connection.execute(sa.select(self.moves).where(self.moves.c.number == number).order_by(
                self.moves.c.created_at.desc(), self.moves.c.id.desc()).limit(1)).mappings().first()
            if move is None:
                return None, []
            events = connection.execute(sa.select(self.events).where(self.events.c.move_id == move['id']).order_by(
                self.events.c.created_at, self.events.c.id)).mappings().all()
        return dict(move), [dict(event) for event in events]

    def start(self, number, from_account, to_account, *, principal_id=None, by_name=None, now=None):
        if from_account == to_account:
            raise RoutingInputError('Choose another account than the one that carries the number now.')
        move, events = self.current(number)
        if move is not None and outcome(events) is None:
            raise RoutingConflict('This number already has a move in progress. Finish or abandon it first.')
        with write_transaction(self.engine) as connection:
            by, name = _who(self.engine, connection, principal_id)
            connection.execute(self.moves.insert().values(
                id=uuid4().hex, number=number, from_account=from_account, to_account=to_account, started_by=by,
                started_by_name=(name or by_name or None) and str(name or by_name)[:200], created_at=now or utcnow()))
        return self.current(number)

    def record(self, number, step, state, *, origin=None, note=None, evidence=None, principal_id=None, by_name=None,
               now=None):
        move, events = self.current(number)
        if move is None or outcome(events) is not None:
            raise RoutingConflict('This number has no move in progress. Start one first.')
        note = (note or '').strip() or None
        if note is not None and len(note) > 2000:
            raise RoutingInputError('Keep the note under 2,000 characters.')
        with write_transaction(self.engine) as connection:
            by, name = _who(self.engine, connection, principal_id)
            connection.execute(self.events.insert().values(
                id=uuid4().hex, move_id=move['id'], step=step, state=state, origin=origin, note=note,
                evidence=json.dumps(evidence, sort_keys=True) if evidence is not None else None, recorded_by=by,
                recorded_by_name=(name or by_name or None) and str(name or by_name)[:200], created_at=now or utcnow()))
        return self.current(number)


def outcome(events):
    """'finished' or 'abandoned' once the move ended, else None."""
    ended = [event for event in events if event['step'] == 'move']
    return ended[-1]['state'] if ended else None


def _latest(events, step):
    found = [event for event in events if event['step'] == step]
    return found[-1] if found else None


# -- what Faxbot reads ----------------------------------------------------------------------------------------------------

def arrivals(engine, number, since):
    """Received faxes to ``number`` since ``since``: [(when, endpoint, pages, sender)] oldest first."""
    from ..engine_frames import same_number
    faxes = reflect(engine, ('inbound_faxes',))['inbound_faxes']
    digits = ''.join(ch for ch in number if ch.isdigit())
    at = sa.func.coalesce(faxes.c.received_at, faxes.c.created_at)
    with read_connection(engine) as connection:
        rows = connection.execute(sa.select(at.label('at'), faxes.c.backend, faxes.c.inbound_backend, faxes.c.pages,
                                            faxes.c.from_number, faxes.c.to_number).where(
            faxes.c.to_number.like('%' + digits[-7:]), at >= since).order_by(at, faxes.c.id)).all()
    return [(row.at, row.inbound_backend or row.backend, row.pages, row.from_number) for row in rows
            if same_number(row.to_number or '', number)]


def test_faxes(engine, number, origin, since):
    """Delivered faxes to ``number`` sent by ``origin`` since ``since``: [(when, pages)] oldest first."""
    t = reflect(engine, ('delivery_attempt_costs', 'fax_jobs'))
    costs, jobs = t['delivery_attempt_costs'], t['fax_jobs']
    with read_connection(engine) as connection:
        rows = connection.execute(sa.select(costs.c.created_at, jobs.c.pages).select_from(
            costs.outerjoin(jobs, jobs.c.id == costs.c.job_id)).where(
            costs.c.destination == number, costs.c.outcome == 'success', costs.c.created_at >= since,
            sa.or_(costs.c.route == origin, costs.c.provider_id == origin)).order_by(costs.c.created_at)).all()
    return [(row.created_at, row.pages) for row in rows]


def _endpoint(values, key):
    """What received faxes record as the endpoint of an account: its provider ('sip', 'humblefax')."""
    from ..accounts import account_named
    account = account_named(values, key)
    return account.provider if account is not None else key


def _name(values, key):
    from ..accounts import account_named
    account = account_named(values, key)
    if account is None:
        return key
    if account.primary:
        from .predict_facts import route_label
        return route_label(account.provider, getattr(values, 'sip_trunk_preset', '') or '')
    return account.label


def reconcile(engine, values, move, events, *, now):
    """Each receipt test matched to one arrival, and arrivals since cutover at the old and new endpoints."""
    old, new = _endpoint(values, move['from_account']), _endpoint(values, move['to_account'])
    cutover = _latest(events, 'cutover')
    since = cutover['created_at'] if cutover is not None and cutover['state'] == 'done' else move['created_at']
    received = arrivals(engine, move['number'], since - timedelta(minutes=TEST_MINUTES))
    used = set()
    tests = []
    for event in [event for event in events if event['step'] == 'receipt_test' and event['state'] == 'started']:
        origin = event['origin']
        sent = test_faxes(engine, move['number'], origin, event['created_at'])
        state, sentence, where = 'not_sent', None, []
        if sent:
            when, pages = sent[0]
            for index, (at, endpoint, arrived_pages, _) in enumerate(received):
                if index in used or not (when - timedelta(minutes=1) <= at <= when + timedelta(minutes=TEST_MINUTES)):
                    continue
                if pages and arrived_pages and pages != arrived_pages:
                    continue
                if endpoint in where and old != new:
                    continue
                used.add(index)
                where.append(endpoint)
                if old == new or len(set(where)) == 2:
                    break
            if not where:
                state = 'waiting' if now - when < timedelta(minutes=TEST_MINUTES) else 'lost'
            elif old == new:
                state = 'arrived_new'
            elif set(where) == {old, new}:
                state = 'arrived_both'
            else:
                state = 'arrived_new' if where[0] == new else 'arrived_old'
        label = _name(values, origin)
        sentence = {
            'not_sent': f'Waiting for a test fax sent through {label}.',
            'waiting': f'The test fax sent through {label} has not arrived yet.',
            'lost': f'The test fax sent through {label} did not arrive within {TEST_MINUTES} minutes.',
            'arrived_new': f'The test fax sent through {label} arrived through {_name(values, move["to_account"])}.',
            'arrived_old': (f'The test fax sent through {label} still arrived through '
                            f'{_name(values, move["from_account"])}: the move has not reached every network yet.'),
            'arrived_both': (f'The test fax sent through {label} arrived through both accounts; Faxbot counts it '
                             'once.'),
        }[state]
        tests.append({'id': event['id'], 'origin': origin, 'origin_label': label,
                      'started_text': _day(event['created_at']), 'state': state, 'sentence': sentence})
    after = [item for item in received if item[0] >= since]
    counted = {'old': 0, 'new': 0, 'both': sum(1 for test in tests if test['state'] == 'arrived_both')}
    for _, endpoint, _, _ in after:
        if old != new and endpoint == old:
            counted['old'] += 1
        elif endpoint == new:
            counted['new'] += 1
    # A fax that arrived at both endpoints is one fax: it is counted once, as having reached the new one.
    counted['old'] -= counted['both']
    total = counted['old'] + counted['new']
    when = ('since the new provider completed the move' if cutover is not None and cutover['state'] == 'done'
            else 'since the move started')
    if old == new:
        sentence = (f'{total} {"fax" if total == 1 else "faxes"} arrived {when}. Both accounts hand '
                    'faxes to Faxbot the same way, so Faxbot counts each once and cannot tell which carrier '
                    'delivered it.')
    else:
        sentence = (f"{counted['new']} {'fax' if counted['new'] == 1 else 'faxes'} arrived through "
                    f"{_name(values, move['to_account'])} and {counted['old']} through "
                    f"{_name(values, move['from_account'])} {when}"
                    + (f"; {counted['both']} arrived through both and {'is' if counted['both'] == 1 else 'are'} "
                       'counted once.' if counted['both'] else '.'))
    return tests, dict(counted, sentence=sentence), old != new


def move_view(engine, values, number, *, now=None):
    """The number's move with each step's state and evidence; state 'none' when no move was started."""
    from .receiving import shown_number
    now = now or utcnow()
    store = MoveStore(engine)
    move, events = store.current(number)
    answers = DependencyStore(engine).answers(number)
    origins = _origins(values)
    accounts = _receiving_accounts(values)
    if move is None:
        return {'number': number, 'display': shown_number(number), 'state': 'none', 'from_account': None,
                'to_account': None, 'started_text': None, 'steps': [], 'tests': [], 'arrivals': None,
                'origins': origins, 'accounts': accounts, 'note': ADVICE_ONLY,
                'sentence': f'No move is planned for {shown_number(number)}. Start one when you choose the account '
                            'it should move to.'}
    tests, counted, separate = reconcile(engine, values, move, events, now=now)
    steps = _steps(engine, values, move, events, answers, tests, counted, separate)
    ended = outcome(events)
    old, new = _name(values, move['from_account']), _name(values, move['to_account'])
    if ended == 'finished':
        sentence = f'The move of {shown_number(number)} from {old} to {new} is finished.'
    elif ended == 'abandoned':
        sentence = f'The move of {shown_number(number)} from {old} to {new} was abandoned.'
    else:
        left = sum(1 for step in steps if step['state'] != 'done')
        sentence = (f'Moving {shown_number(number)} from {old} to {new}: '
                    + (f'{left} of {len(steps)} steps still to do.' if left else 'every step is done.'))
    return {'number': number, 'display': shown_number(number), 'state': ended or 'open', 'sentence': sentence,
            'from_account': old, 'to_account': new, 'started_text': _day(move['created_at']), 'steps': steps,
            'tests': tests, 'arrivals': counted, 'origins': origins, 'accounts': accounts, 'note': ADVICE_ONLY}


def _origins(values):
    """The routes Faxbot sends by, as origins for receipt tests."""
    from ..accounts import all_accounts
    return [{'key': account.key, 'label': _name(values, account.key)} for account in all_accounts(values)
            if account.sends and account.enabled]


def _receiving_accounts(values):
    from ..accounts import all_accounts
    return [{'key': account.key, 'label': _name(values, account.key)} for account in all_accounts(values)
            if account.receives]


def _mailbox(engine, number):
    from ..engine_frames import same_number
    rules = reflect(engine, ('inbound_rules',))['inbound_rules']
    digits = ''.join(ch for ch in number if ch.isdigit())
    with read_connection(engine) as connection:
        rows = connection.execute(sa.select(rules.c.to_number, rules.c.mailbox_label).where(
            rules.c.to_number.like('%' + digits[-7:]))).all()
    return next((row.mailbox_label for row in rows if same_number(row.to_number, number)), None)


def _step(step, stage, label, state, evidence=(), action=None, action_label=None):
    labels = {'done': 'Done', 'waiting': 'To do', 'blocked': 'Blocked', 'failed': 'Needs attention'}
    return {'step': step, 'stage': stage, 'label': label, 'state': state, 'state_label': labels[state],
            'evidence': [line for line in evidence if line], 'action': action, 'action_label': action_label}


def _recorded(events, step, stage, label, action_label):
    event = _latest(events, step)
    done = event is not None and event['state'] == 'done'
    evidence = []
    if event is not None:
        who = f" by {event['recorded_by_name']}" if event['recorded_by_name'] else ''
        evidence.append(f"{'Recorded' if done else 'Marked not done'}{who} on {_day(event['created_at'])}.")
        if event['note']:
            evidence.append(event['note'])
    return _step(step, stage, label, 'done' if done else 'waiting', evidence, step, action_label)


def _answered(answers, question, stage, label, *, facts=()):
    row = answers.get(question)
    if row is None or row['answer'] == 'unknown':
        return _step(question, stage, label, 'waiting', [QUESTIONS[question][1], *facts], 'answer',
                     'Answer under Numbers → Advice')
    if row['answer'] == 'yes':
        return _step(question, stage, label, 'blocked',
                     [f"You answered yes{': ' + row['note'] if row['note'] else '.'}",
                      'Arrange its replacement with your carrier before the number moves.', *facts], 'answer',
                     'Change the answer')
    return _step(question, stage, label, 'done', [f"You answered no on {_day(row['created_at'])}."])


def _steps(engine, values, move, events, answers, tests, counted, separate):
    from .number_placement import carrier_facts
    number = move['number']
    facts = [fact['sentence'] for fact in carrier_facts(number, broadband=True)]
    old, new = _name(values, move['from_account']), _name(values, move['to_account'])
    mailbox = _mailbox(engine, number)
    last = arrivals(engine, number, move['created_at'] - timedelta(days=365))
    steps = [
        _answered(answers, 'broadband', 'dependencies', 'Broadband on the same line', facts=facts),
        _answered(answers, 'other_lines', 'dependencies', 'Alarm, lift or other device lines'),
        _step('arrives', 'dependencies', 'Where it arrives now', 'done',
              [f'It is carried by {old}.'] + ([f'Its last fax arrived on {_day(last[-1][0])}.'] if last else
                                              ['No fax has arrived on it in the last year.'])),
        _step('mailbox', 'dependencies', 'Which mailbox its faxes go to', 'done' if mailbox else 'waiting',
              [f'Faxes to it go to the {mailbox} mailbox.'] if mailbox else
              ['No rule under Numbers sends its faxes to a mailbox yet; add one so the new account delivers them '
               'too.']),
        _recorded(events, 'delivery', 'dependencies', 'How its faxes reach staff', 'Record that you checked it'),
        _recorded(events, 'new_account_ready', 'before', f'{new} is set up in Faxbot', 'Record it'),
        _recorded(events, 'new_route_tested', 'before', f'A test fax arrived through {new}', 'Record it'),
        _recorded(events, 'port_ordered', 'before', f'You placed the port order with {new}', 'Record it'),
        _recorded(events, 'cutover', 'after', f'{new} says the move is complete', 'Record the date'),
    ]
    arrived = {test['origin'] for test in tests if test['state'] in ('arrived_new', 'arrived_both')}
    failed = any(test['state'] in ('arrived_old', 'lost') for test in tests)
    state = 'done' if len(arrived) >= 2 else ('failed' if failed else 'waiting')
    steps.append(_step('receipt_tests', 'after', 'Test faxes from two different routes arrive', state,
                       [test['sentence'] for test in tests] or
                       ['Start a test from each of two routes, then send one page to the number through each.'],
                       'test', 'Start a test'))
    old_after = counted['old'] if separate else 0
    reconciled = state == 'done' and not old_after
    steps.append(_step('reconciled', 'after', 'Arrivals reconciled across the old and new account',
                       'done' if reconciled else ('failed' if old_after and len(arrived) >= 2 else 'waiting'),
                       [counted['sentence']]))
    expired = _latest(events, 'facts_expired')
    evidence = []
    if expired is not None:
        count = json.loads(expired['evidence'] or '{}').get('forgotten', 0)
        evidence.append(f"Faxbot forgot {count} learned {'fact' if count == 1 else 'facts'} about this number's calls "
                        f"on {_day(expired['created_at'])}.")
    steps.append(_step('facts_expired', 'after', "What Faxbot learned about the number's calls on the old carrier is "
                       'forgotten', 'done' if expired is not None else 'waiting', evidence or
                       ["What earlier calls on the old carrier showed about this number does not hold on the new one."],
                       'forget', 'Forget it now'))
    return steps


def forget_learned(engine, number, *, principal_id=None, by_name=None, now=None):
    """Forget what earlier calls taught Faxbot about this number, and record how many facts that was."""
    from .. import engine_learning
    count = engine_learning.forget(engine, number, actor_id=principal_id, actor_name=by_name, now=now)
    return MoveStore(engine).record(number, 'facts_expired', 'done', evidence={'forgotten': count},
                                    principal_id=principal_id, by_name=by_name, now=now)
