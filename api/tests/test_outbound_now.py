"""The live outbound board (GET /outbound/now): every section from seeded deliveries, per-person visibility, and
reads that take no write lock (SQLite and PostgreSQL)."""
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
import sqlalchemy as sa

from api.app import outbound_now
from api.app.access.fax_resources import FaxResources
from api.app.access.outbound import AuthorizedOutbound
from api.app.access.policy import AccessControl
from api.app.access.queries import AuthorizedFaxQueries
from api.app.config_profiles import ProviderConfiguration
from api.app.config_store import ConfigurationStore
from api.app.config_values import ConfigurationValues
from api.app.outbound_store import OutboundStore
from api.app.routing import envelope as envelopes, holds
from api.app.routing.schedule import Decision
from api.tests.test_access_policy import NOW, World
from api.tests.test_schema import database  # noqa: F401 (fixture)


NUMBER, OTHER, THIRD = '+12025550123', '+12025550124', '+12025550125'


def mask(number):
    return '****' + str(number or '')[-4:]


class Desk:
    """One installation with an administrator (olive) and two senders who each see only their own faxes."""

    def __init__(self, database, tmp_path, **environment):
        self.world = World(database)
        self.olive = self.world.user('olive')
        self.world.assignment('olive', 'role_administrator')
        self.alice = self.world.user('alice')
        self.world.assignment('alice', 'role_fax_operator', 'personal-alice')
        self.bob = self.world.user('bob')
        self.world.assignment('bob', 'role_fax_operator', 'personal-bob')
        self.engine = database
        self.configuration = ConfigurationStore(database, tmp_path / 'installation.key')
        values = ConfigurationValues.from_environment({
            'FAX_BACKEND': 'phaxio', 'FAX_DISABLED': 'false', 'FAX_DATA_DIR': str(tmp_path / 'faxdata'),
            **environment})
        self.snapshot = self.configuration.initialize(values, actor='test', providers={
            'outbound': ProviderConfiguration('phaxio', credentials={'api_key': 'synthetic-key'})})
        control = AccessControl(self.configuration.access_store)
        resources = FaxResources(control)
        self.access = SimpleNamespace(store=self.configuration.access_store, control=control,
                                      fax_resources=resources)
        self.outbound = AuthorizedOutbound(self.configuration, resources, clock=lambda: NOW)
        self.queries = AuthorizedFaxQueries(self.configuration, resources, clock=lambda: NOW)
        self.store = OutboundStore(self.configuration)
        self.clock = 0.0
        self.accepted = 0
        self.board = outbound_now.Board(database, clock=lambda: self.clock)

    @property
    def values(self):
        return self.configuration.read().active.values

    def accept(self, who=None, to=NUMBER, *, at=None, trunk=False, pages=1):
        self.accepted += 1
        job, when = uuid4().hex, at or NOW - timedelta(minutes=30) + timedelta(seconds=self.accepted)
        self.outbound.accept(who or self.olive, self.snapshot.active, {
            'id': job, 'to_number': to, 'file_name': 'synthetic.pdf', 'tiff_path': '', 'status': 'queued',
            'pages': pages, 'created_at': when, 'updated_at': when})
        with self.engine.begin() as connection:
            connection.execute(sa.text('UPDATE outbound_deliveries SET created_at = :at, updated_at = :at '
                                       'WHERE id = :id'), {'at': when, 'id': job})
            if trunk:
                connection.execute(sa.text("UPDATE fax_jobs SET backend = 'sip' WHERE id = :id"), {'id': job})
        return job

    def claim(self, expected=None):
        claim = self.store.claim('worker', now=NOW, lease_seconds=300)
        assert claim is not None and (expected is None or claim.job_id == expected)
        return claim

    def start(self, expected=None, *, status='in_progress'):
        claim = self.claim(expected)
        assert self.store.begin_submission(claim, now=NOW)
        if status:
            self.store.record_receipt(claim, provider_sid='SID' + claim.attempt_id[:8], status=status, now=NOW)
        return claim

    def set(self, job, **changes):
        with self.engine.begin() as connection:
            deliveries = {key: value for key, value in changes.items() if key in ('state', 'updated_at')}
            jobs = {key: value for key, value in changes.items() if key in ('error',)}
            if deliveries:
                connection.execute(sa.text('UPDATE outbound_deliveries SET ' + ', '.join(
                    f'{key} = :{key}' for key in deliveries) + ' WHERE id = :id'), {**deliveries, 'id': job})
            if jobs:
                connection.execute(sa.text('UPDATE fax_jobs SET error = :error WHERE id = :id'), {**jobs, 'id': job})

    def hold(self, job, kind, *, reason=None, release_at=None, requested_by=None, separate=False, dialing=False):
        with self.engine.begin() as connection:
            t = envelopes.tables(connection)
            identity = holds.create_on(connection, t, job_id=job, kind=kind, decision_id=None, now=NOW,
                                       release_at=release_at, requested_by=requested_by, separate=separate,
                                       reason=reason)
            if dialing:
                connection.execute(sa.text(
                    "INSERT INTO dialing_guard_holds (id, job_id, class_key, dialed_number, why, created_at) "
                    "VALUES (:id, :job, 'premium', :number, 'blocked', :at)"),
                    {'id': identity, 'job': job, 'number': NUMBER, 'at': NOW})
        return identity

    def look(self, actor=None):
        """The board ``actor`` sees, read afresh."""
        self.board.forget()
        return self.read(actor)

    def read(self, actor=None):
        scope = self.board.scope(self.access, actor or self.olive, now=NOW)
        found = self.board.aggregate(self.values, now=NOW)
        return outbound_now.view(found, scope, mask=mask, sentence_for=lambda fax: self.board.sentence(fax, found))


@pytest.fixture
def desk(database, tmp_path):
    return Desk(database, tmp_path)


@pytest.fixture
def trunk(database, tmp_path):
    return Desk(database, tmp_path, SIP_TRUNK_PRESET='telnyx', SIP_TRUNK_MAX_CALLS='2',
                SIP_TRUNK_CALLS_PER_SECOND='10')


def reasons(board):
    return {item['key']: item['count'] for item in board['waiting']['reasons']}


