"""The California court send profile (N26, migration 0083): filing with MC-005 and serving by fax under California
Rules of Court 2.304-2.306, read 2026-10-10.

The HTTP tests run the real POST /fax path of an installation with a SIP trunk and sending turned off (as
test_header_notice does), so nothing is dialed. Every case, party, court number and document is synthetic. Not yet
run against a real court or a real served party.
"""
from datetime import date, datetime
import io
import json
from types import SimpleNamespace

import pytest
from pypdf import PdfReader
import sqlalchemy as sa

from api.app import schema, schema_court_profile
from api.app.court import acceptance, cover, record, rules
from api.app.routing import envelope as envelopes
from api.tests.test_digital_schema import _downgrade, _table, _tables
from api.tests.test_header_notice import NOTICE, TO, _send, _set, document
from api.tests.test_reply_number import client  # noqa: F401 - fixture
from api.tests.test_rules_delivery import BASE, accept, installation, publish, rule
from api.tests.test_schema import database  # noqa: F401 - fixture

FILING = {
    'purpose': 'file', 'county': 'Modoc', 'case_number': 'CV-2026-0042', 'plaintiff': 'Synthetic Ranch LLC',
    'defendant': 'Example Feed Co.', 'documents': [{'title': 'Motion to Compel', 'pages': 2}],
    'sender': ['Ada Example (SBN 123456)', 'Example Law Group', '100 Main Street', 'Alturas, CA 96101'],
    'telephone': '(530) 555-0100', 'attorney_for': 'Plaintiff', 'filing_fee': True, 'fee_account': 'ACCT-0042'}
SERVICE = {
    'purpose': 'serve', 'county': 'Modoc', 'case_number': 'CV-2026-0042', 'plaintiff': 'Synthetic Ranch LLC',
    'defendant': 'Example Feed Co.', 'documents': [{'title': 'Notice of Motion', 'pages': 2}],
    'served_party': 'Counsel for Example Feed Co.', 'agreement_confirmed': True, 'number_confirmed': True,
    'fax': '(530) 555-0101'}


def utc(year, month, day, hour, minute=0):
    """A UTC moment from California (Pacific daylight time in these tests) wall-clock values."""
    from zoneinfo import ZoneInfo
    from datetime import timezone
    return datetime(year, month, day, hour, minute, tzinfo=ZoneInfo(rules.ZONE)).astimezone(timezone.utc).replace(
        tzinfo=None)


# The rules applied to dates ---------------------------------------------------------------------------------------

def test_service_after_five_is_deemed_on_the_next_court_day_counting_published_holidays():
    # Friday 25 September 2026 is Native American Day; Thursday 24 September at 5:30 p.m. moves to Monday 28.
    served = rules.service(utc(2026, 9, 24, 17, 30))
    assert served.moved and served.served_on == date(2026, 9, 28)
    assert 'deemed to occur on the next court day, Monday 28 September 2026 (rule 2.306(g))' in served.sentences[0]
    assert 'extended by two court days (rule 2.306(d))' in served.sentences[-1]
    assert 'a notice of appeal (rule 2.306(e))' in served.sentences[-1]
    on_time = rules.service(utc(2026, 9, 24, 16, 59))
    assert not on_time.moved and on_time.served_on == date(2026, 9, 24)
    # Exactly 5:00 p.m. is not after 5 p.m.
    assert not rules.service(utc(2026, 9, 24, 17, 0)).moved
    # The rule moves only service after 5 p.m.: a Saturday morning transmission says so instead of guessing.
    saturday = rules.service(utc(2026, 10, 10, 10, 0))
    assert saturday.served_on == date(2026, 10, 10)
    assert 'is not a court day; rule 2.306(g) moves only service completed after 5 p.m.' in saturday.sentences[1]


def test_a_deadline_is_extended_by_two_court_days_and_an_unpublished_year_is_never_guessed():
    moved, sentence = rules.extended(date(2026, 11, 25))  # Thanksgiving and the day after are court holidays
    assert moved == date(2026, 12, 1)
    assert sentence.startswith('A deadline of Wednesday 25 November 2026 is extended by two court days to Tuesday 1 '
                               'December 2026')
    with pytest.raises(rules.CourtCalendarUnknown, match="haven't published their 2028 holidays"):
        rules.extended(date(2027, 12, 31))
    assert rules.holiday(date(2027, 4, 9)) == 'Farmworkers Day (formerly Cesar Chavez Day)'


