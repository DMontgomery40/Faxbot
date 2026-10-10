"""A busy database never costs a fax its result, and a reader never waits behind a writer.

SQLite lets one writer at a time hold its write lock. Before write-ahead logging, a writer that was
committing also shut out every new reader until the lock timeout ran out (the console's three 503s on
2026-10-10). These tests hold a real lock from a second connection; nothing here is a stand-in for the
database.
"""
import asyncio
from datetime import timedelta
import sqlite3
import threading
import time

import pytest
import sqlalchemy as sa

from api.app.schema import create_database_engine
from api.tests.test_schema import database  # noqa: F401 - fixture


def _file_engine(tmp_path, **kwargs):
    path = tmp_path / 'faxbot.db'
    engine = create_database_engine('sqlite:///' + str(path), **kwargs)
    with engine.begin() as connection:
        connection.exec_driver_sql('CREATE TABLE probe (x INTEGER)')
        connection.exec_driver_sql('INSERT INTO probe VALUES (1)')
    return path, engine


def test_a_reader_answers_at_once_while_another_connection_holds_the_write_lock(tmp_path):
    path, engine = _file_engine(tmp_path, lock_timeout=2)
    holder = sqlite3.connect(path, timeout=0, isolation_level=None)
    try:
        # The strongest lock a writer takes: before write-ahead logging it shut out every reader.
        holder.execute('BEGIN EXCLUSIVE')
        holder.execute('INSERT INTO probe VALUES (2)')
        started = time.monotonic()
        with engine.connect() as reader:
            rows = reader.exec_driver_sql('SELECT x FROM probe').scalars().all()
        assert rows == [1]  # the uncommitted row stays invisible
        assert time.monotonic() - started < 1
    finally:
        holder.execute('ROLLBACK')
        holder.close()
        engine.dispose()


def test_a_long_reader_and_a_committing_writer_never_block_a_new_reader(tmp_path):
    """The incident itself: one long read open, a writer committing behind it, and a new console read."""
    path, engine = _file_engine(tmp_path, lock_timeout=2)
    long_reader = sqlite3.connect(path, timeout=0, isolation_level=None)
    committed = threading.Event()

    def write():
        writer = create_database_engine('sqlite:///' + str(path), lock_timeout=2)
        try:
            with writer.begin() as connection:
                connection.exec_driver_sql('INSERT INTO probe VALUES (2)')
            committed.set()
        except sa.exc.OperationalError:
            pass
        finally:
            writer.dispose()

    try:
        long_reader.execute('BEGIN')
        assert long_reader.execute('SELECT count(*) FROM probe').fetchone() == (1,)
        thread = threading.Thread(target=write)
        thread.start()
        # The writer commits without waiting for the long reader.
        assert committed.wait(1.5)
        started = time.monotonic()
        with engine.connect() as reader:
            assert reader.exec_driver_sql('SELECT count(*) FROM probe').scalar_one() == 2
        assert time.monotonic() - started < 1
        # The long reader keeps the view it started with.
        assert long_reader.execute('SELECT count(*) FROM probe').fetchone() == (1,)
        thread.join(5)
    finally:
        long_reader.execute('ROLLBACK')
        long_reader.close()
        engine.dispose()


def test_postgresql_and_sqlite_readers_never_wait_for_the_delivery_write_lock(database):  # noqa: F811
    """PostgreSQL readers never waited; both backends now answer at once while a writer holds the lock."""
    from api.app.routing.database import DELIVERY_LOCK_ID
    with database.begin() as connection:
        connection.exec_driver_sql('CREATE TABLE probe (x INTEGER)')
        connection.exec_driver_sql('INSERT INTO probe VALUES (1)')
    with database.connect() as holder:
        if database.dialect.name == 'sqlite':
            holder.exec_driver_sql('BEGIN EXCLUSIVE')
        else:
            holder.execute(sa.text('SELECT pg_advisory_xact_lock(:key)'), {'key': DELIVERY_LOCK_ID})
        holder.exec_driver_sql('INSERT INTO probe VALUES (2)')
        started = time.monotonic()
        with database.connect() as reader:
            assert reader.exec_driver_sql('SELECT x FROM probe').scalars().all() == [1]
        assert time.monotonic() - started < 1
        holder.rollback()