def items(board, section):
    return {item['id']: item for item in board[section]['items']}


# -- every section from real deliveries ------------------------------------------------------------------------------

def test_the_board_shows_sending_waiting_needs_you_and_today_from_the_real_claim(desk):
    sending = desk.accept(to=NUMBER)
    behind = desk.accept(to=NUMBER)                      # the same number: waits for the first call
    desk.start(sending)
    unsure = desk.accept(to=OTHER)
    after_unsure = desk.accept(to=OTHER)
    claim = desk.claim(unsure)
    assert desk.store.begin_submission(claim, now=NOW)
    assert desk.store.record_uncertain(claim, now=NOW)   # unknown result: holds OTHER for half an hour
    preparing = desk.accept(to=THIRD)
    desk.claim(preparing)                                # claimed, no call yet
    nothing = desk.accept(to='+12025550126')             # free to go
    test_fax = desk.accept(to='+12025550127')
    desk.set(test_fax, state='held')

    board = desk.look()
    assert board['not_checked'] == []
    assert board['sending']['count'] == 1
    item = board['sending']['items'][0]
    assert item == {'id': sending, 'to': '****0123', 'recipient': None, 'pages': 1, 'page': None,
                    'route': 'Phaxio', 'engine': None, 'started_at': item['started_at']}
    assert item['started_at'].endswith('Z')
    counts = reasons(board)
    assert counts['number_busy'] == 1 and counts['unknown_result'] == 1
    assert counts['preparing'] == 1 and counts['next'] == 1
    assert list(counts) == [entry['key'] for entry in board['waiting']['reasons']]
    assert len(board['waiting']['reasons']) == 10 and board['waiting']['more'] == 0
    # Largest first; equal counts keep the contract's order.
    assert [entry['count'] for entry in board['waiting']['reasons']] == sorted(counts.values(), reverse=True)
    waiting = items(board, 'waiting')
    assert set(waiting) == {behind, after_unsure, preparing, nothing}
    # Each fax shown says it in the claim's own words (Capacity.waiting_sentence).
    assert waiting[behind]['reason'] == 'number_busy'
    assert waiting[behind]['sentence'] == 'Waiting: another fax is calling this number.'
    assert waiting[after_unsure]['reason'] == 'unknown_result' and waiting[after_unsure]['until'].endswith('Z')
    assert waiting[after_unsure]['sentence'] == ('Waiting: an earlier fax to this number has an unknown result. '
                                                 'Faxbot waits until it is known or until 12:30 PM UTC.')
    assert waiting[preparing]['sentence'] == 'Being prepared.'
    assert waiting[nothing]['sentence'] == 'Next to go.'
    needs = items(board, 'needs_you')
    assert needs[unsure]['kind'] == 'uncertain' and needs[unsure]['actions'] == ['settle']
    assert needs[unsure]['sentence'] == ('Faxbot could not confirm delivery; check your provider account before '
                                         'sending this fax again.')
    assert needs[test_fax]['kind'] == 'other' and needs[test_fax]['actions'] == []
    assert needs[test_fax]['sentence'] == 'This test fax is held and will not be sent.'
    assert board['today']['accepted'] == 7 and board['today']['since'].endswith('Z')


def test_a_fax_that_failed_and_went_back_to_the_queue_waits_to_try_again(desk):
    job = desk.accept()
    claim = desk.start(job, status='failed')
    assert desk.store.requeue_after_failure(job, attempt_id=claim.attempt_id, category='provider_failed', now=NOW)
    board = desk.look()
    assert items(board, 'waiting')[job]['reason'] == 'retry'
    assert reasons(board)['retry'] == 1


def test_holds_need_you_with_the_actions_this_viewer_may_take(desk):
    from api.app.rules.text import WAITS
    route = desk.accept(desk.alice, to=NUMBER)
    approval = desk.accept(desk.alice, to=OTHER)
    window = desk.accept(desk.alice, to=THIRD)
    dialing = desk.accept(desk.alice, to='+19005550100')
    separate = desk.accept(desk.alice, to='+12025550128')
    desk.hold(route, 'no_route', reason='No account is allowed for this fax. ' + WAITS, requested_by='alice')
    desk.hold(approval, 'approval', requested_by='alice')
    desk.hold(window, 'window', release_at=NOW + timedelta(hours=2), requested_by='alice')
    desk.hold(dialing, 'approval', requested_by='alice', dialing=True)
    desk.hold(separate, 'approval', requested_by='olive', separate=True)

    approver = items(desk.look(desk.olive), 'needs_you')
    assert approver[route]['kind'] == 'no_route'
    assert approver[route]['actions'] == ['send_by_account', 'check_again', 'refuse']
    assert approver[route]['sentence'] == 'No account is allowed for this fax. ' + WAITS
    assert approver[route]['hold']['version'] == 1 and approver[route]['id'] == route
    assert approver[approval]['kind'] == 'approval' and approver[approval]['actions'] == ['approve', 'refuse']
    assert approver[approval]['sentence'] == WAITS
    assert approver[window]['kind'] == 'window' and approver[window]['actions'] == ['refuse']
    assert approver[window]['until'] == (NOW + timedelta(hours=2)).isoformat() + 'Z'
    assert approver[dialing]['kind'] == 'dialing' and approver[dialing]['actions'] == ['refuse']
    # A rule says someone other than the sender decides: olive sent it, so olive may not.
    assert approver[separate]['actions'] == []

    sender = desk.look(desk.alice)
    mine = items(sender, 'needs_you')
    assert mine[route]['actions'] == ['check_again']
    assert mine[approval]['actions'] == [] and mine[window]['actions'] == []
    assert sender['waiting']['count'] == 0


