"""Room for fax calls: one call at a time to a number, the trunk's lines, new calls a second (SQLite and PostgreSQL)."""
from contextlib import contextmanager
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
import sqlalchemy as sa

from api.app.capacity import HOLD, Capacity
from api.app.config_profiles import ProviderConfiguration
from api.app.config_store import ConfigurationStore
from api.app.config_values import ConfigurationValues
from api.app.outbound_store import OutboundStore
from api.app.schema import upgrade_schema
from api.tests.test_schema import database  # noqa: F401 (fixture)
from api.tests.test_batching import T0 as HELD_AT, accept as accept_held, sip  # noqa: F401 (fixture)


NUMBER, OTHER = '+12025550123', '+12025550124'
T0 = datetime(2026, 10, 6, 15, 0, 0)


class Install:
    def __init__(self, database, tmp_path, **environment):
        upgrade_schema(database)
        self.configuration = ConfigurationStore(database, tmp_path / 'installation.key')
        values = ConfigurationValues.from_environment({'FAX_BACKEND': 'phaxio', 'FAX_DISABLED': 'false',
                                                       **environment})
        phaxio = ProviderConfiguration('phaxio', credentials={'api_key': 'synthetic-key'})
        self.snapshot = self.configuration.initialize(values, actor='test', providers={'outbound': phaxio})
        self.store = OutboundStore(self.configuration)
        self.engine = database
        self.clock = T0

    @property
    def values(self):
        return self.configuration.read().active.values

    def accept(self, to=NUMBER, *, urgent=False, trunk=False, at=None):
        job, when = uuid4().hex, at or self.tick()
        self.configuration.accept_outbound(self.snapshot.active, {
            'id': job, 'to_number': to, 'file_name': 'synthetic.pdf', 'tiff_path': '', 'status': 'queued',
            'pages': 1, 'created_at': when, 'updated_at': when, **({'urgent': 1} if urgent else {})})
        with self.engine.begin() as connection:
            connection.execute(sa.text('UPDATE outbound_deliveries SET created_at = :at WHERE id = :id'),
                               {'at': when, 'id': job})
            if trunk:
                # Bound to the SIP trunk, as a fax accepted while the trunk sends.
                connection.execute(sa.text("UPDATE fax_jobs SET backend = 'sip' WHERE id = :id"), {'id': job})
        return job

    def tick(self, seconds=1):
        self.clock += timedelta(seconds=seconds)
        return self.clock

    def claim(self, at=None):
        return self.store.claim('worker', now=at or self.clock, lease_seconds=300)

    def start(self, at=None):
        """Claim the next fax and start its call; None when nothing may start."""
        claim = self.claim(at)
        if claim is not None:
            assert self.store.begin_submission(claim, now=at or self.clock)
        return claim

    def finish(self, claim, status='success'):
        self.store.record_receipt(claim, provider_sid='SID' + claim.attempt_id[:8], status=status,
                                  now=self.clock)

    def waiting(self, job, at=None):
        return Capacity(self.engine).waiting_sentence(job, self.values, at or self.clock)


@pytest.fixture
def install(database, tmp_path):
    return Install(database, tmp_path)


@pytest.fixture
def trunk(database, tmp_path):
    return Install(database, tmp_path, SIP_TRUNK_PRESET='telnyx', SIP_TRUNK_MAX_CALLS='2',
                   SIP_TRUNK_CALLS_PER_SECOND='10')


# -- one call at a time to a number ----------------------------------------------------

def test_a_second_fax_to_the_same_number_waits_and_dials_after_the_first_finishes(install):
    first, second, elsewhere = install.accept(), install.accept(), install.accept(OTHER)
    calling = install.start()
    assert calling.job_id == first
    install.store.observe(first, attempt_id=calling.attempt_id, profile_id=calling.profile_id, provider_sid='SID1',
                          status='in_progress', event_key='ringing', now=install.clock)
    # The other number has room; this number does not, and the waiting fax says why.
    assert install.start().job_id == elsewhere
    assert install.claim() is None
    assert install.waiting(second) == 'Waiting: another fax is calling this number.'
    install.store.observe(first, attempt_id=calling.attempt_id, profile_id=calling.profile_id, provider_sid='SID1',
                          status='success', event_key='done', now=install.tick())
    assert install.waiting(second) is None
    assert install.start().job_id == second