def test_a_memory_database_keeps_its_own_journal():
    engine = create_database_engine('sqlite://')
    try:
        with engine.connect() as connection:
            assert connection.exec_driver_sql('PRAGMA journal_mode').scalar_one() == 'memory'
    finally:
        engine.dispose()


# -- Slow writes: the only trace of a writer holding the lock once WAL removes the -journal file ------------------

class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _slow_lines(caplog):
    return [record for record in caplog.records if 'held the write lock' in record.getMessage()]


def test_a_write_that_holds_the_lock_too_long_is_logged_with_where_it_ran(database, caplog, monkeypatch):  # noqa: F811
    from api.app import schema
    clock = _Clock()
    monkeypatch.setattr(schema, '_clock', clock)
    with database.begin() as connection:
        connection.exec_driver_sql('CREATE TABLE probe (x INTEGER)')
    caplog.set_level('WARNING')

    def quick_write():
        with database.begin() as connection:
            connection.exec_driver_sql('INSERT INTO probe VALUES (1)')
            clock.now += schema.SLOW_WRITE_SECONDS - 0.1

    def held_write():
        with database.begin() as connection:
            connection.exec_driver_sql('INSERT INTO probe VALUES (2)')
            clock.now += schema.SLOW_WRITE_SECONDS + 1.5

    quick_write()
    assert _slow_lines(caplog) == []
    held_write()
    [line] = _slow_lines(caplog)
    assert line.levelname == 'WARNING'
    message = line.getMessage()
    assert 'held the write lock for 3.5 seconds' in message and 'committed' in message
    assert 'test_database_busy.py:' in message and 'in held_write' in message
    # Reads never start the clock, however long they take.
    caplog.clear()
    with database.connect() as connection:
        connection.exec_driver_sql('SELECT x FROM probe').all()
        clock.now += 60
    assert _slow_lines(caplog) == []


def test_a_slow_write_through_the_delivery_lock_names_the_store_method(database, caplog, monkeypatch):  # noqa: F811
    """The outbound store's own helper (``ConfigurationStore._locked``) is skipped: the store method is named."""
    from api.app import schema
    from api.app.routing.database import write_transaction
    clock = _Clock()
    monkeypatch.setattr(schema, '_clock', clock)
    with database.begin() as connection:
        connection.exec_driver_sql('CREATE TABLE probe (x INTEGER)')
    caplog.set_level('WARNING')

    def record_something():
        with write_transaction(database) as connection:
            clock.now += 5
            connection.exec_driver_sql('INSERT INTO probe VALUES (1)')
            clock.now += 5

    record_something()
    [line] = _slow_lines(caplog)
    # On SQLite the clock starts once BEGIN IMMEDIATE holds the lock; on PostgreSQL once the advisory lock is held.
    assert 'for 10.0 seconds' in line.getMessage() and 'in record_something' in line.getMessage()


# -- What a lock timeout does to a fax: the result writes that record a call's outcome ----------------------------

