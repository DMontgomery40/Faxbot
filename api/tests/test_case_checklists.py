"""Checklist packets: the same checklist, originals and day always build the same packet, with reasons."""
from datetime import date, datetime
import random

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from app import main
from app.cases.checklist import EXAMPLE_ITEMS, CaseChecklists, build, parse_items
from app.cases.ledger import CaseInputError
from app.schema import upgrade_schema
from api.tests.test_cases import ADMIN, BOOTSTRAP, TO, pdf
from api.tests.test_routing_http import scoped_key
from api.tests.test_schema import database  # noqa: F401 - fixture


AS_OF = date(2026, 10, 7)


def original(identity, kind, *, dated=None, version='', digest=None, title=None, source=''):
    return {'id': identity, 'title': title or kind, 'document_type': kind, 'version': version, 'source': source,
            'document_date': datetime.fromisoformat(dated) if dated else None, 'digest': digest or identity * 8,
            'page_count': 1}


ORIGINALS = [
    original('a', 'Discharge summary', dated='2026-09-20', version='final', digest='1' * 64),
    original('b', 'Discharge summary', dated='2026-09-28', version='draft', digest='2' * 64),
    original('c', 'Discharge summary', dated='2026-06-01', version='final', digest='3' * 64),
    original('d', 'Medication list', dated='2026-10-01', digest='4' * 64),
    original('e', 'Medication list', dated='2026-10-01', digest='0' * 64),  # same day: the bytes decide
    original('f', 'Insurance card', digest='5' * 64),
    original('g', 'Scanned pages', title='Lab results from Mesa Clinic', dated='2026-09-01', digest='6' * 64),
]


def test_the_same_inputs_in_any_order_build_the_same_packet_with_a_reason_for_each_pick():
    items = parse_items([dict(item) for item in EXAMPLE_ITEMS])
    picks, missing, suggestions = build(items, ORIGINALS, AS_OF)
    assert [(pick.item, pick.original['id']) for pick in picks] == [(0, 'a'), (1, 'd'), (3, 'f')]
    assert [pick.reason for pick in picks] == [
        "Matches 'Discharge summary': version 'final', dated 20 September 2026, within 30 days of 7 October 2026.",
        "Matches 'Medication list': dated 1 October 2026, within 30 days of 7 October 2026.",
        "Matches 'Insurance card'."]
    assert missing == [{'item': 2, 'type': 'Lab results', 'required': False,
                        'reason': "No 'Lab results' is in the case."}]
    assert suggestions == []  # off by default, even though a title clearly matches
    shuffled = list(ORIGINALS)
    for seed in range(5):
        random.Random(seed).shuffle(shuffled)
        again = build(items, shuffled, AS_OF)
        assert [(pick.item, pick.original['id'], pick.reason) for pick in again[0]] == [
            (pick.item, pick.original['id'], pick.reason) for pick in picks]
        assert again[1] == missing


def test_missing_items_say_why_and_suggestions_are_never_picked():
    items = parse_items([{'type': 'Discharge summary', 'version': 'final', 'within_days': 7},
                         {'type': 'Operative report'},
                         {'type': 'Discharge summary', 'version': 'signed'},
                         {'type': 'Insurance card', 'within_days': 30},
                         {'type': 'Lab results', 'required': False}])
    picks, missing, suggestions = build(items, ORIGINALS, AS_OF, suggestions=True)
    assert [pick.original['id'] for pick in picks] == []
    assert [entry['reason'] for entry in missing] == [
        "The newest 'Discharge summary' in the case is dated 28 September 2026, more than 7 days before 7 October 2026.",
        "No 'Operative report' is in the case.",
        "The case has 'Discharge summary' but not version 'signed'.",
        "The case's 'Insurance card' has no date, and this item needs one within 30 days.",
        "No 'Lab results' is in the case."]
    # Near misses and title matches are offered for a person to check, newest first.
    assert [(entry['item'], entry['original_id']) for entry in suggestions] == [
        (0, 'b'), (0, 'a'), (0, 'c'), (2, 'b'), (2, 'a'), (2, 'c'), (3, 'f'), (4, 'g')]
    assert not {entry['original_id'] for entry in suggestions} & {pick.original['id'] for pick in picks}
    # With every summary picked by earlier items, a later one says so.
    picks, missing, _ = build(parse_items([{'type': 'Medication list'}, {'type': 'Medication list', 'within_days': 30},
                                           {'type': 'Medication list', 'within_days': 60}]), ORIGINALS, AS_OF)
    assert [pick.original['id'] for pick in picks] == ['d', 'e']
    assert missing[0]['reason'] == "Every matching 'Medication list' is already used for an earlier item."