def _connections_used(configuration, action):
    """How many more pooled connections ``action`` checks out while it holds the write lock, how many
    times it took the lock, and what it returned.

    A plain read before the lock (the idle check, ``OutboundStore._any``) is not counted: it never
    waits behind a writer and never holds anything a writer waits for.
    """
    state = {'held': 0, 'locks': 0, 'extra': 0}
    original = configuration._locked

    @contextmanager
    def locked():
        with original() as connection:
            state['held'] += 1
            state['locks'] += 1
            try:
                yield connection
            finally:
                state['held'] -= 1

    def checked_out(*_):
        if state['held']:
            state['extra'] += 1

    configuration._locked = locked
    sa.event.listen(configuration.engine, 'checkout', checked_out)
    try:
        result = action()
    finally:
        sa.event.remove(configuration.engine, 'checkout', checked_out)
        del configuration._locked
    return state['extra'], state['locks'], result


# Reflecting on a second connection while the claim held the SQLite write lock left
# that connection blocking the next writer ("database is locked"). Every claim path
# reads room through the one locked connection: a cloud fax, a fax over the trunk
# (its lines, calls coming in, new calls a second, own numbers) and a group sent together.

def test_the_first_claim_reads_room_through_its_own_locked_connection(install):
    install.accept()
    extra, locks, claim = _connections_used(install.configuration, install.claim)
    assert claim is not None and locks >= 1 and extra == 0


def test_the_first_claim_of_a_trunk_fax_reads_room_through_its_own_locked_connection(database, tmp_path):
    install = Install(database, tmp_path, SIP_TRUNK_PRESET='telnyx', SIP_TRUNK_MAX_CALLS='2',
                      SIP_TRUNK_CALLS_PER_SECOND='10', INBOUND_ENABLED='true', SIP_TRUNK_DIDS='+13035550100')
    from api.app.routing.own_numbers import receiving_numbers
    assert receiving_numbers(install.values) == {'+13035550100'}  # so the own-number check runs too
    install.accept(trunk=True)
    extra, locks, claim = _connections_used(install.configuration, install.claim)
    assert claim is not None and locks >= 1 and extra == 0


def test_the_first_claim_of_a_group_sent_together_reads_room_through_its_own_locked_connection(sip, database):
    _, delivery, *_ = sip
    first, second = accept_held(sip, at=HELD_AT), accept_held(sip, at=HELD_AT + timedelta(minutes=1))
    extra, locks, claim = _connections_used(delivery.configuration,
                                            lambda: delivery.claim('worker', now=HELD_AT + timedelta(minutes=10)))
    assert [member.job_id for member in claim.members] == [first, second] and locks >= 1 and extra == 0


def test_a_number_can_take_more_calls_at_once_or_no_limit(install):
    from api.app.routing.store import RouteStore
    jobs = [install.accept() for _ in range(3)]
    RouteStore(install.engine).update_destination(NUMBER, max_calls=2)
    assert [install.start().job_id for _ in range(2)] == jobs[:2]
    assert install.claim() is None
    RouteStore(install.engine).update_destination(NUMBER, max_calls=0)
    assert install.start().job_id == jobs[2]


def test_an_uncertain_call_keeps_the_number_until_it_is_settled_or_the_safety_time_passes(install):
    first, second = install.accept(), install.accept()
    calling = install.start()
    install.store.record_uncertain(calling, now=install.clock)
    assert install.claim() is None
    assert install.waiting(first).startswith('Its result is unknown, so Faxbot keeps this number free of other '
                                             'faxes until the result is known or until ')
    assert install.waiting(second).startswith('Waiting: an earlier fax to this number has an unknown result. '
                                              'Faxbot waits until it is known or until ')
    # Past the safety time the number is free again, and nothing failed on the way.
    later = install.clock + HOLD + timedelta(seconds=1)
    assert install.waiting(second, later) is None
    assert install.claim(later).job_id == second
    assert install.store.get(first)['state'] == 'reconciliation_required'


def test_settling_an_uncertain_call_frees_the_number_at_once(install):
    first, second = install.accept(), install.accept()
    calling = install.start()
    install.store.record_uncertain(calling, now=install.clock)
    assert install.claim() is None
    install.store.observe(first, attempt_id=calling.attempt_id, profile_id=calling.profile_id, provider_sid='SID1',
                          status='success', event_key='confirmed', now=install.tick())
    assert install.start().job_id == second


