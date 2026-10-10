"""Sent faxes whose state changed in the last hours: the list Needs attention opens for "failed in the last 24 hours".

GET /admin/fax-jobs?since_hours= and `faxbot sent list --hours` keep only faxes whose delivery changed within that
many hours, by the same clock and column as the Overview's count, inside the same visibility as the list.
"""
from datetime import datetime, timedelta

import sqlalchemy as sa

import app.main as main_module
from api.tests.test_cli import BOOTSTRAP, Cli, _serve

import pytest

KEY = {"X-API-Key": BOOTSTRAP}


@pytest.fixture
def cli(monkeypatch, tmp_path):
    for client in _serve(monkeypatch, tmp_path):
        yield Cli(client)


def _age(job_id, hours):
    """Make one synthetic fax's delivery look as if it last changed this many hours ago."""
    engine = main_module.app.state.configuration_runtime.manager.store.engine
    deliveries = sa.Table('outbound_deliveries', sa.MetaData(), autoload_with=engine)
    with engine.begin() as connection:
        connection.execute(deliveries.update().where(deliveries.c.id == job_id).values(
            updated_at=datetime.utcnow() - timedelta(hours=hours)))


def _send(cli, tmp_path, number):
    note = tmp_path / f'{number}.txt'
    note.write_text('Synthetic fax\n')
    return cli.json('send', number, note, '--queue')['id']


def test_the_list_keeps_only_faxes_that_changed_within_the_hours_asked(cli, tmp_path):
    recent = _send(cli, tmp_path, '+15551230001')
    older = _send(cli, tmp_path, '+15551230002')
    _age(older, 30)
    every = cli.client.get('/admin/fax-jobs', params={'status': 'held'}, headers=KEY)
    assert every.status_code == 200, every.text
    assert {job['id'] for job in every.json()['jobs']} == {recent, older}
    lately = cli.client.get('/admin/fax-jobs', params={'status': 'held', 'since_hours': 24}, headers=KEY)
    assert lately.status_code == 200, lately.text
    assert lately.json()['total'] == 1 and [job['id'] for job in lately.json()['jobs']] == [recent]
    wider = cli.client.get('/admin/fax-jobs', params={'since_hours': 48}, headers=KEY)
    assert wider.json()['total'] == 2
    for refused in (0, 9000, 'day'):
        answer = cli.client.get('/admin/fax-jobs', params={'since_hours': refused}, headers=KEY)
        assert answer.status_code == 422, (refused, answer.text)

    listed = cli.json('sent', 'list', '--status', 'held', '--hours', '24', '--ids')
    assert [job['id'] for job in listed['jobs']] == [recent]
    shown = cli('sent', 'list', '--status', 'held', '--hours', '24')
    assert shown.exit_code == 0 and 'Only faxes whose state changed in the last 24 hours.' in shown.stdout