@pytest.mark.parametrize('bad', [[], [{'type': ''}], [{'type': 'Card', 'required': 'yes'}],
                                 [{'type': 'Card', 'within_days': 0}], [{'type': 'Card', 'colour': 'blue'}],
                                 [{'type': 'Card'}, {'type': ' card '}]])
def test_checklist_items_are_checked(bad):
    with pytest.raises(CaseInputError):
        parse_items(bad)


def test_checklist_versions_never_change_once_saved(database):
    upgrade_schema(database)
    store = CaseChecklists(database)
    first = store.add('Mesa payer: prior authorization', [{'type': 'Referral'}])
    second = store.add('mesa payer: PRIOR authorization', [{'type': 'Referral'}, {'type': 'Insurance card'}])
    assert (first['version'], second['version'], second['name']) == (1, 2, 'Mesa payer: prior authorization')
    assert store.find('MESA PAYER: prior authorization', version=1)['items'] == [
        {'type': 'Referral', 'required': True, 'within_days': None, 'version': None}]
    assert store.find('Mesa payer: prior authorization')['version'] == 2
    assert [item['version'] for item in store.versions('Mesa payer: prior authorization')] == [2, 1]
    assert [(item['name'], item['version'], item['used']) for item in store.latest()] == [
        ('Mesa payer: prior authorization', 2, 0)]
    with database.connect() as connection:
        rows = connection.execute(sa.text('SELECT version, items FROM case_checklists ORDER BY version')).all()
    assert [row.version for row in rows] == [1, 2] and '"Referral"' in rows[0].items


# -- through the API ----------------------------------------------------------------------------------------------

@pytest.fixture
def client(isolated_installation, monkeypatch):
    for name, value in {'REQUIRE_API_KEY': 'true', 'API_KEY': BOOTSTRAP, 'PUBLIC_API_URL': 'https://testserver',
                        'FAX_BACKEND': 'phaxio', 'MAX_REQUESTS_PER_MINUTE': '0',
                        'DIRECT_ORGANIZATION': 'Valley Hospital'}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv('FAXBOT_CONSOLE_ORIGINS', 'https://testserver')
    with TestClient(main.app, base_url='https://testserver', headers={'Origin': 'https://testserver'}) as client:
        yield client


FILES = [('Discharge summary 20 Sep', 'Discharge summary', '2026-09-20', 'final', 3),
         ('Discharge summary draft', 'Discharge summary', '2026-09-28', 'draft', 3),
         ('Medication list', 'Medication list', '2026-10-01', '', 1),
         ('Insurance card', 'Insurance card', '', '', 1),
         ('Lab results Mesa', 'Scanned pages', '2026-09-01', '', 2)]


def add_originals(client, case, files):
    response = client.post(f'/cases/{case}/originals', headers=ADMIN,
                           files=[('documents', (f'{title}.pdf', pdf(title, pages), 'application/pdf'))
                                  for title, _, _, _, pages in files],
                           data={'titles': [row[0] for row in files], 'types': [row[1] for row in files],
                                 'dates': [row[2] for row in files], 'versions': [row[3] for row in files]})
    assert response.status_code == 201, response.text
    return response.json()['originals']


def build_packet(client, case, checklist, **body):
    return client.post(f'/cases/{case}/checklist-packets', headers=ADMIN,
                       json={'to': TO, 'checklist_id': checklist, 'as_of': '2026-10-07', **body})