def test_a_restart_in_the_middle_of_a_call_neither_loses_nor_doubles_the_hold(install):
    first, second = install.accept(), install.accept()
    install.start()  # left "submitting": the process stops before the result
    restarted = OutboundStore(install.configuration)
    assert restarted.claim('worker-2', now=install.tick(60)) is None
    # The lost worker's call becomes uncertain and keeps holding the number until the safety time.
    later = install.clock + timedelta(seconds=400)
    assert restarted.recover_expired(now=later) == 1
    assert restarted.get(first)['state'] == 'reconciliation_required'
    assert restarted.claim('worker-2', now=later) is None
    assert restarted.claim('worker-2', now=install.clock + HOLD + timedelta(seconds=2)).job_id == second


def test_a_call_whose_result_never_arrives_stops_blocking_after_the_safety_time(install):
    first, second = install.accept(), install.accept()
    calling = install.start()
    install.store.observe(first, attempt_id=calling.attempt_id, profile_id=calling.profile_id, provider_sid='SID1',
                          status='in_progress', event_key='ringing', now=install.clock)
    assert install.claim(install.clock + HOLD - timedelta(seconds=1)) is None
    assert install.claim(install.clock + HOLD + timedelta(seconds=1)).job_id == second


# -- the trunk ---------------------------------------------------------------------------

def test_the_trunk_takes_its_calls_at_once_and_cloud_faxes_still_go(trunk):
    over = [trunk.accept(to, trunk=True) for to in ('+12025550101', '+12025550102', '+12025550103')]
    cloud = trunk.accept('+12025550104')
    started = [trunk.start(trunk.tick()) for _ in range(2)]
    assert [claim.job_id for claim in started] == over[:2]
    # Both lines are in use: the third trunk fax waits, a fax through a cloud provider does not.
    assert trunk.waiting(over[2]) == 'Waiting for a free line: all 2 lines are in use.'
    assert trunk.start(trunk.tick()).job_id == cloud
    assert trunk.claim(trunk.tick()) is None
    trunk.tick()
    trunk.finish(started[0])
    assert trunk.start(trunk.tick()).job_id == over[2]


def test_a_call_coming_in_uses_a_trunk_line_too(trunk):
    from api.app.sip_calls import SipCallRecords
    calls = SipCallRecords(trunk.engine).table
    with trunk.engine.begin() as connection:
        for index in range(2):
            connection.execute(calls.insert().values(
                id=uuid4().hex, direction='inbound', call_id=f'in-{index}', started_at=trunk.clock,
                disposition='answered', t38='yes', fax_preference=0, created_at=trunk.clock,
                updated_at=trunk.clock))
    waiting = trunk.accept(trunk=True)
    assert trunk.claim(trunk.tick()) is None
    assert trunk.waiting(waiting) == 'Waiting for a free line: all 2 lines are in use.'


def test_new_calls_a_second_follow_the_setting_and_the_carriers_published_limit(database, tmp_path):
    from api.app.capacity import calls_per_second, trunk_calls_at_once
    slow = Install(database, tmp_path, SIP_TRUNK_PRESET='telnyx', SIP_TRUNK_MAX_CALLS='5',
                   SIP_TRUNK_CALLS_PER_SECOND='1')
    first, second = slow.accept('+12025550101', trunk=True), slow.accept('+12025550102', trunk=True)
    moment = slow.tick()
    assert slow.start(moment).job_id == first
    assert slow.claim(moment) is None
    assert slow.waiting(second, moment) == 'Waiting a moment: Faxbot starts at most 1 new call each second on your phone line.'
    assert slow.start(moment + timedelta(seconds=1)).job_id == second
    # Defaults: Telnyx's published limits (5 new calls a second without a surcharge), the fax lines otherwise.
    telnyx = ConfigurationValues.from_environment({'FAX_BACKEND': 'phaxio', 'SIP_TRUNK_PRESET': 'telnyx'})
    assert (calls_per_second(telnyx), trunk_calls_at_once(telnyx)) == (5, 2)
    other = ConfigurationValues.from_environment({'FAX_BACKEND': 'phaxio', 'SIP_TRUNK_PRESET': 'flowroute',
                                                  'SIP_FAX_LINES': '4'})
    assert (calls_per_second(other), trunk_calls_at_once(other)) == (None, 4)


# -- urgency and fairness ----------------------------------------------------------------

def test_an_urgent_fax_goes_before_others_waiting_for_the_same_room(install):
    calls = [install.accept(f'+1202555010{index}') for index in range(3)]
    urgent = install.accept('+12025550109', urgent=True)
    assert install.start().job_id == urgent
    assert install.start().job_id == calls[0]


