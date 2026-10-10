"""A fax held while on battery goes out by itself when the power allows (brief 99, item 4; power.py).

The whole cycle runs through the real outbound worker and route transport, with the fake NUT server of
``test_power`` standing in for the UPS: held, released by the routing background step with no person, then sent
exactly once. Synthetic numbers (555) only; no call is placed and no provider is contacted.
"""
from datetime import datetime, timedelta

import pytest
import sqlalchemy as sa

from api.app import nut, power
from api.app.outbound_worker import OutboundWorker
from api.app.power import PowerState, release_reason
from api.app.routing.holds import HoldStore
from api.app.routing.transport import RoutedTransport
from api.tests.test_power import FakeUpsd
from api.tests.test_rules_delivery import ANNE, Inner, accept, publish, rule
from api.tests.test_schema import database  # noqa: F401 (fixture)
from api.tests.test_trunk_capacity import _routed, trunked  # noqa: F401 (fixture and helper)


@pytest.fixture
def ups():
    """A UPS on battery with 65 seconds left: room to hand a fax to a service (60 s), not for a 20-page trunk
    call from this server. Tests change ``ups.variables`` to bring the power back."""
    power._cache.clear()
    with FakeUpsd({'office': {'ups.status': 'OB DISCHRG', 'battery.runtime': '65'}}) as server:
        yield server
    power._cache.clear()


class Clock:
    """The worker's clock: after a hold the worker leaves that fax alone for a few seconds (``RouteHeld``)."""

    def __init__(self):
        self.now = datetime.utcnow()

    def __call__(self):
        return self.now

    def later(self, seconds=6):
        self.now += timedelta(seconds=seconds)


def _on_battery(env, server, reserve=0):
    power.save(env.engine, '127.0.0.1', port=server.port, reserve_seconds=reserve)


def _set(server, **variables):
    server.variables['office'].update(variables)
    power._cache.clear()  # the next read sees the UPS as it is now, not the cached reading


def _held(env, job):
    return HoldStore(env.delivery).holds(state='open', job_id=job)


def _events(env, job):
    with env.engine.connect() as connection:
        return [dict(row) for row in connection.execute(sa.text(
            'SELECT event, why, needed_seconds, reserve_seconds, runtime_seconds FROM power_hold_events '
            'WHERE job_id = :job ORDER BY created_at, id'), {'job': job}).mappings()]


async def _held_on_battery(env, server, monkeypatch, *, reserve=0):
    """A 20-page fax whose rule sends it by the trunk only, held by the worker while on battery."""
    _on_battery(env, server, reserve)
    publish(env, {'format': 1, 'routes': [rule('r-trunk', {'use': 'sip'})]})
    job = accept(env, pages=20)
    transport = _routed(env, monkeypatch)
    clock = Clock()
    worker = OutboundWorker(env.delivery, transport, clock=clock)
    worker.later = clock.later
    assert await worker.step() is False
    assert transport.inner.used == [] and env.delivery.get(job)['state'] == 'ready'
    hold = _held(env, job)
    assert len(hold) == 1 and hold[0]['kind'] == 'no_route' and 'on battery' in hold[0]['reason']
    return job, worker, transport.inner


