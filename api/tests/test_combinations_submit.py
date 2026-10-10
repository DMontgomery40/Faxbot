"""Combinations, group 7: API and CLI submit x delayed estimates x reload x retry (brief 104).

The real application in process (``tests/test_cli.py``'s server: an installation, its access policy and storage)
with the ``faxbot`` command line on the same TestClient. The invariant: **one submission is one fax**. The same
Idempotency-Key with the same request returns the fax already accepted, whether it comes back from the command line
or the API, after a lost answer, after an estimate for the same document, or after the fax was already sent; the
same key with anything different is refused and leaves nothing behind; a request without a key is a new fax, and
nothing is ever retried by the client on its own. What applied is read back from the installation's own records:
the fax rows, their deliveries and attempts, and the files in the data folder.

The console's own reload test (its key kept across a reload) and pointer test belong to faxbot-1a. Synthetic
documents and numbers; nothing is dialed. SQLite.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json

import httpx
import pytest
import sqlalchemy as sa

import app.main as main_module
from api.tests.combinations_support import Case, record
from api.tests.test_cli import BOOTSTRAP, Cli, server  # noqa: F401 - fixture

GROUP = '7 API and CLI submit x estimates x reload x retry'
INVARIANT = 'one submission is one fax (same key and request: the same fax; anything else: refused or a new fax)'
NUMBER = '+15551230001'
DOCUMENT = b'Synthetic referral letter for the combination suite\n'


def store():
    return main_module.app.state.configuration_runtime.manager.store


def faxes():
    """Every accepted fax, its delivery and its attempts, read back from the installation's records."""
    configuration = store()
    with configuration.engine.connect() as connection:
        jobs = connection.execute(sa.select(configuration.jobs.c.id, configuration.jobs.c.to_number)
                                  .order_by(configuration.jobs.c.created_at)).all()
        deliveries = connection.scalar(sa.select(sa.func.count()).select_from(
            configuration.delivery_tables['outbound_deliveries']))
        attempts = connection.scalar(sa.select(sa.func.count()).select_from(
            configuration.delivery_tables['outbound_attempts']))
    return [row.id for row in jobs], deliveries, attempts


def data_files():
    """The fax files in the data folder (the server's own lock files left out)."""
    return sorted(path.name for path in store_root().iterdir() if path.is_file() and not path.name.startswith('.'))


def post(client, *, key=None, document=DOCUMENT, to=NUMBER, **form):
    headers = {'X-API-Key': BOOTSTRAP, 'Origin': 'https://testserver'}
    if key is not None:
        headers['Idempotency-Key'] = key
    return client.post('/fax', data={'to': to, 'queue_only': 'true', **form},
                       files={'file': ('letter.txt', document, 'text/plain')}, headers=headers)


class LosesFirstAnswer:
    """The command line's HTTP client, losing the first ``POST /fax`` answer after the server accepted it (the
    request reached Faxbot; the answer timed out on the way back)."""

    def __init__(self, client):
        self.client, self.lost, self.posts = client, False, 0

    def request(self, method, path, **kwargs):
        response = self.client.request(method, path, **kwargs)
        if method == 'POST' and path == '/fax':
            self.posts += 1
            if not self.lost:
                self.lost = True
                raise httpx.ReadTimeout('synthetic: the answer was lost after Faxbot accepted the fax')
        return response

    def close(self):
        pass


def cli_with(client, http):
    found = Cli(client)

    def invoke(*args, key=BOOTSTRAP):
        from app.cli.main import app as cli_app
        prefix = ['--url', 'https://testserver', '--key', key]
        return found.runner.invoke(cli_app, prefix + [str(arg) for arg in args],
                                   obj={'client_factory': lambda address, timeout: (http, False)})
    return invoke


@pytest.fixture
def letter(tmp_path):
    path = tmp_path / 'letter.txt'
    path.write_bytes(DOCUMENT)
    return path


# Retry after a lost answer -----------------------------------------------------------------------------------