def test_one_senders_batch_cannot_hold_up_another_sender(database, tmp_path):
    """Sender A queues 5 faxes to one number, then sender B queues 1: B's goes second."""
    from api.tests.test_access_policy import NOW, World
    from api.app.access.policy import AccessControl
    from api.app.access.fax_resources import FaxResources
    from api.app.access.outbound import AuthorizedOutbound
    world = World(database)
    alice = world.user('alice')
    world.user('bob')
    world.assignment('alice', 'role_fax_operator', 'personal-alice')
    configuration = ConfigurationStore(database, tmp_path / 'key')
    snapshot = configuration.initialize(ConfigurationValues.from_environment({'FAX_DISABLED': 'false'}), actor='test',
                                        providers={'outbound': ProviderConfiguration('phaxio', credentials={
                                            'api_key': 'synthetic'})})
    outbound = AuthorizedOutbound(configuration, FaxResources(AccessControl(configuration.access_store)),
                                  clock=lambda: NOW)
    jobs = []
    for index in range(6):
        job = uuid4().hex
        outbound.accept(alice, snapshot.active, {'id': job, 'to_number': NUMBER, 'file_name': 'x.pdf',
                                                 'tiff_path': '', 'status': 'queued',
                                                 'created_at': T0 + timedelta(seconds=index),
                                                 'updated_at': T0 + timedelta(seconds=index)})
        with database.begin() as connection:
            connection.execute(sa.text('UPDATE outbound_deliveries SET created_at = :at WHERE id = :id'),
                               {'at': T0 + timedelta(seconds=index), 'id': job})
        jobs.append(job)
    resources = world.tables['access_resources']
    with database.connect() as connection:
        bobs = connection.scalar(sa.select(resources.c.id).where(resources.c.fax_job_id == jobs[5]))
    world.update('access_resources', bobs, parent_id='personal-bob')
    store, now = OutboundStore(configuration), T0 + timedelta(minutes=1)
    order = []
    for _ in range(3):
        claim = store.claim('worker', now=now, lease_seconds=300)
        assert store.begin_submission(claim, now=now)
        order.append(claim.job_id)
        store.record_receipt(claim, provider_sid='SID' + claim.attempt_id[:8], status='success', now=now)
        now += timedelta(minutes=1)
    assert order == [jobs[0], jobs[5], jobs[1]]


# -- fewer wasted calls -------------------------------------------------------------------

def _simulate(install, limit, number):
    """Faxes to a one-line machine: a call while the line is busy fails and is sent again.

    Returns how many calls were placed to deliver every fax.
    """
    from api.app.routing.store import RouteStore
    RouteStore(install.engine).update_destination(number, max_calls=limit)
    pending, calls, line = [install.accept(number) for _ in range(4)], 0, []
    for _ in range(60):
        install.tick(30)
        # The call on the line ends after one round.
        for claim in list(line):
            install.finish(claim)
            line.remove(claim)
        again = 0
        while True:
            claim = install.start()
            if claim is None:
                break
            calls += 1
            if line:
                install.finish(claim, 'failed')  # busy: another call holds the line
                again += 1
            else:
                install.store.observe(claim.job_id, attempt_id=claim.attempt_id, profile_id=claim.profile_id,
                                      provider_sid='SID' + claim.attempt_id[:8], status='in_progress',
                                      event_key='ringing', now=install.clock)
                line.append(claim)
        # A busy fax has to be sent again in the next round.
        pending += [install.accept(number) for _ in range(again)]
        if not line and install.claim() is None:
            break
    return calls


def test_waiting_for_the_line_costs_fewer_calls_than_dialing_into_a_busy_line(install):
    without = _simulate(install, 0, NUMBER)
    with_room = _simulate(install, None, OTHER)
    assert with_room == 4 and without > with_room