def test_the_court_list_is_sourced_and_a_local_cut_off_is_applied():
    counts = {status: sum(1 for item in rules.COURTS if item.status == status)
              for status in (rules.ACCEPTS, rules.STOPPED, rules.REFUSES)}
    assert counts == {'accepts': 11, 'stopped': 8, 'does_not_accept': 13}
    assert len(rules.COURTS) + len(rules.NOT_ESTABLISHED) == 58
    assert all(item.source.startswith('https://') for item in rules.COURTS)
    assert rules.court('mono county').status == rules.STOPPED
    filed, sentence = rules.filed_on('Modoc', utc(2026, 9, 24, 17, 10))
    assert filed == date(2026, 9, 28) and "Modoc's local rule files faxes received after 5:00 p.m." in sentence
    assert rules.filed_on('Modoc', utc(2026, 9, 24, 15, 0))[0] == date(2026, 9, 24)
    assert rules.filed_on('Mariposa', utc(2026, 9, 24, 18, 0)) is None  # no published cut-off


# The case details and the cover sheet -----------------------------------------------------------------------------

def test_the_details_are_checked_and_a_payment_card_is_never_taken():
    with pytest.raises(cover.DetailsError, match='never takes a payment card'):
        cover.parse({**FILING, 'card_number': '4111111111111111'})
    with pytest.raises(cover.DetailsError, match='court account number'):
        cover.parse({**FILING, 'fee_account': ''})
    with pytest.raises(cover.DetailsError, match='Choose file'):
        cover.parse({**FILING, 'purpose': 'deliver'})
    with pytest.raises(cover.DetailsError, match=r'agreement, confirmed in writing \(rule 2.306\(a\)\(1\)\)'):
        cover.parse({**SERVICE, 'agreement_confirmed': False})
    with pytest.raises(cover.DetailsError, match=r'last gave on a filed and served document'):
        cover.parse({**SERVICE, 'number_confirmed': False})
    with pytest.raises(cover.DetailsError, match='at most 15 documents'):
        cover.parse({**FILING, 'documents': [{'title': f'Exhibit {n}', 'pages': 1} for n in range(16)]})
    assert cover.parse(json.dumps(FILING)).title == 'Synthetic Ranch LLC v. Example Feed Co.'
    assert 'card' not in json.dumps(cover.parse(FILING).public())


def test_the_cover_is_the_official_mc005_filled_and_flattened():
    page = cover.fill(cover.parse(FILING))
    reader = PdfReader(io.BytesIO(page))
    assert len(reader.pages) == 1 and not reader.get_fields()
    assert not [annotation for annotation in reader.pages[0].get('/Annots') or []
                if annotation.get_object().get('/Subtype') == '/Widget']
    text = ' '.join((reader.pages[0].extract_text() or '').split())
    for expected in ('FACSIMILE TRANSMISSION COVER SHEET', 'MC-005 [Rev. January 1, 2007]', 'CV-2026-0042',
                     'Synthetic Ranch LLC', 'Motion to Compel', 'ACCT-0042', 'Modoc'):
        assert expected.replace('MC-005', 'MC') in text or expected in text, expected
    assert 'Clear This Form' not in text  # the form's on-screen buttons and notice are left off


# POST /fax ------------------------------------------------------------------------------------------------------

def _engine():
    from app.main import app
    return app.state.configuration_runtime.manager.store.engine


def test_a_filing_goes_with_mc005_first_exactly_as_given_by_a_fax_call(client):  # noqa: F811
    _set(client, '/header-notice', {'notice': NOTICE})
    data = document('MOTION TO COMPEL By fax', 'Page two')
    response = _send(client, data, court=json.dumps(FILING))
    assert response.status_code == 202, response.text
    job = response.json()['id']
    assert response.json()['pages'] == 3
    engine = _engine()
    with engine.connect() as connection:
        row = connection.execute(sa.text('SELECT purpose, source, by_fax, cover_pages, original_pages, sent_pages '
                                         'FROM court_sends WHERE id = :job'), {'job': job}).mappings().one()
        noticed = connection.execute(sa.text('SELECT count(*) FROM fax_header_notices WHERE id = :job'), {'job': job}).scalar()
    assert dict(row) == {'purpose': 'file', 'source': 'sender', 'by_fax': 'found', 'cover_pages': 1,
                         'original_pages': 2, 'sent_pages': 3}
    assert noticed == 0  # no header notice on a court fax
    import os
    from pathlib import Path
    sent = PdfReader(str(Path(os.environ['FAX_DATA_DIR']) / f'{job}.pdf'))
    assert len(sent.pages) == 3 and 'FACSIMILE TRANSMISSION COVER SHEET' in (sent.pages[0].extract_text() or '')
    assert 'MOTION TO COMPEL' in (sent.pages[1].extract_text() or '')
    pinned = envelopes.load(engine, job).envelope
    assert (pinned.local, pinned.direct, pinned.alternate, pinned.dial, pinned.page_layout) == (
        False, False, 'never', None, 'one_per_sheet')
    assert pinned.mode != 'automatic'
    view = client.get(f'/court/faxes/{job}', headers=_B()).json()['court']
    assert view['purpose'] == 'file' and view['sentences'][0] == record.PAGES_AS_GIVEN
    assert 'Faxbot found it' in view['sentences'][2]
    pdf = client.get(f'/court/faxes/{job}/record', headers=_B())
    assert pdf.status_code == 200 and pdf.headers['content-type'] == 'application/pdf'
    text = ' '.join(page.extract_text() or '' for page in PdfReader(io.BytesIO(pdf.content)).pages)
    assert 'Transmission record' in text and 'Draft proof of transmission (rule 2.304(d))' in text
    assert 'penalty of perjury' in text