@pytest.fixture
def busy(tmp_path):
    """A real SQLite installation whose writers give up after 0.2 s (10 s in production), and its file."""
    from api.app.config_profiles import ProviderConfiguration
    from api.app.config_store import ConfigurationStore
    from api.app.config_values import ConfigurationValues
    from api.app.outbound_store import OutboundStore
    from api.app.schema import upgrade_schema
    path = tmp_path / 'faxbot.db'
    engine = create_database_engine('sqlite:///' + str(path), lock_timeout=0.2)
    upgrade_schema(engine)
    configuration = ConfigurationStore(engine, tmp_path / 'installation.key')
    values = ConfigurationValues.from_environment({'FAX_BACKEND': 'phaxio', 'FAX_DISABLED': 'false'})
    snapshot = configuration.initialize(values, actor='test', providers={
        'outbound': ProviderConfiguration('phaxio', credentials={'api_key': 'synthetic-key'})})
    holders = []

    class Busy:
        installation = (configuration, OutboundStore(configuration), snapshot)

        @staticmethod
        def holder():
            holders.append(Holder(path))
            return holders[-1]
    yield Busy
    for each in holders:
        each.close()
    engine.dispose()


class Holder:
    """Another connection holding the database's write lock, as a long writer would."""

    def __init__(self, path):
        self.connection = sqlite3.connect(path, timeout=0, isolation_level=None, check_same_thread=False)
        self.held = False

    def hold(self):
        self.connection.execute('BEGIN IMMEDIATE')
        self.held = True

    def release(self):
        if self.held:
            self.connection.execute('ROLLBACK')
            self.held = False

    def close(self):
        self.release()
        self.connection.close()


def _sip_on_the_line(installation):
    from api.tests.test_partly_sent import SIP, on_the_line
    return on_the_line(installation, SIP)


def _result(job, attempt, status='SUCCESS', pages='3'):
    return {'Event': 'UserEvent', 'UserEvent': 'FaxResult', 'JobID': job, 'AttemptID': attempt, 'Status': status,
            'Pages': pages, 'Station64': ''}


def _waits(monkeypatch, then=None):
    """Record the retry waits instead of sleeping; ``then`` runs at each wait (to release a held lock)."""
    from api.app import database_busy
    waits = []

    def wait(seconds):
        waits.append(seconds)
        if then is not None:
            then()
    monkeypatch.setattr(database_busy, '_sleep', wait)
    return waits


def _submitted(store, attempt_id):
    from datetime import datetime
    with store.configuration.engine.connect() as connection:
        value = connection.execute(sa.text('SELECT submitted_at FROM outbound_attempts WHERE id = :id'),
                                   {'id': attempt_id}).scalar_one()
    return datetime.fromisoformat(value) if isinstance(value, str) else value


def test_a_built_in_engine_result_outlasts_a_busy_database_and_is_kept(busy, monkeypatch, caplog):
    """Before: the lock timeout was swallowed by an audit line, and the fax stayed in progress for ever."""
    from api.app import main
    installation, job, claim = _sip_on_the_line(busy.installation)
    _, store, _ = installation
    monkeypatch.setattr(main, '_deliveries', lambda: store)
    lock = busy.holder()
    lock.hold()
    waits = _waits(monkeypatch, then=lock.release)
    caplog.set_level('WARNING')
    main._handle_fax_result(_result(job, claim.attempt_id))
    assert store.get(job)['state'] == 'success'
    assert waits == [1.0]
    assert 'database is locked' in caplog.text and 'try 2 of 3' in caplog.text


def test_a_shared_call_result_outlasts_a_busy_database_for_every_fax(busy, monkeypatch):
    """The shared-call path writes one outcome per fax (batching/results.py); each write waits out the lock."""
    from api.app.batching import results
    from api.app.batching.outcomes import Outcome
    installation, job, claim = _sip_on_the_line(busy.installation)
    installation, other, other_claim = _sip_on_the_line(installation)
    _, store, _ = installation
    lock = busy.holder()
    lock.hold()
    _waits(monkeypatch, then=lock.release)
    results._apply(store, [Outcome(job, claim.attempt_id, 'success', None, None),
                           Outcome(other, other_claim.attempt_id, 'success', None, None)], 'ami-result:SUCCESS')
    assert store.get(job)['state'] == 'success' and store.get(other)['state'] == 'success'