def test_a_checklist_packet_previews_picks_and_missing_items_then_sends_only_the_confirmed_selection(client):
    listed = client.get('/case-checklists', headers=ADMIN).json()
    assert listed['checklists'] == [] and listed['suggestions'] is False
    assert listed['example']['items'][0] == {'type': 'Discharge summary', 'required': True, 'within_days': 30,
                                             'version': 'final'}
    assert client.get('/admin/settings', headers=ADMIN).json()['cases'] == {'suggestions': False}
    saved = client.post('/case-checklists', headers=ADMIN, json={'name': 'Discharge follow-up', 'to': TO,
                                                                 'items': listed['example']['items']})
    assert saved.status_code == 201, saved.text
    checklist = saved.json()
    assert (checklist['version'], checklist['to'], checklist['used']) == (1, TO, 0)
    kept = add_originals(client, 'case-a', FILES)
    assert [row['title'] for row in client.get('/cases/case-a/originals', headers=ADMIN).json()['originals']] == [
        row['title'] for row in kept]

    preview = build_packet(client, 'case-a', checklist['id'], preview=True, purpose='Follow-up')
    assert preview.status_code == 202, preview.text
    body = preview.json()
    assert body['as_of'] == '2026-10-07' and body['fax_id'] is None and body['suggestions'] == []
    assert [(pick['item'], pick['title']) for pick in body['selected']] == [
        (0, 'Discharge summary 20 Sep'), (1, 'Medication list'), (3, 'Insurance card')]
    assert body['selected'][0]['reason'].startswith("Matches 'Discharge summary': version 'final'")
    assert [(entry['type'], entry['required']) for entry in body['missing']] == [('Lab results', False)]
    assert body['required_not_selected'] == [] and body['packet']['pages'] == 5
    # The same originals added to another case in another order build the same packet.
    add_originals(client, 'case-b', list(reversed(FILES)))
    other = build_packet(client, 'case-b', checklist['id'], preview=True, purpose='Follow-up').json()
    assert [(pick['item'], pick['title'], pick['reason']) for pick in other['selected']] == [
        (pick['item'], pick['title'], pick['reason']) for pick in body['selected']]

    # Sending needs the confirmed selection; dropping a required item needs the person to say so.
    assert build_packet(client, 'case-a', checklist['id']).status_code == 400
    confirmed = [{'original_id': pick['original_id'], 'item': pick['item']} for pick in body['selected']]
    short = build_packet(client, 'case-a', checklist['id'], selection=confirmed[:2])
    assert short.status_code == 409 and "'Insurance card'" in short.json()['detail']
    foreign = build_packet(client, 'case-a', checklist['id'],
                           selection=[{'original_id': other['selected'][0]['original_id'], 'item': 0}])
    assert foreign.status_code == 400
    sent = build_packet(client, 'case-a', checklist['id'], selection=confirmed, purpose='Follow-up')
    assert sent.status_code == 202, sent.text
    job = sent.json()['fax_id']
    assert job and sent.json()['packet']['pages'] == 5
    engine = main.app.state.configuration_runtime.manager.store.engine
    with engine.connect() as connection:
        row = connection.execute(sa.text('SELECT kind, checklist_id, purpose FROM case_packets WHERE id = :id'),
                                 {'id': job}).one()
    assert tuple(row) == ('checklist', checklist['id'], 'Follow-up')
    held = client.get('/cases/case-a/documents', headers=ADMIN, params={'to': TO}).json()['documents']
    assert [(item['title'], item['version'], item['purpose'], item['state']) for item in held] == [
        ('Discharge summary 20 Sep', 'final', 'Follow-up', 'waiting'), ('Medication list', '', 'Follow-up', 'waiting'),
        ('Insurance card', '', 'Follow-up', 'waiting')]
    # Allowed to go without a required item when the person says the recipient agreed.
    partial = build_packet(client, 'case-b', checklist['id'], selection=[
        {'original_id': other['selected'][0]['original_id'], 'item': 0}], allow_missing=True)
    assert partial.status_code == 202 and partial.json()['required_not_selected'] == [
        {'item': 1, 'type': 'Medication list'}, {'item': 3, 'type': 'Insurance card'}]

    # A used checklist is never changed: saving again makes version 2, and version 1 still says it was used.
    changed = client.post('/case-checklists', headers=ADMIN, json={'name': 'discharge FOLLOW-UP', 'items': [
        {'type': 'Discharge summary', 'version': 'final', 'within_days': 14}]}).json()
    assert changed['version'] == 2 and changed['name'] == 'Discharge follow-up'
    shown = client.get('/case-checklists/Discharge follow-up', headers=ADMIN, params={'version': 1}).json()
    assert shown['items'] == listed['example']['items'] and shown['used'] == 2
    assert [(item['version'], item['used']) for item in shown['versions']] == [(2, 0), (1, 2)]
    assert client.get('/case-checklists/Nothing', headers=ADMIN).status_code == 404


