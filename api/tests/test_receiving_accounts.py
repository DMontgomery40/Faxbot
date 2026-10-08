"""Receiving with several provider accounts (provider-rules design §3.4, WP-B).

The first test pins the route gate exactly as it behaved before provider accounts existed. The gate is
kept unchanged until a second receiving account exists, so this table must keep passing after the change.
Provider APIs are a synthetic ``httpx.MockTransport``; nothing leaves the test.
"""
import pytest

from app.inbound import http as inbound_http
from api.tests.test_inbound_acquisition import client, environment, providers  # noqa: F401 (fixture)


def _blocked_audits(monkeypatch):
    seen = []
    original = inbound_http.audit_event

    def record(event, **fields):
        if event == 'inbound_route_blocked':
            seen.append(fields['route'])
        return original(event, **fields)
    monkeypatch.setattr(inbound_http, 'audit_event', record)
    return seen


def _statuses(http):
    """The status each provider's receiving address answers with a notification that names no fax."""
    return {
        '/sinch-inbound': http.post('/sinch-inbound', json={'event': 'INCOMING_FAX'}).status_code,
        '/phaxio-inbound': http.post('/phaxio-inbound', data={'direction': 'received'}).status_code,
        '/_internal/asterisk/inbound': http.post('/_internal/asterisk/inbound', json={'tiff_path': '/nowhere.tiff'},
                                                 headers={'X-Internal-Secret': 'wrong'}).status_code,
    }


# (environment, FAX_INBOUND_BACKEND in the process environment or None, routes answering 404)
GATE = [
    # Receiving provider saved in configuration, no FAX_INBOUND_BACKEND variable: every route stays open and
    # each checks its own secret (the design's "appears to leave every provider's route open", pinned).
    ({'FAX_BACKEND': 'sip'}, None, set()),
    ({'FAX_BACKEND': 'phaxio'}, None, set()),
    # The variable names the receiving provider: only that provider's route is open.
    ({'FAX_BACKEND': 'phaxio'}, 'phaxio', {'/sinch-inbound', '/_internal/asterisk/inbound'}),
    ({'FAX_BACKEND': 'sip'}, 'sinch', {'/phaxio-inbound', '/_internal/asterisk/inbound'}),
    # No receiving provider set up yet: no provider's route is open.
    ({'FAX_BACKEND': ''}, None, {'/sinch-inbound', '/phaxio-inbound', '/_internal/asterisk/inbound'}),
]


@pytest.mark.parametrize('extra, variable, closed', GATE)
def test_the_route_gate_is_unchanged_while_no_extra_account_receives(isolated_installation, monkeypatch, providers,
                                                                     extra, variable, closed):
    environment(monkeypatch, **extra)
    if variable is None:
        monkeypatch.delenv('FAX_INBOUND_BACKEND', raising=False)
    else:
        monkeypatch.setenv('FAX_INBOUND_BACKEND', variable)
    blocked = _blocked_audits(monkeypatch)
    with client() as http:
        statuses = _statuses(http)
    assert {route for route, status in statuses.items() if status == 404} == closed
    # Each closed route wrote one audit row; an open one went on to check its own secret.
    assert sorted(blocked) == sorted(closed)
    assert all(status in (200, 400, 401) for route, status in statuses.items() if route not in closed)


def test_receiving_turned_off_closes_every_route_without_an_audit_row(isolated_installation, monkeypatch, providers):
    environment(monkeypatch, INBOUND_ENABLED='false')
    blocked = _blocked_audits(monkeypatch)
    with client() as http:
        assert set(_statuses(http).values()) == {404}
    assert blocked == []