def test_a_lost_result_frees_the_line_then_waits_for_a_person_and_is_never_sent_again(busy, monkeypatch, caplog):
    """The database stays busy through every try: the fax must not stay sending for ever (the safety net)."""
    from api.app import capacity, main
    from api.app.engine_results import settle_lost_results
    from api.app.work.certainty_service import CATEGORY_TEXT
    from api.tests.test_partly_sent import attempt_of, item_category
    installation, job, claim = _sip_on_the_line(busy.installation)
    _, store, _ = installation
    monkeypatch.setattr(main, '_deliveries', lambda: store)
    lock = busy.holder()
    lock.hold()
    waits = _waits(monkeypatch)
    caplog.set_level('WARNING')
    main._handle_fax_result(_result(job, claim.attempt_id))
    assert waits == [1.0, 4.0]
    assert 'stayed busy through 3 tries' in caplog.text
    assert 'was not saved because the database stayed busy' in caplog.text
    lock.release()
    assert store.get(job)['state'] == 'in_progress'
    submitted = _submitted(store, claim.attempt_id)
    holding = capacity.for_engine(store.configuration.engine)
    with store.configuration.engine.connect() as connection:
        def holds(at):
            return {row.delivery_id for row in connection.execute(holding.holds(at))}
        assert job in holds(submitted + capacity.HOLD - timedelta(minutes=1))
        assert job not in holds(submitted + capacity.HOLD + timedelta(seconds=1))  # its line is free again
    assert settle_lost_results(store, now=submitted + capacity.HOLD - timedelta(minutes=1)) == 0
    assert settle_lost_results(store, now=submitted + capacity.HOLD + timedelta(seconds=1)) == 1
    assert store.get(job)['state'] == 'reconciliation_required'
    assert attempt_of(store, claim.attempt_id) == {'phase': 'uncertain', 'error_category': 'result_lost'}
    assert item_category(store, job) == 'result_lost'
    assert CATEGORY_TEXT['result_lost'] == ("Faxbot lost this call's result; check whether it arrived before "
                                            'sending again.')
    # Settling again changes nothing, and nothing is claimed for another try.
    assert settle_lost_results(store, now=submitted + capacity.HOLD + timedelta(hours=1)) == 0
    assert store.claim('another-worker') is None
    # A result that still arrives replaces the uncertainty with the real outcome.
    main._handle_fax_result(_result(job, claim.attempt_id))
    assert store.get(job)['state'] == 'success'


def test_the_safety_net_leaves_ssl_fax_engine_sends_and_cloud_faxes_to_their_own_checks(busy):
    from api.app import capacity
    from api.app.engine_results import settle_lost_results
    from api.app.hylafax_records import records_for
    from api.tests.test_partly_sent import on_the_line
    from api.tests.test_partly_sent import DOCUMO
    installation, engine_job, engine_claim = _sip_on_the_line(busy.installation)
    _, store, _ = installation
    # The SSL Fax engine took this send (hylafax_http._sweep waits three hours for it: the engine queues retries).
    records_for(store.configuration.engine).record_call(direction='outbound', call_key=engine_claim.attempt_id,
                                                        engine='hylafax', job_id=engine_job)
    installation, cloud_job, cloud_claim = on_the_line(installation, DOCUMO, 'documo-synthetic-1')
    later = _submitted(store, cloud_claim.attempt_id) + capacity.HOLD + timedelta(minutes=1)
    assert settle_lost_results(store, now=later) == 0
    assert store.get(engine_job)['state'] == 'in_progress' and store.get(cloud_job)['state'] == 'in_progress'


# -- The delivery worker: the hand-over receipt and the claim's heartbeat ---------------------------------------