def test_a_cli_send_whose_answer_was_lost_and_is_run_again_with_its_key_is_one_fax(server, letter):
    http = LosesFirstAnswer(server)
    run = cli_with(server, http)
    lost = run('send', NUMBER, letter, '--queue', '--idempotency-key', 'combo-retry-1')
    # The command line never retries by itself and says so.
    assert lost.exit_code == 8 and http.posts == 1
    assert 'did not answer in time. Nothing was retried; check before running the command again.' in lost.stderr
    jobs, deliveries, _ = faxes()
    assert len(jobs) == deliveries == 1  # Faxbot accepted it although the answer never came back
    again = run('--json', 'send', NUMBER, letter, '--queue', '--idempotency-key', 'combo-retry-1')
    assert again.exit_code == 0, again.stderr
    assert json.loads(again.stdout)['id'] == jobs[0]
    assert faxes()[:2] == (jobs, 1)
    record(Case(GROUP, 'CLI retry after a lost answer, same key', ('cli', 'timeout', 'retry', 'key'),
                ('one fax',), INVARIANT))


def test_a_cli_send_without_a_key_is_never_retried_by_itself_and_running_it_again_is_a_second_fax(server, letter):
    """Without a key Faxbot cannot tell a retry from a deliberate second send: the command line retries nothing and
    says to check first; running it again anyway is a new fax, by design."""
    http = LosesFirstAnswer(server)
    run = cli_with(server, http)
    lost = run('send', NUMBER, letter, '--queue')
    assert lost.exit_code == 8 and http.posts == 1 and 'Nothing was retried' in lost.stderr
    [first], _, _ = faxes()
    # Checking first finds the fax that was accepted.
    shown = run('--json', 'status', first)
    assert shown.exit_code == 0 and json.loads(shown.stdout)['id'] == first
    again = run('--json', 'send', NUMBER, letter, '--queue')
    assert again.exit_code == 0 and json.loads(again.stdout)['id'] != first
    assert len(faxes()[0]) == 2
    # The group's invariant (one submission is one fax) does not hold for this retry: recorded as a candidate defect
    # for the lead to decide (C20: should the command line make its own key and print it when the answer is lost?).
    # If it is a defect, this case becomes a strict xfail asserting one fax.
    record(Case(GROUP, 'CLI retry after a lost answer, no key', ('cli', 'timeout', 'retry'), ('two faxes',),
                INVARIANT, 'xfail', defect='C20 (candidate): a keyless re-run after a lost answer makes a second '
                                           'fax; the command says to check first'))


# One request from the command line and the API alike ----------------------------------------------------------

def test_the_same_key_from_the_cli_and_then_the_api_is_one_fax(server, letter):
    cli = Cli(server)
    sent = cli.json('send', NUMBER, letter, '--queue', '--idempotency-key', 'combo-both-1')
    replay = post(server, key='combo-both-1')
    assert replay.status_code == 202, replay.text
    assert replay.json()['id'] == sent['id']
    jobs, deliveries, attempts = faxes()
    assert jobs == [sent['id']] and deliveries == 1 and attempts == 0
    record(Case(GROUP, 'CLI then API, same key', ('cli', 'api', 'key'), ('one fax',), INVARIANT))


SEND_BY = (datetime.now(timezone.utc) + timedelta(hours=3)).replace(microsecond=0).isoformat()
FACTS = [
    ('urgent', {'urgent': 'true'}, {'urgent': 'false'}),
    ('send by', {'send_by': SEND_BY}, {'send_by': (datetime.fromisoformat(SEND_BY) + timedelta(hours=1)).isoformat()}),
    ('by call', {'send_by_call': 'true'}, {'send_by_call': 'false'}),
    ('another number', {}, {'to': '+15551230002'}),
    ('another document', {}, {'document': DOCUMENT + b'changed\n'}),
]