def test_today_groups_failures_by_how_they_ended(desk):
    from api.app import hylafax_engine, sinch_service, sip_calls
    from api.app.humblefax_service import _FAILURES
    endings = {
        'busy': sinch_service.CALL_ERRORS[17],
        'no_answer': sinch_service.CALL_ERRORS[16],
        'not_fax': sip_calls.PERSON,
        'no_speed': hylafax_engine._REASONS[2][1],
        'rejected': sinch_service.CALL_ERRORS[34],
        'other': 'Something else went wrong.',
    }
    for key, sentence in endings.items():
        job = desk.accept(to=f'+120255501{len(key):02d}')
        desk.set(job, state='failed', updated_at=NOW - timedelta(minutes=5), error=sentence)
    second_busy = desk.accept(to='+12025550199')
    desk.set(second_busy, state='failed', updated_at=NOW - timedelta(minutes=5),
             error=dict((words[0], sentence) for words, sentence in _FAILURES)['busy'])
    done = desk.accept(to='+12025550198')
    desk.set(done, state='success', updated_at=NOW - timedelta(minutes=1))
    yesterday = desk.accept(to='+12025550197', at=NOW - timedelta(days=1))
    desk.set(yesterday, state='failed', updated_at=NOW - timedelta(days=1), error=endings['busy'])
    # A person settled one as can't tell: failed for the API, but it may have arrived, so it is counted on its own,
    # never as failed today.
    from api.app.outbound_store import CANT_TELL
    unknown = desk.accept(to='+12025550196')
    desk.set(unknown, state='failed', updated_at=NOW - timedelta(minutes=2), error=CANT_TELL)

    today = desk.look()['today']
    assert today['accepted'] == 9 and today['delivered'] == 1 and today['failed'] == 7 and today['cant_tell'] == 1
    assert today['failures'][0] == {'key': 'busy', 'label': 'The line was busy', 'count': 2}
    assert {item['key']: item['count'] for item in today['failures']} == {
        'busy': 2, 'no_answer': 1, 'not_fax': 1, 'no_speed': 1, 'rejected': 1, 'other': 1}
    assert {item['key']: item['label'] for item in today['failures']}['not_fax'] == (
        'A person or voicemail answered, not a fax machine')


def test_failure_keys_extend_the_busy_hour_classifier():
    from api.app import sip_calls
    assert outbound_now.failure_key(disposition='busy') == 'busy'
    assert outbound_now.failure_key(disposition='no_answer') == 'no_answer'
    assert outbound_now.failure_key(disposition='answered', pages=0) == 'not_fax'
    assert outbound_now.failure_key(disposition='answered', pages=0,
                                    cause='Failed to train with any of the compatible modems') == 'no_speed'
    assert outbound_now.failure_key(category=sip_calls.PERSON_ANSWERED) == 'not_fax'
    assert outbound_now.failure_key(sentence=sip_calls.CLEARED) == 'rejected'
    assert outbound_now.failure_key(sentence=None) == 'other'
    # The trunk carrier's own answer to a call it never connected: a refusal or a number it could not reach is
    # turned down (even when the call record says busy), never busy; no answer stays no answer.
    refused = {'direction': 'outbound', 'disposition': 'failed', 'error_cause': 'SIP 603 Decline (cause 21)'}
    missing = {**refused, 'error_cause': 'SIP 404 Not Found (cause 1)'}
    unreached = {**refused, 'disposition': 'no_answer', 'error_cause': 'SIP 480 Temporarily Unavailable (cause 19)'}
    for record in (refused, missing, {**refused, 'error_cause': 'call failed (cause 21)'}):
        assert outbound_now.failure_key(sentence=sip_calls.carrier_sentence(record), disposition='busy') == 'rejected'
    assert outbound_now.failure_key(sentence=sip_calls.carrier_sentence(unreached), disposition='no_answer') == (
        'no_answer')


def test_a_submitter_with_five_faxes_today_gets_a_progress_line(desk):
    jobs = [desk.accept(desk.alice, to=f'+1202555020{index}') for index in range(5)]
    desk.set(jobs[0], state='success', updated_at=NOW)
    desk.set(jobs[1], state='failed', updated_at=NOW, error='Something else went wrong.')
    for index in range(4):
        desk.accept(desk.bob, to=f'+1202555030{index}')
    groups = desk.look()['groups']
    assert groups == [{'who': 'alice', 'total': 5, 'delivered': 1, 'sending': 0, 'waiting': 3, 'needs_you': 0,
                       'failed': 1}]


# -- room on the trunk, its lines, and the fax engine -----------------------------------------------------------------

def test_faxes_wait_for_a_free_line_when_the_trunk_is_at_its_limit(trunk):
    first, second = trunk.accept(to=NUMBER, trunk=True), trunk.accept(to=OTHER, trunk=True)
    trunk.start(first)
    trunk.start(second)
    waits = trunk.accept(to=THIRD, trunk=True)
    board = trunk.look()
    assert items(board, 'waiting')[waits]['reason'] == 'lines_busy'
    assert items(board, 'waiting')[waits]['sentence'] == 'Waiting for a free line: all 2 lines are in use.'
    entry = next(item for item in board['waiting']['reasons'] if item['key'] == 'lines_busy')
    assert entry['count'] == 1 and entry['sentence'] == 'Waiting for a free line: all 2 lines are in use.'
    assert board['waiting']['reasons'][0]['key'] == 'lines_busy'
    line = next(item for item in board['lines'] if item['key'] == 'sip')
    assert line == {'key': 'sip', 'label': 'Telnyx', 'in_use': 2, 'limit': 2, 'incoming': 0,
                    'sentence': '2 of 2 calls in use. Your carrier allows 2 at once.'}
    assert {item['route'] for item in board['sending']['items']} == {'Telnyx'}


def test_a_call_coming_in_counts_on_the_line_and_is_named(trunk):
    with trunk.engine.begin() as connection:
        connection.execute(sa.text(
            "INSERT INTO sip_call_records (id, direction, call_id, started_at, disposition, t38, fax_preference, "
            "created_at, updated_at) VALUES (:id, 'inbound', :call, :at, 'answered', 'no', 1, :at, :at)"),
            {'id': uuid4().hex, 'call': uuid4().hex, 'at': NOW - timedelta(minutes=1)})
    line = trunk.look()['lines'][0]
    assert line['in_use'] == 1 and line['incoming'] == 1
    assert line['sentence'] == '1 of 2 calls in use (1 incoming). Your carrier allows 2 at once.'


