"""Receiving rules over HTTP: the number-rule options, Try a received fax, and what a rule does on arrival
(email off or through a chosen connector, urgent work items, keep days, and the stated subaddress)."""
from datetime import datetime, timedelta

import pytest

from app import main
from api.tests.test_inbound_acquisition import (ADMIN, FROM, TO, _tiff, client, environment, feed,  # noqa: F401
                                                providers, rows)


def version(http):
    return http.get('/auth/me', headers=ADMIN).json()['policy_version']


def mailbox(http, label):
    created = http.post('/access/mailboxes', headers=ADMIN, json={'label': label, 'enabled': True,
                                                                  'expected_policy_version': version(http)})
    assert created.status_code == 200, created.text
    return created.json()['mailbox']['id']


def rule(http, **body):
    created = http.post('/access/inbound-rules', headers=ADMIN, json={**body, 'expected_policy_version': version(http)})
    assert created.status_code == 200, created.text
    return created.json()['rule']


def handover(http, tmp_path, name, **extra):
    image = _tiff(tmp_path / 'faxdata' / 'inbound' / f'{name}.tiff')
    answer = http.post('/_internal/asterisk/inbound', headers={'X-Internal-Secret': 'synthetic-internal'},
                       json={'tiff_path': str(image), 'to_number': TO, 'from_number': FROM, 'faxstatus': 'SUCCESS',
                             'faxpages': 1, 'uniqueid': f'1791049{abs(hash(name)) % 1000:03d}.1', **extra})
    assert answer.status_code == 200, answer.text
    return answer.json()['id']


# The T.30 SUB frame for subaddress 2001 (characters sent last first), as patch 0004 reports it in hex.
SUB_2001 = 'ff03c2' + '2001'[::-1].encode().hex()


@pytest.fixture
def http(isolated_installation, monkeypatch, providers, tmp_path):  # noqa: F811
    environment(monkeypatch, FAX_BACKEND='sip')
    with client() as http:
        yield http


def test_number_rule_options_are_saved_listed_placed_and_explained(http, isolated_installation):
    front, billing = mailbox(http, 'Front desk'), mailbox(http, 'Billing')
    plain = rule(http, to_number=TO, mailbox_id=front)
    assert (plain['position'], plain['enabled'], plain['subaddress'], plain['from_numbers']) == (1, True, None, [])
    # A second rule for the same number needs a condition, and is read before the plain one.
    sub = rule(http, to_number=TO, mailbox_id=billing, subaddress='20 01', urgent=True, keep_days=30)
    assert (sub['position'], sub['subaddress'], sub['urgent'], sub['keep_days']) == (1, '2001', True, 30)
    duplicate = http.post('/access/inbound-rules', headers=ADMIN, json={
        'to_number': TO, 'mailbox_id': billing, 'expected_policy_version': version(http)})
    assert duplicate.status_code == 400
    refused = http.post('/access/inbound-rules', headers=ADMIN, json={
        'to_number': TO, 'mailbox_id': billing, 'start_minute': 60, 'expected_policy_version': version(http)})
    assert (refused.status_code, refused.json()['detail']) == (400, 'Give both a start and an end time, or neither.')
    listed = {item['id']: item for item in http.get('/access/inbound-rules', headers=ADMIN).json()['items']}
    assert {(item['position'], item['mailbox_label']) for item in listed.values()} == {(1, 'Billing'), (2, 'Front desk')}
    # Try a received fax: the subaddress decides, nothing is saved.
    explain = lambda **body: http.post('/access/inbound-rules/explain', headers=ADMIN,  # noqa: E731
                                       json={'to_number': TO, 'from_number': FROM, **body}).json()
    with_sub = explain(subaddress='2001')
    assert (with_sub['mailbox_label'], with_sub['urgent'], with_sub['keep_days']) == ('Billing', True, 30)
    assert with_sub['sentence'] == f'It would go to Billing, marked urgent, not emailed and kept for 30 days, by the rule for {TO}.'
    assert explain()['mailbox_label'] == 'Front desk'
    assert explain(to_number='+15555550999')['sentence'] == ('No number rule matches it, so it would wait in Received '
                                                             'with no mailbox, not emailed.')
    assert rows(isolated_installation, 'inbound_faxes') == []
    # A change counts in the rule's version; moving it changes its place.
    moved = http.patch(f"/access/inbound-rules/{plain['id']}", headers=ADMIN, json={
        'position': 1, 'version': plain['version'], 'expected_policy_version': version(http)}).json()['rule']
    assert (moved['position'], moved['version']) == (1, plain['version'] + 1)
    assert explain(subaddress='2001')['mailbox_label'] == 'Front desk'