@pytest.mark.parametrize('fact, form, changed', FACTS, ids=[row[0] for row in FACTS])
def test_a_replay_with_the_same_key_is_the_same_fax_and_any_change_is_refused_with_nothing_left(server, fact, form,
                                                                                              changed):
    first = post(server, key='combo-' + fact.replace(' ', '-'), **form)
    assert first.status_code == 202, first.text
    before = data_files()
    # Double-submit: the same request again (a second click, a reload that sends again).
    again = post(server, key='combo-' + fact.replace(' ', '-'), **form)
    assert again.status_code == 202 and again.json()['id'] == first.json()['id']
    # The same key with a different request: refused, nothing accepted and no file left behind.
    refused = post(server, key='combo-' + fact.replace(' ', '-'), **{**form, **changed})
    assert refused.status_code == 409
    assert refused.json()['detail'] == 'Idempotency-Key already belongs to a different fax request.'
    jobs, deliveries, _ = faxes()
    assert jobs == [first.json()['id']] and deliveries == 1
    assert data_files() == before
    record(Case(GROUP, f'API replay with {fact}', ('api', 'key', 'double-submit', fact),
                ('one fax', 'changed: refused'), INVARIANT))


# Estimates that arrive before, during or after the send -------------------------------------------------------

def test_an_estimate_for_the_same_document_before_and_after_the_send_never_makes_or_changes_a_fax(server):
    headers = {'X-API-Key': BOOTSTRAP, 'Origin': 'https://testserver'}

    def estimate():
        return server.post('/routing/predict', data={'to': NUMBER},
                           files={'file': ('letter.txt', DOCUMENT, 'text/plain')}, headers=headers)
    early = estimate()
    assert early.status_code == 200, early.text
    assert faxes()[0] == [] and data_files() == []
    first = post(server, key='combo-estimate-1')
    assert first.status_code == 202
    job = first.json()['id']
    accepted = {name: hashlib.sha256((store_root() / name).read_bytes()).hexdigest() for name in data_files()}
    # The estimate the Send page asked for before sending arrives late, and the page asks again.
    late = estimate()
    assert late.status_code == 200, late.text
    assert faxes()[0] == [job]
    assert {name: hashlib.sha256((store_root() / name).read_bytes()).hexdigest() for name in data_files()} == accepted
    # Sending again after the estimate is the same fax.
    again = post(server, key='combo-estimate-1')
    assert again.status_code == 202 and again.json()['id'] == job
    assert faxes()[0] == [job]
    record(Case(GROUP, 'estimates before and after the send, then a replay', ('api', 'estimate', 'key'),
                ('one fax', 'no estimate wrote a fax or file'), INVARIANT))


def store_root():
    from pathlib import Path
    return Path(store().read().active.values.fax_data_dir)


# A replay after the fax was already sent -----------------------------------------------------------------------

@pytest.fixture
def sending(monkeypatch, tmp_path):
    """The same server with sending on, its background worker submitting to a stand-in Phaxio that records each
    submission (as tests/test_api.py's lost-answer test does); nothing leaves the process."""
    from api.tests.test_cli import _serve
    submitted = []

    class Provider:
        status_callback_url = None

        def is_configured(self):
            return True

        async def send_fax(self, to, url, job_id, *, attempt_id):
            submitted.append((job_id, attempt_id))
            return {'provider_sid': f'PX-{len(submitted)}', 'status': 'queued'}
    monkeypatch.setattr('app.outbound_transport.service_from_profile', lambda profile: Provider())
    for client in _serve(monkeypatch, tmp_path, FAX_DISABLED='false', PHAXIO_API_KEY='synthetic-key',
                         PHAXIO_API_SECRET='synthetic-secret'):
        client.submitted = submitted
        yield client


def wait_for(condition, seconds=20.0):
    """Wait until ``condition()`` is true (the background worker runs on its own schedule), at most ``seconds``."""
    import time
    deadline = time.monotonic() + seconds
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError('the condition did not hold in time')
        time.sleep(0.05)


