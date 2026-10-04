"""The numbers Faxbot sends from are numbers someone was given, never placeholders.

An earlier release saved a made-up station ID (+10000000000) and FreeSWITCH
caller ID (3035551234) as defaults. Both now default to empty; a saved
configuration that still holds exactly those values has them cleared once at
the next start. An empty station ID on a trunk call is the trunk's caller ID,
FreeSWITCH refuses to call without a caller ID, and HumbleFax's own account
numbers are read (GetUser, cached, never while sending) so the console and
`faxbot numbers list` can show them. All values are synthetic.
"""
import asyncio
import json

import httpx
import pytest

from api.tests.test_cli import Cli, _serve
from api.tests.test_config_env_secrets import (  # noqa: F401 (fixtures)
    Installation, _environment_audits, database_url, installation)
from api.tests.test_config_views import snapshot

ACCESS, SECRET = 'synthetic-access-key', 'synthetic.secret+/=='


# -- placeholders ------------------------------------------------------------------------

def test_new_installations_have_no_placeholder_numbers():
    from app.config_values import ConfigurationValues
    values = ConfigurationValues.from_environment({})
    assert (values.fax_station_id, values.fs_caller_id_number) == ('', '')
    assert values.placeholder_clearings() == {}


def test_saved_placeholders_are_cleared_once_and_typed_numbers_stay(installation, monkeypatch):
    from app.config_values import ConfigurationValues
    with pytest.MonkeyPatch.context() as earlier:  # as an earlier release saved them
        earlier.setattr(ConfigurationValues, 'placeholder_clearings', lambda self: {})
        with installation.start(FAX_LOCAL_STATION_ID='+10000000000', FREESWITCH_CALLER_ID_NUMBER='3035551234'):
            saved = installation.store().read().active.values
            assert (saved.fax_station_id, saved.fs_caller_id_number) == ('+10000000000', '3035551234')
    with installation.start():
        values = installation.store().read().active.values
        assert (values.fax_station_id, values.fs_caller_id_number) == ('', '')
        assert _environment_audits(installation.store())[-1]['settings'] == ['fax_station_id', 'fs_caller_id_number']
    with installation.start():
        assert len(_environment_audits(installation.store())) == 1


def test_a_typed_station_id_is_kept(installation):
    with installation.start(FAX_LOCAL_STATION_ID='+17205550162', FREESWITCH_CALLER_ID_NUMBER='+17205550163'):
        values = installation.store().read().active.values
        assert (values.fax_station_id, values.fs_caller_id_number) == ('+17205550162', '+17205550163')
        assert _environment_audits(installation.store()) == []


# -- FreeSWITCH ----------------------------------------------------------------------------

def test_freeswitch_refuses_to_call_without_a_caller_id():
    from app.freeswitch_service import CALLER_ID_MISSING, CallerIdMissing, build_originate_command
    with pytest.raises(CallerIdMissing) as refused:
        build_originate_command('+15551230001', '/fax/a.tif', 'job-1', gateway_name='gw', caller_id_number='',
                                t38_enable=True)
    assert str(refused.value) == CALLER_ID_MISSING == 'Enter the caller ID number your carrier gave you for FreeSWITCH.'
    assert isinstance(refused.value, ValueError)  # the send is refused before anything is submitted
    command = build_originate_command('+15551230001', '/fax/a.tif', 'job-1', gateway_name='gw',
                                      caller_id_number='+17205550163', t38_enable=False)
    assert 'origination_caller_id_number=+17205550163' in command
    from api.app.config_views import project_admin_settings
    assert project_admin_settings(snapshot())['fs']['problem'] == CALLER_ID_MISSING
    assert project_admin_settings(snapshot({'FREESWITCH_CALLER_ID_NUMBER': '+17205550163'}))['fs']['problem'] is None


# -- HumbleFax account numbers ----------------------------------------------------------------

def _user(numbers_handler):
    def handle(request):
        return numbers_handler(request)
    return httpx.MockTransport(handle)


def _get_user(request):
    assert request.method == 'GET' and request.url.path == '/user'
    return httpx.Response(200, json={'data': {'user': {
        'id': 1, 'name': 'Synthetic', 'assignedFaxNumber': '13035550197',
        'allFaxNumbersCanAccess': ['13035550197', 13035550198], 'isAdmin': False}}})