@pytest.mark.asyncio
async def test_a_provider_receipt_outlasts_a_busy_database_and_the_fax_never_becomes_uncertain(busy, monkeypatch):
    """Before: the refused receipt left the fax submitting, and lease recovery made a delivered fax uncertain."""
    from api.app.outbound_worker import OutboundWorker, SubmissionReceipt
    from api.tests.test_outbound_store import accept
    from api.tests.test_outbound_worker import Transport
    installation = busy.installation
    _, store, _ = installation
    job = accept(installation)
    lock = busy.holder()

    async def handed_over():
        lock.hold()  # another writer takes the lock just as the provider answers
        return SubmissionReceipt('remote-one', 'in_progress')
    waits = _waits(monkeypatch, then=lock.release)
    transport = Transport(handed_over)
    assert await OutboundWorker(store, transport).step() is True
    assert waits == [1.0] and transport.submissions == 1
    assert store.get(job)['state'] == 'in_progress'
    from api.tests.test_partly_sent import attempt_of
    with store.configuration.engine.connect() as connection:
        assert connection.execute(sa.text('SELECT provider_sid FROM outbound_attempts WHERE job_id = :job'),
                                  {'job': job}).scalar_one() == 'remote-one'
    assert attempt_of(store, store.get(job)['attempt_id'])['phase'] == 'in_progress'


@pytest.mark.parametrize('engine', ['built-in', 'ssl-fax'])
@pytest.mark.asyncio
async def test_a_receipt_commit_without_an_answer_is_never_tried_again_and_the_call_result_settles_it(
        busy, monkeypatch, engine):
    """A commit whose answer was lost may have been kept: the worker never repeats it. The fax becomes uncertain
    when its lease ends, and the engine's own result for the call still settles it."""
    from api.app import main
    from api.app.config_store import ConfigurationCommitUncertain
    from api.app.outbound_worker import OutboundWorker, SubmissionReceipt
    from api.tests.test_outbound_polling import with_profile
    from api.tests.test_outbound_store import accept, another_number
    from api.tests.test_outbound_worker import Transport
    from api.tests.test_partly_sent import SIP, attempt_of
    installation = with_profile(busy.installation, SIP)
    _, store, _ = installation
    job = accept(installation, to_number=another_number())
    calls = []

    def unanswered(*args, **kwargs):
        calls.append(kwargs)
        try:
            raise sqlite3.OperationalError('database is locked')
        except sqlite3.OperationalError:
            raise ConfigurationCommitUncertain('Configuration commit was not acknowledged; reload before retrying.')
    monkeypatch.setattr(store, 'record_receipt', unanswered)
    waits = _waits(monkeypatch)

    async def handed_over():
        return SubmissionReceipt(job, 'in_progress')
    with pytest.raises(ConfigurationCommitUncertain):
        await OutboundWorker(store, Transport(handed_over)).step()
    assert len(calls) == 1 and waits == []
    row = store.get(job)
    assert row['state'] == 'submitting'
    from datetime import datetime
    assert store.recover_expired(now=datetime.utcnow() + timedelta(minutes=5)) == 1
    assert store.get(job)['state'] == 'reconciliation_required'
    assert attempt_of(store, row['attempt_id']) == {'phase': 'uncertain', 'error_category': 'worker_lost'}
    if engine == 'built-in':
        monkeypatch.setattr(main, '_deliveries', lambda: store)
        main._handle_fax_result(_result(job, row['attempt_id']))
    else:
        # What hylafax_http.engine_result writes for the SSL Fax engine's report on the call.
        _, profile = store.attempt_context(job, row['attempt_id'])
        store.observe(job, attempt_id=row['attempt_id'], profile_id=profile.id, provider_sid=job, status='success',
                      event_key=f"{row['attempt_id']}:hylafax:")
    assert store.get(job)['state'] == 'success'