@pytest.mark.asyncio
async def test_when_mains_power_returns_the_held_fax_goes_by_itself_exactly_once(trunked, monkeypatch, ups):  # noqa: F811
    job, worker, inner = await _held_on_battery(trunked, ups, monkeypatch)
    held = _events(trunked, job)
    assert [(row['event'], row['why'], row['runtime_seconds'], row['reserve_seconds']) for row in held] == [
        ('held', 'battery', 65, 0)]
    assert held[0]['needed_seconds'] > 65
    assert power.history(trunked.engine, job, zone_name='UTC').endswith(
        'while on battery; it goes by itself when the power is back or the battery can cover the call.')
    # Still on battery: nothing is released, and the worker never claims the held fax.
    assert power.release_step(trunked.engine, trunked.delivery) == 0
    worker.later()
    assert await worker.step() is False and inner.used == []
    # Mains power is back: the background step releases it, with no person, and the worker sends it once.
    _set(ups, **{'ups.status': 'OL CHRG', 'battery.runtime': '1800'})
    assert power.release_step(trunked.engine, trunked.delivery) == 1
    assert _held(trunked, job) == []
    released = HoldStore(trunked.delivery).holds(state='released', job_id=job)
    assert released[0]['reason'] == 'Released by itself when the power came back.'
    assert power.history(trunked.engine, job, zone_name='UTC').endswith('when the power came back, and waiting to go.')
    worker.later()
    assert await worker.step() is True and inner.used == ['sip']
    assert [row['event'] for row in _events(trunked, job)] == ['held', 'released']
    sentence = power.history(trunked.engine, job, zone_name='UTC')
    assert sentence.startswith('Held at ') and ' UTC while on battery; sent at ' in sentence
    assert sentence.endswith(' UTC when the power came back.')
    # Released once, sent once: nothing is released again and nothing is sent again.
    assert power.release_step(trunked.engine, trunked.delivery) == 0
    assert await worker.step() is False and inner.used == ['sip']


@pytest.mark.asyncio
async def test_when_the_battery_can_cover_the_call_with_room_to_spare_it_goes_on_battery(trunked, monkeypatch,  # noqa: F811
                                                                                         ups):
    job, worker, inner = await _held_on_battery(trunked, ups, monkeypatch, reserve=60)
    needed = _events(trunked, job)[0]['needed_seconds']
    # The runtime covers the call and its reserve, but not the margin a wobbling estimate needs: it keeps waiting.
    _set(ups, **{'battery.runtime': str(needed + 60 + power.RELEASE_MARGIN_SECONDS - 1)})
    assert power.release_step(trunked.engine, trunked.delivery) == 0 and len(_held(trunked, job)) == 1
    _set(ups, **{'battery.runtime': str(needed + 60 + power.RELEASE_MARGIN_SECONDS)})
    assert power.release_step(trunked.engine, trunked.delivery) == 1
    worker.later()
    assert await worker.step() is True and inner.used == ['sip']
    assert power.history(trunked.engine, job, zone_name='UTC').endswith('when the battery could cover the call.')
    assert [(row['event'], row['why']) for row in _events(trunked, job)] == [('held', 'battery'),
                                                                             ('released', 'runtime')]


@pytest.mark.asyncio
async def test_an_unreadable_ups_keeps_the_fax_held_and_turning_the_check_off_releases_it(trunked, monkeypatch,  # noqa: F811
                                                                                         ups):
    job, worker, inner = await _held_on_battery(trunked, ups, monkeypatch)
    _set(ups, **{'ups.status': 'ERR DATA-STALE'})
    assert power.release_step(trunked.engine, trunked.delivery) == 0 and len(_held(trunked, job)) == 1
    power.save(trunked.engine, '')  # you turned the UPS check off
    assert power.release_step(trunked.engine, trunked.delivery) == 1
    worker.later()
    assert await worker.step() is True and inner.used == ['sip']
    assert power.history(trunked.engine, job, zone_name='UTC').endswith('after the UPS check was turned off.')


@pytest.mark.asyncio
async def test_a_fax_someone_decided_on_or_that_is_not_waiting_is_never_released(trunked, monkeypatch, ups):  # noqa: F811
    job, worker, inner = await _held_on_battery(trunked, ups, monkeypatch)
    hold = _held(trunked, job)[0]
    # Not waiting to go any more (here: failed by hand): the power step leaves it alone even with mains back.
    with trunked.engine.begin() as connection:
        connection.execute(sa.text("UPDATE outbound_deliveries SET state = 'failed' WHERE id = :job"), {'job': job})
    _set(ups, **{'ups.status': 'OL'})
    assert power.release_step(trunked.engine, trunked.delivery) == 0
    assert _held(trunked, job)[0]['id'] == hold['id'] and [row['event'] for row in _events(trunked, job)] == ['held']
    # A person's Check again is that person's release; the power step never releases it a second time.
    with trunked.engine.begin() as connection:
        connection.execute(sa.text("UPDATE outbound_deliveries SET state = 'ready' WHERE id = :job"), {'job': job})
    HoldStore(trunked.delivery).check_again(hold['id'], version=hold['version'], actor=ANNE)
    assert power.held_for_power(trunked.engine) == []
    assert power.release_step(trunked.engine, trunked.delivery) == 0
    worker.later()
    assert await worker.step() is True and inner.used == ['sip']