@pytest.fixture
def suggesting(isolated_installation, monkeypatch):
    """An installation whose owner turned suggestions on."""
    monkeypatch.setenv('CASE_SUGGESTIONS', 'true')
    for name, value in {'REQUIRE_API_KEY': 'true', 'API_KEY': BOOTSTRAP, 'PUBLIC_API_URL': 'https://testserver',
                        'FAX_BACKEND': 'phaxio', 'MAX_REQUESTS_PER_MINUTE': '0'}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv('FAXBOT_CONSOLE_ORIGINS', 'https://testserver')
    with TestClient(main.app, base_url='https://testserver', headers={'Origin': 'https://testserver'}) as client:
        yield client


def test_suggestions_are_off_by_default(client):
    checklist = client.post('/case-checklists', headers=ADMIN, json={
        'name': 'Labs only', 'items': [{'type': 'Lab results'}]}).json()
    add_originals(client, 'case-s', FILES)
    off = build_packet(client, 'case-s', checklist['id'], preview=True).json()
    # 'Lab results Mesa' plainly matches by title, yet nothing is suggested while the setting is off.
    assert off['suggestions'] == [] and off['suggestions_enabled'] is False and off['selected'] == []


def test_suggestions_when_turned_on_are_never_selected_until_a_person_adds_them(suggesting):
    client = suggesting
    assert client.get('/admin/settings', headers=ADMIN).json()['cases'] == {'suggestions': True}
    checklist = client.post('/case-checklists', headers=ADMIN, json={
        'name': 'Labs only', 'items': [{'type': 'Lab results'}]}).json()
    add_originals(client, 'case-s', FILES)
    on = build_packet(client, 'case-s', checklist['id'], preview=True).json()
    assert on['suggestions_enabled'] is True
    assert [entry['title'] for entry in on['suggestions']] == ['Lab results Mesa']
    assert on['selected'] == [] and on['packet'] is None
    assert build_packet(client, 'case-s', checklist['id'], selection=[]).status_code == 409
    # A person adding the suggestion is what makes it part of the packet.
    added = build_packet(client, 'case-s', checklist['id'], preview=True, selection=[
        {'original_id': on['suggestions'][0]['original_id'], 'item': 0}]).json()
    assert [(pick['title'], pick['reason']) for pick in added['selected']] == [('Lab results Mesa', 'Added by a person.')]
    assert added['required_not_selected'] == [] and added['packet']['pages'] == 2


def test_checklist_permissions(client):
    reader = scoped_key(client, ['fax:read'])
    assert client.get('/case-checklists', headers=reader).status_code == 403
    assert client.post('/case-checklists', headers=reader,
                       json={'name': 'X', 'items': [{'type': 'Referral'}]}).status_code == 403
    assert client.post('/cases/case-a/checklist-packets', headers=reader,
                       json={'to': TO, 'checklist_id': 'x', 'preview': True}).status_code == 403
    assert client.post('/cases/case-a/originals', headers=reader,
                       files=[('documents', ('a.pdf', pdf('a'), 'application/pdf'))]).status_code == 403
    assert client.post('/cases/case-a/checklist-packets', headers=ADMIN,
                       json={'to': TO, 'checklist_id': 'missing', 'preview': True}).status_code == 404
    bad_date = client.post('/cases/case-a/originals', headers=ADMIN, data={'dates': ['30/09/2026']},
                           files=[('documents', ('a.pdf', pdf('a'), 'application/pdf'))])
    assert bad_date.status_code == 400 and 'year-month-day' in bad_date.json()['detail']


def test_suggestions_are_a_setting_administrators_may_change(client):
    """Suggestions send nothing anywhere and nothing is sent unless a person adds it, so it is an ordinary setting."""
    from app.access.configuration import owner_only_fields
    assert 'case_suggestions_enabled' not in owner_only_fields()
    current = client.get('/admin/settings', headers=ADMIN).json()
    assert 'case_suggestions_enabled' not in current['owner_only']
    changed = client.put('/admin/settings', headers=ADMIN, json={
        'case_suggestions_enabled': True, 'expected_revision_id': current['_meta']['desired_revision_id']})
    assert changed.status_code == 200, changed.text
    assert client.get('/admin/settings', headers=ADMIN).json()['cases'] == {'suggestions': True}