def test_the_engine_shows_only_while_faxbot_switched_new_calls_to_audio(trunk, tmp_path):
    from api.app import sip_fax_mode
    assert trunk.look()['engine'] is None
    values = ConfigurationValues.from_environment({
        'FAX_DATA_DIR': str(tmp_path / 'faxdata'), 'SIP_TRUNK_PRESET': 'telnyx', 'SIP_T38_ENABLED': 'false'})
    sip_fax_mode.write(values, 'audio', sip_fax_mode.NETWORK, NOW)
    engine = trunk.board._engine(values)
    assert engine['audio'] is True and engine['since'] == NOW.isoformat() + 'Z'
    assert engine['sentence'] == ('Your network changes port numbers, so fax over IP (T.38) cannot work; Faxbot '
                                  'sends audio fax until the network is fixed.')
    assert engine['fix'].startswith('Faxbot switches back by itself')
    # A T.38 call that got no fax data back: Faxbot switches back by itself only after its network check saw this
    # network blocked; otherwise "Try T.38 again" is the way back.
    sip_fax_mode.write(values, 'audio', sip_fax_mode.NO_DATA_BACK, NOW, network='open')
    engine = trunk.board._engine(values)
    assert engine['sentence'].startswith('A T.38 fax got no fax data back on this network')
    assert engine['fix'] == ('Once your network lets fax over IP (T.38) data come back, open Carrier trunk and choose '
                             '"Try T.38 again".')
    sip_fax_mode.write(values, 'audio', sip_fax_mode.NO_DATA_BACK, NOW, network='blocked')
    assert trunk.board._engine(values)['fix'].startswith('Faxbot switches back by itself once its network check')
    # A person's own choice of audio fax is theirs: nothing on the board.
    sip_fax_mode.write(values, 'audio', sip_fax_mode.CHOSEN, NOW)
    assert trunk.board._engine(values) is None


# -- the recipient's schedule and sending together (the real collaborators) ------------------------------------------

def test_a_fax_waits_for_the_recipients_hours_from_the_real_schedule(desk):
    from api.app.routing.schedule import for_engine
    # NOW is a Saturday: the recipient takes faxes Monday to Friday only.
    for_engine(desk.engine).save(NUMBER, time_zone='UTC', days={0, 1, 2, 3, 4}, start=9 * 60, end=17 * 60,
                                 learn_busy=True, now=NOW - timedelta(days=1))
    job = desk.accept(to=NUMBER)
    board = desk.look()
    item = items(board, 'waiting')[job]
    assert item['reason'] == 'recipient_hours' and item['until'] == '2026-10-05T09:00:00Z'
    assert item['sentence'] == ('Waiting until Monday at 9:00 AM UTC: this recipient takes faxes only Monday to '
                                'Friday, 9:00 AM to 5:00 PM UTC.')
    assert reasons(board)['recipient_hours'] == 1


def test_a_fax_held_to_go_with_others_is_sending_together(desk):
    from api.app.batching import store as batching
    job = desk.accept(to=NUMBER)
    with desk.configuration._locked() as connection:
        batching.hold_on(connection, batching.tables(desk.engine, connection), job,
                         batching.HoldPlan(NUMBER, 'principal:olive', 'olive', 1, False, 600), NOW)
    assert items(desk.look(), 'waiting')[job]['reason'] == 'sending_together'


@pytest.mark.parametrize('why,key', [('busy', 'recipient_busy'), ('cheaper', 'send_later'), ('faster', 'send_later')])
def test_each_schedule_wait_maps_to_its_reason(desk, monkeypatch, why, key):
    from api.app.capacity import Capacity
    job = desk.accept()
    until = NOW + timedelta(hours=1)
    monkeypatch.setattr(Capacity, 'decision', lambda self, *args, **kwargs: (Decision(until, why), None))
    item = items(desk.look(), 'waiting')[job]
    assert item['reason'] == key and item['until'] == until.isoformat() + 'Z'


# -- who sees what ----------------------------------------------------------------------------------------------------

def test_a_sender_sees_only_the_faxes_sent_lists_for_them_and_counts_follow(desk):
    alices = [desk.accept(desk.alice, to=f'+1202555040{index}') for index in range(2)]
    bobs = [desk.accept(desk.bob, to=f'+1202555050{index}') for index in range(3)]
    desk.start(alices[0])
    for actor, own in ((desk.alice, alices), (desk.bob, bobs)):
        board = desk.look(actor)
        seen = set(items(board, 'sending')) | set(items(board, 'waiting'))
        listed = {job['id'] for job in desk.queries.page(actor, limit=100)['jobs']}
        assert seen == set(own) == listed
        assert board['today']['accepted'] == len(own)
    everyone = desk.look(desk.olive)
    assert everyone['waiting']['count'] + everyone['sending']['count'] == 5
    # A grant on one fax shows that fax too.
    desk.world.assignment('bob', 'role_fax_viewer', _resource(desk, alices[1]))
    _bump_policy(desk)
    assert alices[1] in items(desk.look(desk.bob), 'waiting')


def _resource(desk, job):
    with desk.engine.connect() as connection:
        return connection.scalar(sa.text("SELECT id FROM access_resources WHERE fax_job_id = :id"), {'id': job})


def _bump_policy(desk):
    with desk.engine.begin() as connection:
        connection.execute(sa.text("UPDATE access_state SET policy_version = policy_version + 1 WHERE id = 'state'"))


def test_a_new_fax_under_a_granted_container_shows_without_reading_access_again(desk):
    desk.look(desk.alice)
    reads = desk.board.scope_reads
    job = desk.accept(desk.alice, to=OTHER)
    desk.board._last = None                    # a new aggregate, the same scope
    assert job in items(desk.read(desk.alice), 'waiting')
    assert desk.board.scope_reads == reads


# -- no write lock, bounded, partial ----------------------------------------------------------------------------------

def _count_immediate(engine):
    seen = []

    def count(connection, cursor, statement, parameters, context, executemany):
        # The access lock: SQLite's lock_on writes access_state (and a standalone transaction begins IMMEDIATE);
        # PostgreSQL's lock_on reads access_state FOR UPDATE.
        text = ' '.join(statement.upper().split())
        if (text.startswith('BEGIN IMMEDIATE') or 'UPDATE ACCESS_STATE' in text
                or ('FROM ACCESS_STATE' in text and 'FOR UPDATE' in text)):
            seen.append(text)
    sa.event.listen(engine, 'before_cursor_execute', count)
    return seen, lambda: sa.event.remove(engine, 'before_cursor_execute', count)