@pytest.mark.asyncio
async def test_a_busy_database_during_a_long_preparation_never_fails_the_fax(busy, monkeypatch, caplog):
    """Before: one refused lease renewal ended the heartbeat, and the fax failed as a preparation failure."""
    from api.app.outbound_worker import OutboundWorker
    from api.tests.test_outbound_store import accept
    from api.tests.test_outbound_worker import Transport
    installation = busy.installation
    _, store, _ = installation
    job = accept(installation)
    lock = busy.holder()
    renewals = []
    renewed = asyncio.Event()
    loop = asyncio.get_running_loop()
    original = store.renew_lease

    def renew(claim, **kwargs):
        renewals.append(claim.job_id)
        if len(renewals) == 1:
            lock.hold()  # another writer holds the lock through the first renewal
            try:
                return original(claim, **kwargs)
            finally:
                lock.release()
        result = original(claim, **kwargs)
        if result:
            loop.call_soon_threadsafe(renewed.set)
        return result
    monkeypatch.setattr(store, 'renew_lease', renew)

    async def long_preparation(claim):
        await asyncio.wait_for(renewed.wait(), 20)  # until a renewal after the refused one went through
    transport = Transport()
    transport.preparing = long_preparation
    caplog.set_level('WARNING')
    # A 9-second lease: a beat every 3 seconds, so a loaded machine still renews before the lease ends.
    assert await OutboundWorker(store, transport, lease_seconds=9).step() is True
    assert len(renewals) >= 2 and transport.submissions == 1
    assert store.get(job)['state'] == 'in_progress'
    assert 'could not be renewed this time (database is locked)' in caplog.text


# -- The built-in engine's events: off the event loop, in arrival order --------------------------------------------

@pytest.mark.asyncio
async def test_engine_results_run_off_the_event_loop_in_the_order_asterisk_sent_them(monkeypatch):
    """Two calls' events interleaved; the first result's write is slow (a busy database). Every event is handled
    after the ones before it, on the results thread, and the event loop keeps serving meanwhile."""
    from api.app import engine_results, main
    from api.app.ami import AMIClient
    handled = []
    first_started = threading.Event()
    let_first_finish = threading.Event()

    def result(event):
        if not handled:
            first_started.set()
            assert let_first_finish.wait(10)
        handled.append(('result', event['JobID'], threading.current_thread().name))

    def originate(event):
        handled.append(('originate', event['ActionID'].split(':')[1], threading.current_thread().name))
    monkeypatch.setattr(main, '_handle_fax_result', result)
    monkeypatch.setattr(main, '_handle_originate_response', originate)
    client = AMIClient()
    client.on_fax_result(main._on_fax_result)
    client.on_originate_response(main._on_originate_response)
    a, b = 'a' * 32, 'b' * 32
    arrivals = [
        {'Event': 'UserEvent', 'UserEvent': 'FaxResult', 'JobID': a, 'AttemptID': a},
        {'Event': 'OriginateResponse', 'Response': 'Failure', 'ActionID': f'faxbot:{b}:{b}'},
        {'Event': 'UserEvent', 'UserEvent': 'FaxResult', 'JobID': b, 'AttemptID': b},
        {'Event': 'OriginateResponse', 'Response': 'Failure', 'ActionID': f'faxbot:{a}:{a}'},
    ]
    for event in arrivals:
        client._dispatch(event)  # what the AMI reader does for each event, on the event loop
    # The event loop is free while the first write waits.
    assert await asyncio.wait_for(asyncio.get_running_loop().run_in_executor(None, first_started.wait, 10), 10)
    await asyncio.sleep(0)
    assert handled == []
    let_first_finish.set()
    await engine_results.drain(timeout=10)
    assert [(kind, job) for kind, job, _ in handled] == [('result', a), ('originate', b), ('result', b),
                                                         ('originate', a)]
    assert {thread for _, _, thread in handled} == {f'{engine_results.THREAD_NAME}_0'}


# -- PostgreSQL's lock timeout is recognised the same way ------------------------------------------------------