@pytest.fixture
def humblefax(monkeypatch):
    from api.app import humblefax_service
    monkeypatch.setattr(humblefax_service, '_numbers', {})
    monkeypatch.setattr(humblefax_service, '_reading', {})
    return humblefax_service


def test_humblefax_account_numbers_come_from_getuser(humblefax):
    service = humblefax.HumbleFaxFaxService(ACCESS, SECRET, transport=_user(_get_user))
    assert asyncio.run(service.account_numbers()) == ('+13035550197', '+13035550198')
    refused = humblefax.HumbleFaxFaxService(ACCESS, SECRET, transport=_user(lambda request: httpx.Response(401, json={})))
    with pytest.raises(humblefax.HumbleFaxCredentialsError):
        asyncio.run(refused.account_numbers())


def test_settings_show_the_account_numbers_read_once_and_cached(humblefax, monkeypatch):
    calls = []

    def counted(request):
        calls.append(request.url.path)
        return _get_user(request)
    monkeypatch.setattr(humblefax, 'NUMBERS_TRANSPORT', _user(counted))
    from api.app.config_views import project_admin_settings
    keys = {'HUMBLEFAX_ACCESS_KEY': ACCESS, 'HUMBLEFAX_SECRET_KEY': SECRET}
    assert project_admin_settings(snapshot(keys))['humblefax']['account_numbers'] == ['+13035550197', '+13035550198']
    assert project_admin_settings(snapshot(keys))['humblefax']['account_numbers'] == ['+13035550197', '+13035550198']
    assert calls == ['/user']
    assert project_admin_settings(snapshot())['humblefax']['account_numbers'] == []
    assert ACCESS not in json.dumps(humblefax._numbers, default=str)


def test_a_sent_fax_reports_the_number_it_went_from(humblefax):
    def sent(request):
        return httpx.Response(200, json={'data': {'sentFax': {'id': '123456', 'status': 'success',
                                                               'fromNumber': 13035550199}}})
    service = humblefax.HumbleFaxFaxService(ACCESS, SECRET, transport=_user(sent))
    assert asyncio.run(service.get_fax_status('123456')) == {'provider_sid': '123456', 'status': 'success'}
    account = humblefax._account(ACCESS, SECRET)
    assert humblefax._numbers[account][1] == ('+13035550199',)


def test_under_the_test_harness_no_real_humblefax_request_is_made(humblefax):
    assert humblefax.NUMBERS_TRANSPORT is None
    assert humblefax.account_numbers(ACCESS, SECRET) is None and humblefax._reading == {}


# -- faxbot numbers list ------------------------------------------------------------------------

@pytest.fixture
def numbers_cli(monkeypatch, tmp_path):
    for client in _serve(monkeypatch, tmp_path, SIP_TRUNK_DIDS='+15555550100,+15555550101',
                         HUMBLEFAX_FROM_NUMBER='3035550197'):
        yield Cli(client)


def test_numbers_list_shows_every_carried_number_like_your_numbers(numbers_cli):
    cli = numbers_cli
    cli.json('numbers', 'mailboxes', 'add', 'Front desk')
    cli.json('numbers', 'add', '+15555550100', '--mailbox', 'Front desk')
    rows = {row['number']: row for row in cli.json('numbers', 'list')}
    assert set(rows) == {'+15555550100', '+15555550101', '+13035550197'}
    assert rows['+15555550100']['mailbox'] == 'Front desk'
    assert rows['+15555550101']['mailbox'] is None and rows['+15555550101']['email'] == 'Not emailed'
    assert [item['name'] for item in rows['+13035550197']['providers']] == ['HumbleFax']
    assert rows['+15555550101']['providers'] == [{'provider': 'sip', 'name': 'Carrier trunk', 'in_use': False}]
    # The keys rule rows always had stay, so scripts reading them keep working.
    assert (rows['+15555550100']['to_number'], rows['+15555550100']['mailbox_label']) == ('+15555550100', 'Front desk')
    assert rows['+15555550100']['id'] and rows['+15555550100']['mailbox_id'] and rows['+15555550100']['version']
    assert (rows['+15555550101']['to_number'], rows['+15555550101']['mailbox_label'], rows['+15555550101']['id']) == \
        ('+15555550101', None, None)
    table = cli('numbers', 'list').stdout
    assert 'Carrier trunk (not in use now)' in table and 'No mailbox: received faxes are visible' in table
    assert '3035551234' not in table and 'FreeSWITCH' not in table