def test_a_poll_takes_no_write_lock_and_two_viewers_share_one_aggregate(desk):
    desk.accept()
    desk.read(desk.olive)                      # the first poll reads each scope once
    desk.read(desk.alice)
    assert desk.board.reads == 1
    seen, stop = _count_immediate(desk.engine)
    try:
        desk.clock += 1.0                      # within AGGREGATE_SECONDS
        desk.read(desk.olive)
        desk.read(desk.alice)
        assert desk.board.reads == 1
        desk.clock += outbound_now.AGGREGATE_SECONDS
        desk.read(desk.olive)
        assert desk.board.reads == 2
    finally:
        stop()
    assert seen == []
    # An access change reads that person's scope again, under the access lock, once.
    _bump_policy(desk)
    seen, stop = _count_immediate(desk.engine)
    try:
        desk.read(desk.olive)
        desk.read(desk.olive)
    finally:
        stop()
    assert len([statement for statement in seen if statement.startswith('BEGIN')]) == (
        1 if desk.engine.dialect.name == 'sqlite' else 0)
    # One access lock in each database's own form: SQLite's UPDATE, or PostgreSQL's SELECT ... FOR UPDATE.
    assert len([statement for statement in seen if not statement.startswith('BEGIN')]) == 1


def test_lists_hold_twenty_and_more_counts_faxes_beyond_the_reasoned_ones(desk, monkeypatch):
    assert (outbound_now.LIST_LIMIT, outbound_now.REASONED) == (20, 200)
    monkeypatch.setattr(outbound_now, 'REASONED', 22)
    for index in range(25):
        desk.accept(to=f'+120255506{index:02d}', at=NOW - timedelta(minutes=60 - index))
    board = desk.look()
    assert board['waiting']['count'] == 25 and len(board['waiting']['items']) == 20
    assert board['waiting']['more'] == 3 and reasons(board)['next'] == 22


def test_one_section_that_cannot_be_read_is_named_and_the_rest_still_answer(desk, monkeypatch):
    job = desk.accept()

    def broken(self, *args, **kwargs):
        raise RuntimeError('synthetic lines failure')
    monkeypatch.setattr(outbound_now.Board, '_lines', broken)
    board = desk.look()
    assert board['not_checked'] == ['lines'] and board['lines'] == []
    assert job in items(board, 'waiting')


def test_why_faxes_wait_is_read_on_one_connection(trunk):
    """The reasons pass reads through its own connection: no connection per fax (the predictor's prices included)."""
    for index in range(4):
        trunk.accept(to=f'+120255507{index:02d}', trunk=True)
    trunk.look()                               # reflection and first reads
    checkouts, values = [], trunk.values

    def checked_out(*_):
        checkouts.append(1)
    sa.event.listen(trunk.engine, 'checkout', checked_out)
    try:
        trunk.board._last = None
        found = trunk.board.aggregate(values, now=NOW)
    finally:
        sa.event.remove(trunk.engine, 'checkout', checked_out)
    assert {fax.reason for fax in found.faxes} == {'next'}
    assert len(checkouts) == 1


# -- over HTTP, with the real application and access policy -----------------------------------------------------------

BOOTSTRAP = 'synthetic-board-bootstrap'
ADMIN = {'X-API-Key': BOOTSTRAP}


@pytest.fixture
def client(isolated_installation, monkeypatch):
    from fastapi.testclient import TestClient
    from app import main
    for name, value in {'REQUIRE_API_KEY': 'true', 'API_KEY': BOOTSTRAP, 'PUBLIC_API_URL': 'https://testserver',
                        'FAX_BACKEND': 'phaxio', 'MAX_REQUESTS_PER_MINUTE': '0'}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv('FAXBOT_CONSOLE_ORIGINS', 'https://testserver')
    with TestClient(main.app, base_url='https://testserver', headers={'Origin': 'https://testserver'}) as client:
        yield client


def _send(client, headers, number):
    sent = client.post('/fax', headers=headers, data={'to': number},
                       files={'file': ('note.txt', b'Synthetic fax body', 'text/plain')})
    assert sent.status_code == 202, sent.text
    return sent.json()['id']


def test_the_board_over_http_follows_sent_for_each_key(client):
    created = client.post('/admin/api-keys', headers=ADMIN, json={'name': 'synthetic', 'scopes': ['fax:send',
                                                                                                    'fax:read']})
    assert created.status_code == 200, created.text
    sender = {'X-API-Key': created.json()['token']}
    theirs = _send(client, sender, NUMBER)
    ours = _send(client, ADMIN, OTHER)
    assert client.get('/outbound/now').status_code == 401
    everyone = client.get('/outbound/now', headers=ADMIN)
    assert everyone.status_code == 200, everyone.text
    board = everyone.json()
    assert list(board) == ['as_of', 'not_checked', 'sending', 'waiting', 'needs_you', 'today', 'lines', 'engine',
                           'groups', 'carrier_balance']
    assert board['not_checked'] == [] and board['as_of'].endswith('Z')
    # Sending is turned off here, so both faxes are held test faxes.
    assert set(items(board, 'needs_you')) == {theirs, ours}
    assert items(board, 'needs_you')[ours]['to'] == '********0124'
    # A key's fax:read is granted at the installation: it reads what Sent lists for it, every fax.
    for headers in (sender, ADMIN):
        seen = client.get('/outbound/now', headers=headers).json()
        listed = {job['id'] for job in client.get('/admin/fax-jobs', headers=headers).json()['jobs']}
        assert set(items(seen, 'needs_you')) == listed == {theirs, ours}
    # A key that may only send reads none: Sent lists nothing for it, and the board counts nothing.
    created = client.post('/admin/api-keys', headers=ADMIN, json={'name': 'send only', 'scopes': ['fax:send']})
    blind = {'X-API-Key': created.json()['token']}
    _send(client, blind, THIRD)
    assert client.get('/admin/fax-jobs', headers=blind).json()['jobs'] == []
    nothing = client.get('/outbound/now', headers=blind).json()
    assert nothing['needs_you']['count'] == 0 and nothing['today']['accepted'] == 0