def test_postgresql_lock_timeout_is_a_busy_database(database):  # noqa: F811
    from api.app.database_busy import busy_cause
    from api.app.routing.database import DELIVERY_LOCK_ID
    if database.dialect.name != 'postgresql':
        error = sa.exc.OperationalError('BEGIN IMMEDIATE', {}, sqlite3.OperationalError('database is locked'))
        assert busy_cause(error) == 'database is locked'
        assert busy_cause(sa.exc.OperationalError('x', {}, sqlite3.OperationalError('no such table: probe'))) is None
        return
    with database.connect() as holder, database.connect() as waiter:
        holder.execute(sa.text('SELECT pg_advisory_xact_lock(:key)'), {'key': DELIVERY_LOCK_ID})
        waiter.execute(sa.text("SELECT set_config('lock_timeout', '50ms', true)"))
        with pytest.raises(sa.exc.OperationalError) as refused:
            waiter.execute(sa.text('SELECT pg_advisory_xact_lock(:key)'), {'key': DELIVERY_LOCK_ID})
        waiter.rollback()
        holder.rollback()
    assert busy_cause(refused.value) == 'lock not available'


# -- Who opens the database: the API and `docker compose exec api faxbot ...` --------------------------------------

def test_the_api_and_its_command_line_run_as_the_same_user_under_the_real_compose_file():
    """WAL adds -wal and -shm files that every process opening the database must be able to write. In Compose the
    API and anything run with ``docker compose exec api`` (the faxbot command, the campaign harness) are one user:
    the api service sets no ``user`` and its image no ``USER``, so both are the image's root. A ``user:`` added to
    one of them later fails here first."""
    import re
    from pathlib import Path
    import yaml
    root = Path(__file__).resolve().parents[2]
    api = yaml.safe_load((root / 'docker-compose.yml').read_text())['services']['api']
    assert 'user' not in api
    assert api['environment'].count('DATABASE_URL=sqlite:////faxdata/faxbot.db') == 1
    dockerfile = (root / 'api' / 'Dockerfile').read_text()
    assert re.search(r'^\s*USER\s', dockerfile, re.MULTILINE) is None
    # Both make their files their own alone (umask 077), the server and the faxbot command.
    assert 'umask 077 && exec uvicorn' in dockerfile and r'#!/bin/sh\numask 077\nexec env' in dockerfile


def test_a_process_with_another_umask_keeps_the_log_files_private(tmp_path):
    """``docker compose exec api python ...`` runs without the faxbot wrapper's umask. SQLite gives -wal and -shm the
    database's own mode, and every Faxbot connection takes away anything more (``keep_database_private``)."""
    import os
    import stat
    import subprocess
    import sys
    from pathlib import Path
    path = tmp_path / 'faxbot.db'
    engine = create_database_engine('sqlite:///' + str(path))
    with engine.begin() as connection:
        connection.exec_driver_sql('CREATE TABLE probe (x INTEGER)')
    engine.dispose()
    script = ("import sys; from api.app.schema import create_database_engine as e; "
              "x = e('sqlite:///' + sys.argv[1]); c = x.connect(); "
              "c.exec_driver_sql('INSERT INTO probe VALUES (1)'); c.commit(); "
              "import os, json; d = sys.argv[2]; "
              "print(json.dumps({n: os.stat(os.path.join(d, n)).st_mode & 0o777 for n in os.listdir(d)})); "
              "c.close(); x.dispose()")
    root = Path(__file__).resolve().parents[2]
    seen = subprocess.run([sys.executable, '-c', script, str(path), str(tmp_path)], cwd=root, check=True,
                          capture_output=True, text=True, preexec_fn=lambda: os.umask(0o022),
                          env={'PATH': os.environ.get('PATH', ''), 'PYTHONPATH': str(root)}).stdout
    import json
    # While it wrote, under umask 022: the database and both log files were the owner's alone.
    assert json.loads(seen) == {'faxbot.db': 0o600, 'faxbot.db-wal': 0o600, 'faxbot.db-shm': 0o600}
    with sqlite3.connect(path) as check:
        assert check.execute('SELECT count(*) FROM probe').fetchone() == (1,)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