def _B():
    from api.tests.test_access_management_http import B
    return B


def test_by_fax_must_be_below_each_title_or_confirmed_for_a_scan(client):  # noqa: F811
    missing = _send(client, document('MOTION TO COMPEL', 'Page two'), court=json.dumps(FILING))
    assert missing.status_code == 400
    assert missing.json()['detail'] == ('Add "By fax" immediately below the title of "Motion to Compel" (California '
                                        'Rules of Court, rule 2.304(c)), then send it again.')
    counted = _send(client, document('MOTION By fax'), court=json.dumps(FILING))
    assert counted.status_code == 400 and 'add up to 2' in counted.json()['detail']
    service = _send(client, document('NOTICE OF MOTION', 'Page two'), court=json.dumps(SERVICE))
    assert service.status_code == 202, service.text
    assert service.json()['pages'] == 2  # service gets no MC-005; "By fax" is a filing rule
    view = client.get(f"/court/faxes/{service.json()['id']}", headers=_B()).json()['court']
    assert view['purpose_text'] == 'Service on a party by fax (rule 2.306)'


def test_a_mailbox_or_rule_under_the_profile_needs_the_case_details(client):  # noqa: F811
    from api.tests.test_work_http import mailbox
    from api.tests.test_reply_number import DID_B
    box = mailbox(client, 'Litigation', DID_B)
    attached = client.put('/court/profile/attachments', headers=_B(),
                          json={'kind': 'mailbox', 'target': box['id'], 'on': True})
    assert attached.status_code == 200, attached.text
    assert attached.json()['attached'][0]['sentence'] == ('Faxes from the Litigation mailbox go under the California '
                                                         'court profile.')
    refused = _send(client, document('Letter'), mailbox=box['id'])
    assert refused.status_code == 400 and refused.json()['detail'] == acceptance.NEEDS_DETAILS
    accepted = _send(client, document('MOTION By fax', 'Two'), mailbox=box['id'], court=json.dumps(FILING))
    assert accepted.status_code == 202, accepted.text
    with _engine().connect() as connection:
        source = connection.execute(sa.text('SELECT source FROM court_sends WHERE id = :job'),
                                    {'job': accepted.json()['id']}).scalar()
    assert source == 'mailbox'
    unknown = client.put('/court/profile/attachments', headers=_B(), json={'kind': 'rule', 'target': 'r-x', 'on': True})
    assert unknown.status_code == 400 and unknown.json()['detail'] == 'Choose one of your routing rules.'
    profile = client.get('/court/profile', headers=_B()).json()
    assert profile['trend'].startswith('California courts are leaving fax')
    assert len(profile['courts']) == 32 and profile['holidays']['years'] == [2026, 2027]


def test_a_rule_that_allows_only_direct_delivery_blocks_a_court_fax(database, tmp_path):  # noqa: F811
    env = installation(database, tmp_path, BASE)
    automatic = envelopes.load(database, accept(env, court=True))
    assert automatic.envelope.mode == 'cheapest' and automatic.envelope.direct is False
    publish(env, {'format': 1, 'limits': [rule('l-direct', {'require_direct': True})]})
    blocked = envelopes.load(database, accept(env, court=True))
    assert blocked.decision.outcome == 'blocked' and blocked.decision.reason == 'no_allowed_account'