def test_a_section_that_fails_over_http_still_answers_200(client, monkeypatch):
    from app import outbound_now as served

    def broken(self, *args, **kwargs):
        raise RuntimeError('synthetic engine failure')
    monkeypatch.setattr(served.Board, '_engine', broken)
    response = client.get('/outbound/now', headers=ADMIN)
    assert response.status_code == 200, response.text
    assert response.json()['not_checked'] == ['engine'] and response.json()['engine'] is None


def test_a_sender_created_after_a_viewer_looked_shows_at_the_next_aggregate(client):
    """A new sender's personal container is an access change: the administrator's kept scope is read again."""
    from app import outbound_now as served
    from app.routing.background import installation_engine
    assert client.get('/outbound/now', headers=ADMIN).status_code == 200
    board = served.for_engine(installation_engine(client.app)[0])
    reads = board.scope_reads
    created = client.post('/admin/api-keys', headers=ADMIN, json={'name': 'new sender', 'scopes': ['fax:send']})
    assert created.status_code == 200, created.text
    job = _send(client, {'X-API-Key': created.json()['token']}, NUMBER)
    board._last = None                        # the next aggregate, with the administrator's scope still kept
    seen = client.get('/outbound/now', headers=ADMIN).json()
    assert job in items(seen, 'needs_you')
    assert board.scope_reads == reads + 1


# -- bounded loads, filled sentences, Retry-After ---------------------------------------------------------------------

def sentences_filled(board):
    """Every sentence the console shows is product text, never null."""
    for item in board['waiting']['reasons'] + board['waiting']['items'] + board['needs_you']['items']:
        assert isinstance(item['sentence'], str) and item['sentence'], item


def _bulk(desk, owner, count, *, state, at, number=lambda index: '+12025550123'):
    """``count`` faxes of ``owner``'s in one transaction, as acceptance leaves them (fax, delivery, Sent resource)."""
    t = desk.world.tables
    deliveries = sa.Table('outbound_deliveries', sa.MetaData(), autoload_with=desk.engine)
    jobs, deliveries_rows, resources = [], [], []
    for index in range(count):
        job = uuid4().hex
        jobs.append({'id': job, 'to_number': number(index), 'file_name': 'synthetic.pdf', 'tiff_path': '',
                     'status': 'queued', 'backend': 'phaxio', 'pages': 1, 'created_at': at, 'updated_at': at})
        deliveries_rows.append({'id': job, 'dispatch_mode': 'normal', 'state': state, 'version': 1,
                                'created_at': at + timedelta(seconds=index), 'updated_at': at})
        resources.append({'id': uuid4().hex, 'kind': 'outbound', 'parent_id': 'personal-' + owner,
                          'parent_kind': 'personal', 'principal_id': None, 'mailbox_id': None, 'fax_job_id': job,
                          'inbound_fax_id': None, 'enabled': 1, 'version': 1, 'created_at': at, 'updated_at': at})
    with desk.engine.begin() as connection:
        connection.execute(t['fax_jobs'].insert(), jobs)
        connection.execute(deliveries.insert(), deliveries_rows)
        connection.execute(t['access_resources'].insert(), resources)


def test_a_big_day_loads_a_bounded_number_of_rows_and_counts_stay_right(desk):
    """5,000 delivered today and 2,000 waiting: counts come from SQL, and about the first 200 waiting are read."""
    at = NOW - timedelta(hours=2)
    _bulk(desk, 'alice', 3000, state='success', at=at)
    _bulk(desk, 'bob', 2000, state='success', at=at)
    _bulk(desk, 'alice', 1200, state='ready', at=at, number=lambda index: f'+1303555{index:04d}')
    _bulk(desk, 'bob', 800, state='ready', at=at + timedelta(hours=1), number=lambda index: f'+1404555{index:04d}')
    _bulk(desk, 'bob', 30, state='failed', at=at)
    everyone = desk.look(desk.olive)
    assert desk.board.rows_read <= 1000, desk.board.rows_read
    assert everyone['today'] == {'since': '2026-10-03T00:00:00Z', 'accepted': 7030, 'delivered': 5000, 'failed': 30,
                                 'cant_tell': 0, 'failures': [{'key': 'other', 'label': 'Another problem (see Sent)', 'count': 30}]}
    assert everyone['waiting']['count'] == 2000 and everyone['waiting']['more'] == 1800
    assert sum(reason['count'] for reason in everyone['waiting']['reasons']) == 200
    assert len(everyone['waiting']['items']) == 20
    sentences_filled(everyone)
    groups = {group['who']: group for group in everyone['groups']}
    assert groups['alice'] == {'who': 'alice', 'total': 4200, 'delivered': 3000, 'sending': 0, 'waiting': 1200,
                               'needs_you': 0, 'failed': 0}
    assert groups['bob']['total'] == 2830 and groups['bob']['failed'] == 30
    alice, bob = desk.read(desk.alice), desk.read(desk.bob)
    assert desk.board.reads == 1               # the same aggregate serves all three
    assert (alice['today']['accepted'], alice['today']['delivered'], alice['waiting']['count']) == (4200, 3000, 1200)
    assert (bob['today']['accepted'], bob['today']['delivered'], bob['waiting']['count']) == (2830, 2000, 800)
    assert bob['today']['failed'] == 30 and alice['today']['failed'] == 0
    # The first 200 in the queue are alice's (accepted first), so bob's list is shorter than his count.
    assert alice['waiting']['more'] == 1000 and bob['waiting']['items'] == [] and bob['waiting']['more'] == 800


def test_a_waiting_part_that_cannot_be_read_still_counts_and_lists_no_fax_without_a_sentence(desk, monkeypatch):
    desk.accept()
    desk.accept(to=OTHER)

    def broken(self, *args, **kwargs):
        raise RuntimeError('synthetic waiting failure')
    monkeypatch.setattr(outbound_now.Board, '_waiting', broken)
    board = desk.look()
    assert board['not_checked'] == ['waiting'] and board['waiting']['count'] == 2
    assert board['waiting']['items'] == [] and board['waiting']['more'] == 2
    sentences_filled(board)


