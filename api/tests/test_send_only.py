"""Send-only numbers (B9): shown on faxes you send, never received on here, and never one of your own numbers.

A send-only number is a number the organization already has (its main office number, say) that a trunk shows
as caller ID: a fax to it places a real call, it is never a reply number Faxbot picks, and saving refuses one
that an account receives on. The advice names a trunk number you rent only to send from. Synthetic numbers only.
"""
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
import sqlalchemy as sa

from api.app.config_values import ConfigurationValues
from api.app.routing import local, own_numbers, reply_number, send_only
from api.app.schema import upgrade_schema
from api.tests.test_schema import database  # noqa: F401 (fixture)


OFFICE = '+13035550142'     # the organization's main office number: send-only
RENTED = '+17205550101'     # a number rented on the trunk
NOW = datetime(2026, 10, 8, 12, 0)


def values(**changes):
    return ConfigurationValues.from_environment({
        'FAX_BACKEND': 'sip', 'INBOUND_ENABLED': 'true', 'SIP_TRUNK_PRESET': 'telnyx', 'SIP_TRUNK_AUTH': 'ip',
        'SIP_TRUNK_DIDS': RENTED, 'SIP_TRUNK_CALLER_ID': RENTED, 'FAX_DEFAULT_COUNTRY': 'US', **changes})


def test_a_send_only_number_never_appears_in_your_own_numbers_and_a_fax_to_it_places_a_call():
    shown = values(SIP_TRUNK_CALLER_ID=OFFICE, FAX_SEND_ONLY_NUMBERS=OFFICE)
    assert OFFICE not in own_numbers.receiving_numbers(shown)
    assert OFFICE not in own_numbers.account_numbers(shown)
    assert OFFICE not in reply_number.receiving_numbers(shown) and OFFICE not in reply_number.account_numbers(shown)
    assert local.applies(shown, OFFICE) is False and local.applies(shown, RENTED) is True
    # Even when it was also listed as a trunk number (settings written outside Faxbot), send-only wins.
    both = values(SIP_TRUNK_DIDS=f'{RENTED},{OFFICE}', FAX_SEND_ONLY_NUMBERS=OFFICE)
    assert own_numbers.receiving_numbers(both) == {RENTED}
    # It can never be the reply number: faxes sent to it do not reach this Faxbot.
    assert reply_number.refusal(both, OFFICE, routes=[]) == reply_number.SEND_ONLY.format(number=OFFICE)


def test_saving_refuses_a_number_an_account_receives_on_and_reads_every_other_number():
    assert send_only.checked(values(), ['303 555 0142', '+13035550142', '']) == [OFFICE]
    with pytest.raises(send_only.SendOnlyRefused, match='receives faxes on Telnyx, so it cannot be send-only'):
        send_only.checked(values(), [RENTED])
    with pytest.raises(send_only.SendOnlyRefused, match='not a fax number Faxbot can read'):
        send_only.checked(values(), ['not a number'])
    assert send_only.numbers(values(FAX_SEND_ONLY_NUMBERS=f'{OFFICE},{OFFICE}')) == (OFFICE,)


def test_where_a_send_only_number_shows_and_what_the_carrier_requires():
    [item] = send_only.view(values(SIP_TRUNK_CALLER_ID=OFFICE, FAX_SEND_ONLY_NUMBERS=OFFICE,
                                   FAX_LOCAL_STATION_ID=OFFICE))
    assert item['caller_id_on'] == ['sip'] and item['station_id'] is True
    assert item['sentence'].startswith('Shown as caller ID on Telnyx; sent as the station ID.')
    [rule] = item['carrier_rules']
    assert rule['source_url'] == 'https://support.telnyx.com/en/articles/6790265-verified-numbers-faq'
    assert rule['sentence'].startswith('Telnyx shows another number only when it was bought on your Telnyx account')
    [unused] = send_only.view(values(FAX_SEND_ONLY_NUMBERS=OFFICE))
    assert unused['sentence'].startswith("Not shown yet: set it as a trunk's caller ID or as the station ID.")


def _received(engine, number, when):
    table = sa.Table('sip_call_records', sa.MetaData(), autoload_with=engine)
    with engine.begin() as connection:
        connection.execute(table.insert().values(
            id=uuid4().hex, direction='inbound', call_id=uuid4().hex, did=number, called=number, started_at=when,
            disposition='answered', t38='yes', fax_preference=0, fax_status='SUCCESS', created_at=when,
            updated_at=when))


def test_advice_names_a_number_you_rent_only_to_send_from(database):  # noqa: F811
    upgrade_schema(database)
    [item] = send_only.advice(values(), database, now=NOW)
    assert (item['number'], item['trunk'], item['saving_per_month']['micros']) == (RENTED, 'sip', 1_000_000)
    assert item['sentence'] == (
        f'You rent {RENTED} on Telnyx for $1.00 a month and use it only to show on faxes you send: no fax arrived on '
        'it in 90 days and no rule under Numbers sends its faxes to a mailbox. You could show a number you already '
        'have instead (add it as a send-only number; your carrier may need to verify it first) and release this one '
        'to save $1.00 a month.')
    # Needed: it received a fax in the last 90 days, or it is your reply number.
    assert send_only.advice(values(FAX_REPLY_NUMBER=RENTED), database, now=NOW) == []
    _received(database, RENTED, NOW - timedelta(days=10))
    assert send_only.advice(values(), database, now=NOW) == []


def test_the_trunk_page_and_command_line_save_and_show_send_only_numbers(isolated_installation, monkeypatch):
    from api.tests.test_inbound_acquisition import ADMIN, client, environment
    environment(monkeypatch, FAX_BACKEND='sip', SIP_TRUNK_PRESET='telnyx', SIP_TRUNK_AUTH='ip',
                SIP_TRUNK_DIDS=RENTED, SIP_TRUNK_CALLER_ID=RENTED)
    with client() as http:
        refused = http.put('/admin/sip/send-only', headers=ADMIN, json={'numbers': [RENTED]})
        assert refused.status_code == 409 and 'cannot be send-only' in refused.json()['detail']
        saved = http.put('/admin/sip/send-only', headers=ADMIN, json={'numbers': ['303-555-0142']})
        assert saved.status_code == 200, saved.text
        assert saved.json() == {'ok': True, 'numbers': [OFFICE]}
        shown = http.get('/admin/sip/send-only', headers=ADMIN).json()
        assert [item['number'] for item in shown['numbers']] == [OFFICE] and shown['quiet_days'] == 90
        assert http.get('/admin/sip/send-only').status_code in (401, 403)