def test_an_attempt_sends_a_court_fax_exactly_as_accepted(client):  # noqa: F811
    from api.app.pages import sending
    job = _send(client, document('MOTION By fax', 'Two'), court=json.dumps(FILING)).json()['id']
    other = _send(client, document('Letter')).json()['id']
    from app.main import app
    values = app.state.configuration_runtime.manager.store.read().active.values
    import os
    from pathlib import Path
    root = Path(os.environ['FAX_DATA_DIR'])
    account = SimpleNamespace(provider_id='sip', mode='image', card=None)
    for fax in (job,):
        claim = SimpleNamespace(job_id=fax, attempt_id='a' * 32, members=None)
        found = sending.evaluate(_engine(), values, account, claim, {'id': fax, 'to_number': TO},
                                 root / f'{fax}.pdf', root / f'{fax}.tiff')
        if fax == job:
            assert found is None  # nothing packed, shaded, encoded, trimmed or kept at another resolution
    from api.app.court.store import send_for
    assert send_for(_engine(), job) is not None and send_for(_engine(), other) is None


# The migration -----------------------------------------------------------------------------------------------------

def test_0083_is_the_head_and_its_downgrade_keeps_court_rows(database):  # noqa: F811
    schema.upgrade_schema(database)
    assert schema_court_profile.REVISION == '0083_court_profile' == schema.COURT_PROFILE
    assert schema.PARTNER_LAYOUTS == '0082_partner_layouts'
    assert schema_court_profile.TABLES <= schema.STRICT_TABLES and schema_court_profile.TABLES <= _tables(database)
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD
    with pytest.raises(sa.exc.IntegrityError):
        with database.begin() as connection:
            connection.execute(_table(database, 'court_profile_attachments').insert().values(
                id='a1', kind='printer', target='x', state='active', created_at=datetime(2026, 10, 10)))
    _downgrade(database, '0082_partner_layouts')
    assert not schema_court_profile.TABLES & _tables(database)
    schema.upgrade_schema(database)
    from api.app.court import store
    store.set_attached(database, 'mailbox', 'm1', True, actor={'name': 'Ada'})
    with pytest.raises(schema.SchemaUpgradeError, match='cannot be undone'):
        _downgrade(database, '0082_partner_layouts')


def test_the_command_line_attaches_lists_courts_and_extends_a_deadline(monkeypatch, tmp_path):
    import httpx
    from typer.testing import CliRunner
    from app.cli.main import app
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('FAXBOT_CLI_CONFIG', str(tmp_path / 'absent-config.toml'))
    profile = {'attached': [], 'summary': 'Faxes under this profile go exactly as given.', 'trend': rules.TREND,
               'rules': [], 'holidays': {'years': [2026, 2027], 'source': rules.HOLIDAYS_SOURCE}, 'courts': [
                   {'county': 'Modoc', 'status': 'accepts', 'note': 'Civil.', 'fax': '(530) 233-6500',
                    'cutoff': '17:00', 'source': 'https://www.modoc.courts.ca.gov/', 'source_date': None,
                    'read_on': rules.READ_ON}], 'not_established': ['Fresno'],
               'mailboxes': [{'id': 'm' * 32, 'label': 'Litigation'}], 'routing_rules': []}
    sent = []

    def handle(request):
        if request.url.path == '/court/profile':
            return httpx.Response(200, json=profile)
        sent.append((request.method, request.url.path, json.loads(request.content or b'{}')))
        if request.url.path == '/court/deadline':
            return httpx.Response(200, json={'deadline': '2026-11-25', 'extended': '2026-12-01',
                                             'sentence': 'A deadline of Wednesday 25 November 2026 is extended.'})
        return httpx.Response(200, json={**profile, 'changed': True, 'attached': [
            {'kind': 'mailbox', 'target': 'm' * 32, 'name': 'Litigation',
             'sentence': 'Faxes from the Litigation mailbox go under the California court profile.'}]})

    def run(*args):
        with httpx.Client(transport=httpx.MockTransport(handle), base_url='https://faxbot.example') as http:
            return CliRunner().invoke(app, ['--url', 'https://faxbot.example', '--key', 'synthetic', 'delivery',
                                            'rules', 'court', *args], obj={'client_factory': lambda *_: (http, False)})
    attached = run('attach', 'mailbox', 'Litigation')
    assert attached.exit_code == 0, attached.output
    assert sent[-1] == ('PUT', '/court/profile/attachments', {'kind': 'mailbox', 'target': 'm' * 32, 'on': True})
    assert 'Litigation mailbox go under the California court profile' in ' '.join(attached.output.split())
    courts = run('courts')
    assert courts.exit_code == 0 and 'Modoc' in courts.output and 'Fresno' in courts.output
    extended = run('deadline', '2026-11-25')
    assert extended.exit_code == 0 and sent[-1][2] == {'deadline': '2026-11-25'}
    assert run('deadline', '25/11/2026').exit_code != 0