def test_a_stated_subaddress_places_a_trunk_fax_and_is_recorded(http, isolated_installation, tmp_path):
    front, billing = mailbox(http, 'Front desk'), mailbox(http, 'Billing')
    rule(http, to_number=TO, mailbox_id=front)
    rule(http, to_number=TO, mailbox_id=billing, subaddress='2001')
    # Built-in engine: the SUB frame in hex; SSL Fax engine: the subaddress as text; none stated: the plain rule.
    from_frame = handover(http, tmp_path, 'frame', sub_hex=SUB_2001)
    from_text = handover(http, tmp_path, 'text', subaddress='2001')
    unstated = handover(http, tmp_path, 'none')
    faxes = {fax['id']: fax for fax in http.get('/inbound', headers=ADMIN).json()}
    assert [faxes[fax]['mailbox'] for fax in (from_frame, from_text, unstated)] == ['Billing', 'Billing', 'Front desk']
    assert [faxes[fax]['subaddress'] for fax in (from_frame, from_text, unstated)] == ['2001', '2001', None]
    assert faxes[from_frame]['account_key'] == 'sip'


def test_the_frames_row_supplies_the_subaddress_when_the_hand_over_has_none(http, isolated_installation, tmp_path):
    import sqlalchemy as sa
    front, billing = mailbox(http, 'Front desk'), mailbox(http, 'Billing')
    rule(http, to_number=TO, mailbox_id=front)
    rule(http, to_number=TO, mailbox_id=billing, subaddress='2001')
    engine = sa.create_engine(isolated_installation['DATABASE_URL'])
    try:
        frames = sa.Table('fax_call_frames', sa.MetaData(), autoload_with=engine)
        with engine.begin() as connection:
            connection.execute(frames.insert().values(id='in:1791049555.5', direction='in', call_key='1791049555.5',
                                                      sub=SUB_2001, created_at=datetime.utcnow()))
    finally:
        engine.dispose()
    image = _tiff(tmp_path / 'faxdata' / 'inbound' / 'framed.tiff')
    answer = http.post('/_internal/asterisk/inbound', headers={'X-Internal-Secret': 'synthetic-internal'},
                       json={'tiff_path': str(image), 'to_number': TO, 'from_number': FROM, 'faxstatus': 'SUCCESS',
                             'faxpages': 1, 'uniqueid': '1791049555.5'})
    assert http.get(f"/inbound/{answer.json()['id']}", headers=ADMIN).json()['mailbox'] == 'Billing'


def test_email_off_a_chosen_connector_keep_days_and_urgent_work(http, isolated_installation, tmp_path):
    from app.intake.store import IntakeStore
    from app.intake.worker import ConnectorSecrets
    from app.work.store import WorkStore
    runtime = main.app.state.configuration_runtime
    store = IntakeStore(runtime.manager.store.engine, ConnectorSecrets(runtime.manager.store))
    settings = lambda to: {'host': 'smtp.example.org', 'port': 587, 'security': 'starttls',  # noqa: E731
                           'from_address': 'fax@example.org', 'recipients': [to]}
    store.create_connector(name='Everyone', settings=settings('desk@example.org'))
    chosen = store.create_connector(name='Billing team', settings=settings('billing@example.org'),
                                    match_number='+15555550999')
    front, billing, cases = mailbox(http, 'Front desk'), mailbox(http, 'Billing'), mailbox(http, 'Cases')
    rule(http, to_number=TO, mailbox_id=front, email_off=True, subaddress='1')
    rule(http, to_number=TO, mailbox_id=billing, email_connector_id=chosen.id, subaddress='2', keep_days=3,
         urgent=True)
    rule(http, to_number=TO, mailbox_id=cases)
    silent = handover(http, tmp_path, 'silent', subaddress='1')
    billed = handover(http, tmp_path, 'billed', subaddress='2')
    usual = handover(http, tmp_path, 'usual')
    assert feed() == 3
    items = {item['inbound_fax_id']: item for item in rows(isolated_installation, 'intake_items')}
    assert items[silent]['next_attempt_at'] is None
    assert items[silent]['last_error'] == 'The number rule for this fax sends no email.'
    assert items[billed]['next_attempt_at'] is not None and items[usual]['next_attempt_at'] is not None
    assert store.connector_for_item(items[billed]).name == 'Billing team'
    assert store.connector_for_item(items[usual]).name == 'Everyone'
    faxes = {row['id']: row for row in rows(isolated_installation, 'inbound_faxes')}
    moment = lambda value: value if isinstance(value, datetime) else datetime.fromisoformat(value)  # noqa: E731
    kept = moment(faxes[billed]['retention_until']) - moment(faxes[billed]['updated_at'])
    assert timedelta(days=3) - timedelta(seconds=5) <= kept <= timedelta(days=3)
    assert moment(faxes[usual]['retention_until']) - moment(faxes[usual]['updated_at']) >= timedelta(days=29)
    # The urgent fax's work item says so and comes first among open items.
    assert WorkStore(runtime.manager.store.engine).feed() == 3
    work = http.get('/work', headers=ADMIN).json()
    listed = work['items'] if isinstance(work, dict) else work
    assert listed[0]['inbound_fax_id'] == billed and listed[0]['urgent'] is True
    assert {item['inbound_fax_id'] for item in listed if not item['urgent']} == {silent, usual}