def test_a_busy_database_answers_503_with_retry_after(client, monkeypatch):
    import sqlite3
    from app import outbound_now as served
    from app.access.types import AccessUnavailableError

    def locked(self, *args, **kwargs):
        raise sa.exc.OperationalError('SELECT 1', {}, sqlite3.OperationalError('database is locked'))
    monkeypatch.setattr(served.Board, '_counts', locked)
    busy = client.get('/outbound/now', headers=ADMIN)
    assert busy.status_code == 503 and busy.headers['Retry-After'] == '2'
    assert busy.json()['detail'] == "Faxbot is busy right now, so outgoing faxes can't be shown. Try again in a moment."
    # Another cause still answers 200 with what it could read.
    monkeypatch.setattr(served.Board, '_counts', lambda self, *args, **kwargs: (_ for _ in ()).throw(KeyError('x')))
    partial = client.get('/outbound/now', headers=ADMIN)
    assert partial.status_code == 200 and 'sending' in partial.json()['not_checked']

    def unavailable(self, *args, **kwargs):
        raise AccessUnavailableError()
    monkeypatch.setattr(served.Board, 'scope', unavailable)
    access_busy = client.get('/outbound/now', headers=ADMIN)
    assert access_busy.status_code == 503 and access_busy.headers['Retry-After'] == '1'


# -- the console renders what the API returns ---------------------------------------------------------------------------

CONSOLE_FIXTURE = __import__('pathlib').Path(__file__).parents[1] / 'admin_ui/src/__tests__/outboundNowFromApi.json'


def _keys(value):
    """The field names of a board, recursively; a list's items are merged, so one sample of each item shape is enough."""
    if isinstance(value, dict):
        return {key: _keys(item) for key, item in value.items()}
    if isinstance(value, list):
        merged = {}
        for item in value:
            if isinstance(item, dict):
                for key, keys in _keys(item).items():
                    merged[key] = keys if merged.get(key) in (None, 'value') else merged[key]
        return [merged] if merged else []
    return 'value'


def test_the_console_fixture_has_every_field_the_api_returns(database, tmp_path):
    """outboundNowFromApi.json is a reply from this API; the console's tests render it, so the two can't drift apart.

    Set FAXBOT_WRITE_BOARD_FIXTURE=1 to write it again after a change to the reply.
    """
    import json
    import os
    from api.app import sip_fax_mode
    board_desk = Desk(database, tmp_path, SIP_TRUNK_PRESET='telnyx', SIP_TRUNK_MAX_CALLS='2',
                      SIP_TRUNK_CALLS_PER_SECOND='10', SIP_T38_ENABLED='false')
    first, second = board_desk.accept(to=NUMBER, trunk=True, pages=3), board_desk.accept(to=OTHER, trunk=True)
    board_desk.start(first)
    board_desk.start(second)
    for index in range(4):
        board_desk.accept(to=f'+1202555040{index}', trunk=True, pages=2)
    held = board_desk.accept(board_desk.alice, to=THIRD)
    board_desk.hold(held, 'no_route', reason='No account is allowed for this fax.', requested_by='alice')
    failed = board_desk.accept(to='+12025550150')
    board_desk.set(failed, state='failed', updated_at=NOW - timedelta(minutes=5), error='Something else went wrong.')
    delivered = board_desk.accept(to='+12025550151')
    board_desk.set(delivered, state='success', updated_at=NOW - timedelta(minutes=2))
    sip_fax_mode.write(board_desk.values, 'audio', sip_fax_mode.NETWORK, NOW)

    board = board_desk.look()
    for section in ('sending', 'waiting', 'needs_you'):
        assert board[section]['items'], section
    assert board['waiting']['reasons'] and board['today']['failures'] and board['lines'] and board['groups']
    assert board['engine'] is not None and board['not_checked'] == []
    if os.environ.get('FAXBOT_WRITE_BOARD_FIXTURE') == '1':
        CONSOLE_FIXTURE.write_text(json.dumps(board, indent=2, sort_keys=True) + '\n')
    assert _keys(json.loads(CONSOLE_FIXTURE.read_text())) == _keys(board)


# -- uncertain faxes follow Sent's settling list (certainty_items) -----------------------------------------------------

def _certainty(desk, job, *, state='open', outcome=None, category='person_answered', at=None):
    """A certainty item for the fax's attempt, as the certainty worker files it and a person settles it."""
    at = at or NOW - timedelta(minutes=10)
    with desk.engine.begin() as connection:
        attempt = connection.scalar(sa.text('SELECT attempt_id FROM outbound_deliveries WHERE id = :id'), {'id': job})
        if attempt is None or connection.scalar(sa.text('SELECT count(*) FROM certainty_items WHERE attempt_id = :id'),
                                                {'id': attempt}):
            # One item per attempt: a later item on the same fax is another attempt's.
            attempt = uuid4().hex
            sequence = connection.scalar(sa.text('SELECT count(*) FROM outbound_attempts WHERE job_id = :job'),
                                         {'job': job}) + 1
            connection.execute(sa.text(
                "INSERT INTO outbound_attempts (id, job_id, sequence, phase, created_at, submitted_at, completed_at) "
                "VALUES (:id, :job, :sequence, 'failed', :at, :at, :at)"),
                {'id': attempt, 'job': job, 'sequence': sequence, 'at': at})
            connection.execute(sa.text('UPDATE outbound_deliveries SET attempt_id = :attempt WHERE id = :id'),
                               {'attempt': attempt, 'id': job})
        connection.execute(sa.text(
            'INSERT INTO certainty_items (id, job_id, attempt_id, category, reference, state, outcome, settled_at, '
            'version, created_at, updated_at) VALUES (:id, :job, :attempt, :category, :reference, :state, :outcome, '
            ':settled, 1, :at, :at)'),
            {'id': uuid4().hex, 'job': job, 'attempt': attempt, 'category': category,
             'reference': uuid4().hex[:8].upper(), 'state': state, 'outcome': outcome,
             'settled': at if state == 'settled' else None, 'at': at})