def test_a_replay_after_the_fax_was_submitted_returns_it_and_submits_nothing_again(sending):
    """The background worker submitted the fax; the same request comes back from a reloaded page: the answer is that
    fax, with no new delivery, attempt or submission. The fax is past being claimable (in progress), so no later
    worker pass can submit it again."""
    from app.outbound_store import OutboundStore
    first = post(sending, key='combo-after-1', queue_only='false')
    assert first.status_code == 202, first.text
    job = first.json()['id']
    outbound = OutboundStore(store())
    wait_for(lambda: sending.submitted and outbound.get(job)['state'] != 'ready')
    assert [item[0] for item in sending.submitted] == [job]
    again = post(sending, key='combo-after-1', queue_only='false')
    assert again.status_code == 202 and again.json()['id'] == job
    jobs, deliveries, attempts = faxes()
    assert jobs == [job] and deliveries == 1 and attempts == 1
    assert outbound.get(job)['state'] in ('in_progress', 'success') and len(sending.submitted) == 1
    # A request with another key is a deliberate second fax, submitted on its own (to another number here: Faxbot
    # places one call at a time to a number, and the first fax's call never ends in this stand-in).
    second = post(sending, key='combo-after-2', queue_only='false', to='+15551230002')
    assert second.status_code == 202 and second.json()['id'] != job
    wait_for(lambda: len(sending.submitted) == 2)
    assert sorted(item[0] for item in sending.submitted) == sorted([job, second.json()['id']])
    record(Case(GROUP, 'replay after the worker submitted the fax', ('api', 'key', 'reload', 'submitted'),
                ('one fax, one submission',), INVARIANT))


# Retries after a refusal, and after an acceptance whose commit answer was lost ----------------------------------

def test_a_refused_request_binds_no_key_so_the_corrected_retry_with_it_is_the_one_fax(server):
    refused = post(server, key='combo-corrected-1', to='abc')
    assert refused.status_code == 400
    assert faxes()[0] == [] and data_files() == []
    fixed = post(server, key='combo-corrected-1')
    assert fixed.status_code == 202, fixed.text
    again = post(server, key='combo-corrected-1')
    assert again.status_code == 202 and again.json()['id'] == fixed.json()['id']
    assert faxes()[:2] == ([fixed.json()['id']], 1)
    record(Case(GROUP, 'retry with the same key after a refusal', ('api', 'key', 'refused', 'retry'),
                ('one fax',), INVARIANT))


def test_an_acceptance_whose_commit_answer_was_lost_is_found_again_by_its_key_and_never_doubled(server,
                                                                                                monkeypatch):
    """The database committed the fax but the answer to COMMIT was lost: Faxbot keeps the document, answers 503 with
    the fax's ID to check, and the retry with the same key returns that fax."""
    from app.access import outbound
    from app.config_store import ConfigurationCommitUncertain
    real = outbound.AuthorizedOutbound.accept
    lost = []

    def commit_then_lose_the_answer(self, actor, revision, job, **kwargs):
        real(self, actor, revision, job, **kwargs)
        if not lost:
            lost.append(job['id'])
            raise ConfigurationCommitUncertain('synthetic: the answer to COMMIT was lost')
    monkeypatch.setattr(outbound.AuthorizedOutbound, 'accept', commit_then_lose_the_answer)
    first = post(server, key='combo-uncertain-1')
    assert first.status_code == 503
    assert first.json()['detail'] == f'Fax acceptance is uncertain. Retain job {lost[0]} for reconciliation.'
    assert faxes()[0] == lost and f'{lost[0]}.pdf' in data_files()  # the document is kept with the fax
    retry = post(server, key='combo-uncertain-1')
    assert retry.status_code == 202 and retry.json()['id'] == lost[0]
    assert faxes()[:2] == (lost, 1)
    record(Case(GROUP, 'retry with the same key after an uncertain commit', ('api', 'key', 'timeout', 'retry'),
                ('one fax',), INVARIANT))