@pytest.mark.asyncio
async def test_a_fax_routed_over_a_full_trunk_waits_with_nothing_sent_and_nothing_failed(database, tmp_path):
    """The claim gate covers faxes bound to the trunk; the route chooser stops one routed onto it."""
    from api.app.outbound_worker import OutboundWorker
    from api.app.routing.store import RouteStore
    from api.app.routing.transport import RoutedTransport
    from api.tests.test_routing_multiprovider import Inner, card
    full = Install(database, tmp_path, SIP_TRUNK_PRESET='telnyx', SIP_TRUNK_MAX_CALLS='1',
                   FAX_OUTBOUND_ROUTES='sip')
    RouteStore(database).replace_cards([card('phaxio', page='0.07'), card('sip', minute='0.005')])
    holding = full.accept('+12025550101', trunk=True)
    assert full.start().job_id == holding
    routed = full.accept('+12025550102')
    inner = Inner(full.store, [])
    worker = OutboundWorker(full.store, RoutedTransport(inner, direct=None), clock=lambda: full.clock)
    assert await worker.step() is False
    row = full.store.get(routed)
    assert (row['state'], inner.used) == ('ready', [])
    assert routed in worker.paused and 'capacity_wait' in [event['kind'] for event in full.store.history(routed)]
    # While it is paused the worker leaves it alone; nothing was sent and nothing failed.
    assert await worker.step() is False
    with database.connect() as connection:
        phases = connection.execute(sa.text('SELECT phase FROM outbound_attempts WHERE job_id = :id'),
                                    {'id': routed}).scalars().all()
    assert phases == ['abandoned']


def test_urgent_is_bound_into_a_request_only_when_set():
    import hashlib
    import json
    from api.app.request_identity import intent_fingerprint
    plain = hashlib.sha256(json.dumps({'version': 2, 'to': NUMBER, 'queue_only': False, 'document_sha256': 'a' * 64},
                                      sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    assert intent_fingerprint(version=2, to=NUMBER, queue_only=False, document_sha256='a' * 64) == plain
    assert intent_fingerprint(version=2, to=NUMBER, queue_only=False, document_sha256='a' * 64, urgent=True) != plain


# -- the API and `faxbot` ------------------------------------------------------------------

@pytest.fixture
def capacity_cli(monkeypatch, tmp_path):
    from api.tests.test_cli import Cli, _serve
    for client in _serve(monkeypatch, tmp_path, SIP_TRUNK_PRESET='telnyx'):
        yield Cli(client)


def test_cli_and_api_show_and_change_capacity_and_urgency(capacity_cli):
    from api.tests.test_cli import BOOTSTRAP
    cli, admin = capacity_cli, {'X-API-Key': BOOTSTRAP}
    # The trunk: its limits in effect, and Telnyx's published limits with their sources.
    limits = cli.json('providers', 'trunk', 'limits')
    assert (limits['max_calls_in_effect'], limits['calls_per_second_in_effect']) == (2, 5)
    assert limits['carrier_limits']['read_on'] == '2026-10-06' and len(limits['carrier_limits']['sources']) == 2
    shown = ' '.join(cli('providers', 'trunk', 'limits').stdout.split())
    assert 'Calls at once 2 (the same as the fax lines)' in shown and "5 (your carrier's limit)" in shown
    changed = cli.json('providers', 'trunk', 'limits', '--calls-at-once', '3', '--calls-per-second', '1')
    assert (changed['max_calls_in_effect'], changed['calls_per_second_in_effect']) == (3, 1)
    # A recipient: calls at once to this number.
    assert cli.json('recipients', 'set', NUMBER, '--calls-at-once', '2')['max_calls'] == 2
    assert 'Calls at once to this number 2 at once' in ' '.join(cli('recipients', 'show', NUMBER).stdout.split())
    assert cli.json('recipients', 'set', NUMBER, '--calls-at-once', 'default')['max_calls'] is None
    assert cli('recipients', 'set', NUMBER, '--calls-at-once', 'many').exit_code != 0
    # An urgent fax: stored, shown, and bound into the request.
    def post(urgent, key):
        return cli.client.post('/fax', headers={**admin, 'Idempotency-Key': key},
                               data={'to': NUMBER, **({'urgent': 'true'} if urgent else {})},
                               files={'file': ('note.txt', b'Synthetic page\n', 'text/plain')})
    sent = post(True, 'capacity-key-1')
    assert sent.status_code == 202, sent.text
    job = sent.json()['id']
    detail = cli.client.get(f'/admin/fax-jobs/{job}', headers=admin).json()
    assert detail['urgent'] is True and 'waiting_reason' in detail
    assert post(False, 'capacity-key-1').status_code == 409
    assert 'Urgent Yes: it goes before other faxes waiting for the same line.' in ' '.join(
        cli('sent', 'show', job).stdout.split())
    health = cli.client.get('/admin/health-status', headers=admin).json()
    assert health['jobs']['waiting_for_line'] == 0
    assert 'waiting for a free line' in cli('providers', 'status').stdout