def test_needs_you_uncertain_follows_open_certainty_items_whatever_the_delivery_state(desk):
    from api.app.work.certainty_service import category_text
    # An open item on a fax that failed two days ago, before today's counts: shown with the item's own reason.
    old = desk.accept(desk.alice, to=NUMBER, at=NOW - timedelta(days=2))
    desk.set(old, state='failed', updated_at=NOW - timedelta(days=2), error='A person answered.')
    _certainty(desk, old, at=NOW - timedelta(days=2))
    # A person settled "can't tell": nothing is left to settle, and settling ended the delivery as can't tell
    # (outbound_store.settle_uncertain_on), never failed today.
    from api.app.outbound_store import CANT_TELL
    settled = desk.accept(desk.alice, to=OTHER)
    claim = desk.claim(settled)
    assert desk.store.begin_submission(claim, now=NOW) and desk.store.record_uncertain(claim, now=NOW)
    _certainty(desk, settled, state='settled', outcome='unknown', category='transport_ambiguous')
    desk.set(settled, state='failed', updated_at=NOW, error=CANT_TELL)
    # Uncertain a moment ago, before the certainty worker filed its item: shown with the delivery's own sentence.
    fresh = desk.accept(desk.bob, to=THIRD)
    claim = desk.claim(fresh)
    assert desk.store.begin_submission(claim, now=NOW) and desk.store.record_uncertain(claim, now=NOW)

    board = desk.look(desk.olive)
    needs = items(board, 'needs_you')
    assert set(needs) == {old, fresh} and board['needs_you']['count'] == 2
    assert needs[old] == {**needs[old], 'kind': 'uncertain', 'actions': ['settle'],
                          'sentence': category_text('person_answered'),
                          'since': (NOW - timedelta(days=2)).isoformat() + 'Z'}
    assert needs[fresh]['kind'] == 'uncertain' and needs[fresh]['sentence'] == (
        'Faxbot could not confirm delivery; check your provider account before sending this fax again.')
    sentences_filled(board)
    # Today's counts stay as they were: the old failure is not today's, nothing new failed today, and the fax settled
    # as can't tell is counted on its own.
    assert board['today']['failed'] == 0 and board['today']['accepted'] == 2 and board['today']['cant_tell'] == 1
    # Each sender sees only their own.
    assert set(items(desk.look(desk.alice), 'needs_you')) == {old}
    assert desk.look(desk.alice)['needs_you']['count'] == 1
    assert set(items(desk.look(desk.bob), 'needs_you')) == {fresh}


def test_an_open_item_on_a_fax_that_failed_today_still_counts_as_failed_today(desk):
    job = desk.accept(to=NUMBER)
    desk.set(job, state='failed', updated_at=NOW - timedelta(minutes=5), error='Something else went wrong.')
    _certainty(desk, job, category='partly_sent')
    board = desk.look()
    assert items(board, 'needs_you')[job]['sentence'] == (
        'The call failed part way through, so some pages may have arrived.')
    assert board['today']['failed'] == 1 and board['needs_you']['count'] == 1


def test_only_the_faxs_latest_certainty_item_counts(desk):
    reopened, closed = desk.accept(to=NUMBER), desk.accept(to=OTHER)
    for job in (reopened, closed):
        desk.set(job, state='failed', updated_at=NOW - timedelta(minutes=30))
    _certainty(desk, reopened, state='settled', outcome='unknown', at=NOW - timedelta(minutes=20))
    _certainty(desk, reopened, category='result_lost', at=NOW - timedelta(minutes=5))
    _certainty(desk, closed, category='result_lost', at=NOW - timedelta(minutes=20))
    _certainty(desk, closed, state='settled', outcome='unknown', at=NOW - timedelta(minutes=5))
    board = desk.look()
    assert set(items(board, 'needs_you')) == {reopened} and board['needs_you']['count'] == 1


# -- signed in through the console (a browser session) ---------------------------------------------------------------

@pytest.fixture
def browser_client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from app import main
    from api.tests.test_access_management_http import ORIGIN, _environment
    _environment(monkeypatch, tmp_path)
    with TestClient(main.app, base_url=ORIGIN, headers={'Origin': ORIGIN}) as test_client:
        yield test_client


def test_a_signed_in_console_session_reads_the_board_with_its_own_scope(browser_client):
    """The console signs in with a password and polls with its session cookie: the board answers 200, each person
    sees what Sent lists for them, and a session that has ended gets 401."""
    from api.tests.test_access_management_http import B, assign, ready_user
    client = browser_client
    admin, _ = ready_user(client, 'dana', role='role_administrator')
    sender, principal = ready_user(client, 'sam')
    personal = next(item['id'] for item in client.get('/access/resources', headers=B).json()['items']
                    if item['kind'] == 'personal' and item['principal_id'] == principal['id'])
    assign(client, principal['id'], 'role_fax_operator', personal)
    sender.refresh()
    theirs = sender.post('/fax', data={'to': NUMBER}, json=None,
                         files={'file': ('note.txt', b'Synthetic fax body', 'text/plain')})
    assert theirs.status_code == 202, theirs.text
    ours = client.post('/fax', headers=B, data={'to': OTHER},
                       files={'file': ('note.txt', b'Synthetic fax body', 'text/plain')})
    assert ours.status_code == 202, ours.text
    theirs, ours = theirs.json()['id'], ours.json()['id']

    everyone = admin.get('/outbound/now')
    assert everyone.status_code == 200, everyone.text
    assert set(items(everyone.json(), 'needs_you')) == {theirs, ours}
    own = sender.get('/outbound/now')
    assert own.status_code == 200, own.text
    listed = {job['id'] for job in sender.get('/admin/fax-jobs').json()['jobs']}
    assert set(items(own.json(), 'needs_you')) == listed == {theirs}
    # Polling again within the scope's life reads it from the cache; it still answers.
    assert sender.get('/outbound/now').status_code == 200

    assert sender.post('/auth/logout').status_code == 200
    ended = sender.get('/outbound/now')
    assert ended.status_code == 401