def test_the_ups_is_not_read_while_no_fax_is_held_for_power(database):  # noqa: F811
    from api.app.schema import upgrade_schema
    upgrade_schema(database)
    power.save(database, '192.0.2.9')
    reads = []
    assert power.release_step(database, None, reader=lambda *args, **kwargs: reads.append(args)) == 0
    assert reads == []


def _reading(status, runtime):
    return PowerState(True, nut.Reading('office', frozenset(status.split()), runtime, 50.0), reserve_seconds=120)


@pytest.mark.parametrize('found, item, why', [
    (PowerState(False), {'needed': 600, 'reserve': 0}, 'ups_off'),
    (PowerState(True, error='Faxbot could not connect to the UPS server.'), {'needed': 600, 'reserve': 0}, None),
    (_reading('OL', 30), {'needed': 600, 'reserve': 0}, 'mains'),
    (_reading('OB', 659), {'needed': 600, 'reserve': 0}, None),
    (_reading('OB', 660), {'needed': 600, 'reserve': 0}, 'runtime'),
    (_reading('OB', None), {'needed': 600, 'reserve': 0}, None),
    (_reading('OB', 3600), {'needed': None, 'reserve': 0}, None),
    # Without a recorded reserve, the reserve you set now counts.
    (_reading('OB', 779), {'needed': 600, 'reserve': None}, None),
    (_reading('OB', 780), {'needed': 600, 'reserve': None}, 'runtime'),
])
def test_only_evidence_releases_a_fax_held_for_power(found, item, why):
    assert release_reason(found, item) == why


def test_the_sentence_names_the_day_when_the_fax_went_on_another_day():
    from api.app.power import _local_time
    held = datetime(2026, 10, 9, 23, 50)
    assert _local_time(held, 'UTC') == '11:50 PM UTC'
    assert _local_time(held + timedelta(minutes=20), 'UTC', held) == '10 Oct 12:10 AM UTC'
    assert _local_time(held + timedelta(minutes=5), 'UTC', held) == '11:55 PM UTC'


@pytest.fixture
def power_cli(monkeypatch, tmp_path):
    from api.tests.test_cli import Cli, _serve
    for client in _serve(monkeypatch, tmp_path, SIP_TRUNK_PRESET='telnyx'):
        yield Cli(client)


def test_sent_details_and_the_command_line_show_the_hold(power_cli):
    from api.app.routing.background import installation_engine
    from api.tests.test_cli import BOOTSTRAP
    cli, admin = power_cli, {'X-API-Key': BOOTSTRAP}
    sent = cli.client.post('/fax', headers={**admin, 'Idempotency-Key': 'power-key-1'},
                           data={'to': '+12025550123'}, files={'file': ('note.txt', b'Synthetic page\n', 'text/plain')})
    assert sent.status_code == 202, sent.text
    job = sent.json()['id']
    assert cli.client.get(f'/admin/fax-jobs/{job}', headers=admin).json()['power_hold'] is None
    engine, _ = installation_engine(cli.client.app)
    with engine.begin() as connection:
        connection.execute(sa.text(
            "INSERT INTO power_hold_events (id, job_id, event, why, needed_seconds, reserve_seconds, runtime_seconds, "
            "created_at) VALUES ('e1', :job, 'held', 'battery', 400, 120, 300, :at)"),
            {'job': job, 'at': datetime(2026, 10, 10, 15, 41)})
    detail = cli.client.get(f'/admin/fax-jobs/{job}', headers=admin).json()
    assert detail['power_hold'] == ('Held at 3:41 PM UTC while on battery; it goes by itself when the power is back '
                                    'or the battery can cover the call.')
    assert 'On battery Held at 3:41 PM UTC while on battery;' in ' '.join(cli('sent', 'show', job).stdout.split())
