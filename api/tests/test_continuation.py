"""Sending only the pages a broken call did not confirm (T15): the evidence, the offer, the pages and the sending.

Synthetic faxes on the real HTTPS application. What the call confirmed is put
in the records the engines and services write (the call record, the call's
T.30 frames, the page reports); nothing here dials or calls a provider.
"""
import base64
from datetime import datetime, timedelta
import io
import json
import re

import httpx
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from app import main
from app.routing import continuation
from app.routing.continuation import (builtin_confirmed, continuation_id, continuation_pdf, cost_sentence,
                                      hylafax_confirmed, provider_confirmed)
from app.routing.pricing import Price
from app.work.certainty import CertaintyStore, CertaintyWorker, PartnerQuestion
from api.tests.test_hylafax_scripts import engine as script_engine, run  # noqa: F401 - fixture
from api.tests.test_outbound_store import installation  # noqa: F401 - fixture
from api.tests.test_schema import database  # noqa: F401 - fixture


BOOTSTRAP = 'synthetic-continuation-bootstrap'
ADMIN = {'X-API-Key': BOOTSTRAP}
NUMBER = '+12025550123'
DCS_ECM = 'ff138300200004'      # T.30 DCS, FIF bit 27 (error correction) set
DCS_PLAIN = 'ff138300200000'    # the same without error correction
SENT_AT = datetime(2026, 10, 7, 15, 41)


def synthetic_document(pages):
    """A PDF whose page N says 'Original page N'."""
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
    output = io.BytesIO()
    pdf = canvas.Canvas(output, pagesize=letter)
    for number in range(1, pages + 1):
        pdf.setFont('Helvetica', 14)
        pdf.drawString(72, 700, f'Original page {number}')
        pdf.showPage()
    pdf.save()
    return output.getvalue()


def page_texts(document):
    from pypdf import PdfReader
    return [page.extract_text() or '' for page in PdfReader(io.BytesIO(document)).pages]


# -- What the call record proves ------------------------------------------------------------------------

def test_the_built_in_engine_counts_confirmed_pages_and_without_error_correction_only_a_call_that_never_retrained():
    with_ecm = builtin_confirmed(7, ecm=True, trainings=3)
    assert (with_ecm.pages, with_ecm.source) == (7, 'builtin')
    assert with_ecm.sentence == 'The call used error correction, and the receiving machine confirmed the first 7 ' \
                                'pages whole.'
    # Without error correction an RTP page (kept, damaged lines) counts in FAXPAGES; every RTP or RTN brings a new
    # training, so only a call that trained once had no damaged page.
    assert builtin_confirmed(7, ecm=False, trainings=1).pages == 7
    retrained = builtin_confirmed(7, ecm=False, trainings=2)
    assert retrained.pages is None and 'cannot tell' in retrained.sentence
    assert builtin_confirmed(7, ecm=None, trainings=1).pages is None
    assert builtin_confirmed(None, ecm=True, trainings=1).pages is None
    assert continuation.dcs_ecm(DCS_ECM) is True and continuation.dcs_ecm(DCS_PLAIN) is False
    assert continuation.dcs_ecm(None) is None


def test_the_ssl_fax_engine_counts_only_clean_pages_without_error_correction():
    assert hylafax_confirmed(9, ecm='on', clean_pages=None, flagged_page=None).pages == 9
    # Retransmit-Ignore counted page 7 after its third RTN: only the six clean pages stand.
    flagged = hylafax_confirmed(9, ecm='off', clean_pages=6, flagged_page=7)
    assert flagged.pages == 6
    assert flagged.sentence == 'The receiving machine reported damaged lines on page 7, so only the first 6 pages ' \
                               'count as confirmed.'
    assert hylafax_confirmed(9, ecm='off', clean_pages=12, flagged_page=None).pages == 9
    # Error correction on some pages only: its answers count blocks, not pages, so nothing is claimed.
    assert hylafax_confirmed(9, ecm='mixed', clean_pages=4, flagged_page=None).pages is None
    unknown = hylafax_confirmed(9, ecm='off', clean_pages=None, flagged_page=None)
    assert unknown.pages is None and unknown.source == 'hylafax'
    assert hylafax_confirmed(9, ecm=None, clean_pages=None, flagged_page=None).pages is None


def test_cloud_services_count_only_where_they_report_pages_sent():
    sinch = provider_confirmed('sinch', {'pages_sent': 7, 'total_pages': 20})
    assert (sinch.pages, sinch.total, sinch.sentence) == (7, 20, 'Sinch reported 7 of 20 pages sent successfully.')
    assert provider_confirmed('documo', {'pages_sent': 5, 'total_pages': 9}).sentence == \
        'Documo reported 5 of 9 pages completed.'
    assert provider_confirmed('humblefax', {'pages_sent': 3, 'total_pages': None}).pages == 3
    assert provider_confirmed('sinch', None).pages is None
    for name, label in (('phaxio', 'Phaxio'), ('signalwire', 'SignalWire')):
        found = provider_confirmed(name, {'pages_sent': 7})
        assert found.pages is None
        assert found.sentence == f'{label} does not report how many pages it sent before a fax failed, so Faxbot ' \
                                 'cannot tell which pages arrived.'


def test_each_service_report_is_read_from_its_own_documented_fields():
    assert continuation.sinch_pages({'data': {'pagesSentSuccessfully': 7, 'numberOfPages': 20}}) == (7, 20)
    assert continuation.documo_pages({'pagesComplete': '5', 'pagesCount': 9}) == (5, 9)
    fax = {'recipients': [{'attempts': [{'numPagesSent': 2}, {'numPagesSent': 4}]}]}
    assert continuation.humblefax_pages(fax) == (4, None)  # each attempt starts at page 1: never added up
    assert continuation.humblefax_pages({'recipients': [{'attempts': [{'numPagesSent': 2}, {}]}]}) == (None, None)
    assert continuation.provider_report('phaxio', {'num_pages': 20}) == (None, None)


def test_the_session_log_reports_the_clean_pages_before_the_first_damaged_one(script_engine):  # noqa: F811
    spool, _, _, environment = script_engine
    path = spool / 'log' / 'c000000031'
    lines = ['Oct 08 10:00:00.00: [  200]: SESSION BEGIN 000000031 15555550199',
             'Oct 08 10:00:02.00: [  200]: SEND training at v.17 14400 bit/s',
             'Oct 08 10:00:03.00: [  200]: TRAINING succeeded',
             'Oct 08 10:00:03.10: [  200]: USE 14400 bit/s']
    answers = ['MCF (message confirmation)', 'MCF (message confirmation)', 'RTN (retrain negative)',
               'MCF (message confirmation)']
    for answer in answers:
        lines += ['Oct 08 10:00:04.00: [  200]: SEND begin page', 'Oct 08 10:00:14.00: [  200]: SEND end page',
                  'Oct 08 10:00:14.10: [  200]: SEND send MPS (more pages, same document)',
                  f'Oct 08 10:00:15.00: [  200]: SEND recv {answer}']
    path.write_text('\n'.join(lines + ['Oct 08 10:01:00.00: [  200]: SESSION END']) + '\n')
    result = run('negotiation', environment, str(path))
    assert result.returncode == 0, result.stderr
    report = json.loads(base64.b64decode(result.stdout))
    assert (report['clean_pages'], report['flagged_page']) == (2, 3)
    assert continuation.engine_report(result.stdout) == (2, 3)
    # A log with no page answer reports nothing, never zero.
    path.write_text('\n'.join(lines[:3] + ['Oct 08 10:01:00.00: [  200]: SESSION END']) + '\n')
    report = run('negotiation', environment, str(path)).stdout
    assert 'clean_pages' not in json.loads(base64.b64decode(report))
    assert continuation.engine_report(report) == (None, None)


# -- The pages ------------------------------------------------------------------------------------------

def test_the_continuation_holds_exactly_the_remaining_pages_and_the_note_on_the_first():
    note = continuation.note_text(SENT_AT, 20, 8, zone_name='America/Denver')
    assert note == ('Continuation of our fax of 7 October 2026 at 9:41 AM MDT, 20 pages: pages 8–20. '
                    'Pages 1–7 arrived on the first call.')
    document = continuation_pdf(synthetic_document(20), first_page=8, last_page=20, note=note)
    texts = page_texts(document)
    assert len(texts) == 13
    assert 'Original page 8' in texts[0] and 'Continuation of our fax of 7 October 2026' in texts[0]
    assert 'Pages 1' in texts[0] and 'arrived on the first call.' in texts[0]
    for offset, text in enumerate(texts[1:], start=9):
        assert f'Original page {offset}' in text and 'Continuation' not in text
    assert not any('Original page 7' in text for text in texts)
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(document))
    original = PdfReader(io.BytesIO(synthetic_document(20)))
    # The first page keeps its size; the note sits in a band above the page's own content, never over it.
    assert reader.pages[0].mediabox == original.pages[7].mediabox
    assert reader.metadata['/Subject'] == note
    # One page left: "page 20", "Page 1 arrived" for a call that confirmed one page.
    assert continuation.note_text(SENT_AT, 2, 2, zone_name='UTC').endswith(
        '2 pages: page 2. Page 1 arrived on the first call.')


def test_the_cost_line_compares_the_remaining_pages_with_the_whole_fax_and_unknown_stays_unknown():
    def money(micros):
        return Price('phaxio', micros, 'USD')
    assert cost_sentence(money(20_000), money(50_000)) == 'About $0.02, against $0.05 for the whole fax.'
    assert cost_sentence(money(None), money(50_000)) == 'Faxbot cannot price these pages, so their cost is unknown.'
    assert cost_sentence(money(20_000), money(None)) == 'About $0.02; the whole fax cannot be priced.'
    plan = Price('humblefax', 0, 'USD', in_plan=True)
    assert cost_sentence(plan, plan) == 'In your plan, as the whole fax would be.'


# -- Kept when the result arrives ---------------------------------------------------------------------------

def test_documo_and_humblefax_status_answers_carry_their_page_counts():
    from app import documo_service, humblefax_service
    identity = '0f0e0d0c-0b0a-4908-8706-050403020100'
    receipt = documo_service._receipt(httpx.Response(200, json={
        'messageId': identity, 'status': 'failed', 'resultCode': '7000', 'pagesComplete': 7, 'pagesCount': 20}),
        requested_sid=identity)
    assert (receipt['status'], receipt['pages_sent'], receipt['pages_total']) == ('failed', 7, 20)
    fax = {'id': 4711, 'status': 'failure', 'recipients': [
        {'failureReason': 'Communication error', 'attempts': [{'numPagesSent': 3, 'failureReason': 'x'}]}]}
    answer = humblefax_service._receipt(httpx.Response(200, json={'data': {'sentFax': fax}}), requested_sid='4711')
    assert (answer['status'], answer['pages_sent']) == ('failed', 3)
    # A service that says nothing about pages adds nothing to its answer.
    quiet = documo_service._receipt(httpx.Response(200, json={'messageId': identity, 'status': 'failed',
                                                               'resultCode': '7000'}), requested_sid=identity)
    assert 'pages_sent' not in quiet


@pytest.mark.asyncio
async def test_a_polled_failure_keeps_the_services_page_count_once(installation, monkeypatch):  # noqa: F811
    from api.tests.test_outbound_polling import issued, with_profile
    from api.app.config_profiles import ProviderConfiguration
    from api.app.outbound_polling import OutboundPoller
    documo = ProviderConfiguration('documo', credentials={'api_key': 'synthetic-key'},
                                   settings={'base_url': 'https://original.invalid', 'sandbox': False})
    installation = with_profile(installation, documo)
    configuration, store, _ = installation
    job, claim = issued(installation)

    class Service:
        async def get_fax_status(self, sid):
            return {'provider_sid': sid, 'status': 'failed', 'before_fax_data': False, 'pages_sent': 2,
                    'pages_total': 3}
    monkeypatch.setattr('api.app.outbound_polling.service_from_profile', lambda profile: Service())
    await OutboundPoller(store).refresh(job)
    await OutboundPoller(store).refresh(job)
    with configuration.engine.connect() as connection:
        rows = connection.execute(sa.text('SELECT source, pages_sent, total_pages, attempt_id FROM fax_page_reports '
                                          'WHERE job_id = :job'), {'job': job}).all()
    assert [tuple(row) for row in rows] == [('documo', 2, 3, claim.attempt_id)]


# -- Over the real application ------------------------------------------------------------------------------

@pytest.fixture
def client(isolated_installation, monkeypatch):
    for name, value in {'REQUIRE_API_KEY': 'true', 'API_KEY': BOOTSTRAP, 'PUBLIC_API_URL': 'https://testserver',
                        'FAX_BACKEND': 'phaxio', 'MAX_REQUESTS_PER_MINUTE': '0'}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv('FAXBOT_CONSOLE_ORIGINS', 'https://testserver')
    monkeypatch.setattr(CertaintyWorker, 'step', lambda self, now=None: False)

    async def idle(self):
        return False
    monkeypatch.setattr(PartnerQuestion, 'step', idle)
    with TestClient(main.app, base_url='https://testserver', headers={'Origin': 'https://testserver'}) as client:
        yield client


def engine():
    return main.app.state.configuration_runtime.manager.store.engine


def data_dir():
    return main.app.state.configuration_runtime.manager.store.read().active.values.fax_data_dir


def table(name):
    return sa.Table(name, sa.MetaData(), autoload_with=engine())


def broken(client, *, headers=ADMIN, pages=20, confirmed=7, dcs=DCS_ECM, trainings=1, category='partly_sent',
           route='sip', state='failed'):
    """A fax of ``pages`` pages through POST /fax whose only call broke after ``confirmed`` confirmed pages.

    Test mode holds every fax; this one is made an ordinary fax whose call went out on the built-in engine (or
    ``route``) and failed part way, with the call record and T.30 frames that engine keeps.
    """
    sent = client.post('/fax', headers=headers, data={'to': NUMBER},
                       files={'file': ('referral.pdf', synthetic_document(pages), 'application/pdf')})
    assert sent.status_code == 202, sent.text
    job = sent.json()['id']
    attempt = 'attempt-' + job[:24]
    now = datetime.utcnow()
    with engine().begin() as connection:
        connection.execute(table('outbound_attempts').insert().values(
            id=attempt, job_id=job, sequence=1, phase='failed' if state == 'failed' else 'uncertain',
            error_category=category, created_at=now - timedelta(minutes=5), submitted_at=now - timedelta(minutes=5),
            completed_at=now))
        connection.execute(table('outbound_deliveries').update().where(sa.text('id = :job')).values(
            state=state, dispatch_mode='normal', attempt_id=attempt), {'job': job})
        connection.execute(table('fax_jobs').update().where(sa.text('id = :job')).values(
            backend=route, outbound_backend=route, status='failed' if state == 'failed' else 'queued'), {'job': job})
        connection.execute(table('sip_call_records').insert().values(
            id='call-' + job[:24], direction='outbound', call_id='call-' + job[:24], job_id=job, attempt_id=attempt,
            started_at=now - timedelta(minutes=5), answered_at=now - timedelta(minutes=5), ended_at=now,
            disposition='answered', connected_seconds=240, t38='yes', pages=confirmed, fax_status='FAILED',
            fax_preference=0, created_at=now, updated_at=now))
        connection.execute(table('fax_call_frames').insert().values(
            id=f'out:{attempt}', direction='out', job_id=job, attempt_id=attempt, dcs_last=dcs, dcs_sent=1,
            trainings=trainings, ftt=0, t38_now=0, created_at=now))
    return job, attempt


def fax_ids():
    with engine().connect() as connection:
        return set(connection.execute(sa.text('SELECT id FROM fax_jobs')).scalars())


def decisions(job_id):
    with engine().connect() as connection:
        return connection.execute(sa.text('SELECT outcome FROM fax_job_rule_decisions WHERE job_id = :job'),
                                  {'job': job_id}).scalars().all()


def test_a_broken_call_offers_only_the_unconfirmed_pages_and_a_person_sends_them_once(client):
    job, attempt = broken(client)
    found = client.get(f'/continuations/faxes/{job}', headers=ADMIN)
    assert found.status_code == 200, found.text
    offer = found.json()['offer']
    assert offer['available'] is True and offer['may_send'] is True and offer['open_item_id'] is None
    assert (offer['first_page'], offer['last_page'], offer['pages']) == (8, 20, 13)
    assert offer['action'] == 'Send pages 8–20'
    assert offer['warning'] == 'Page 8 may already have arrived, so the recipient may get it twice.'
    assert offer['basis'] == 'The call used error correction, and the receiving machine confirmed the first 7 ' \
                             'pages whole.'
    # Priced by the shared predictor on the account the rules choose (Phaxio's published per-page price).
    assert re.fullmatch(r'About \$[0-9.]+, against \$[0-9.]+ for the whole fax\.', offer['cost_text']), \
        offer['cost_text']
    assert offer['cost_account'] == 'phaxio' and offer['cost']['part']['currency'] == 'USD'
    # Looking sends nothing.
    before = fax_ids()
    assert client.get(f'/continuations/faxes/{job}', headers=ADMIN).status_code == 200 and fax_ids() == before
    stale = client.post(f'/continuations/faxes/{job}', headers=ADMIN, json={'first_page': 9})
    assert stale.status_code == 409 and fax_ids() == before
    sent = client.post(f'/continuations/faxes/{job}', headers=ADMIN, json={'first_page': 8, 'reason': 'Rest of it'})
    assert sent.status_code == 200, sent.text
    new_fax = continuation_id(attempt)
    assert fax_ids() - before == {new_fax}
    assert sent.json()['continued_by']['fax_id'] == new_fax and sent.json()['offer'] is None
    shown = client.get(f'/admin/fax-jobs/{new_fax}', headers=ADMIN)
    assert shown.status_code == 200 and shown.json()['pages'] == 13
    # The pages it carries: 8 to 20 of the original, the note on the first.
    from pathlib import Path
    texts = page_texts((Path(data_dir()) / f'{new_fax}.pdf').read_bytes())
    assert len(texts) == 13 and 'Original page 8' in texts[0] and 'Original page 20' in texts[-1]
    assert 'Continuation of our fax of' in texts[0] and 'pages 8–20' in texts[0]
    # Linked both ways in Sent.
    assert client.get(f'/continuations/faxes/{new_fax}', headers=ADMIN).json()['continues'] == {
        'fax_id': job, 'first_page': 8, 'last_page': 20, 'pages_text': 'pages 8–20'}
    again = client.post(f'/continuations/faxes/{job}', headers=ADMIN, json={'first_page': 8})
    assert again.status_code == 409 and fax_ids() - before == {new_fax}
    with engine().connect() as connection:
        link = connection.execute(sa.text('SELECT * FROM fax_continuations')).mappings().one()
    assert (link['job_id'], link['attempt_id'], link['continuation_job_id'], link['confirmed_by'],
            link['reason']) == (job, attempt, new_fax, 'builtin', 'Rest of it')
    # Through the sending rules, like any fax.
    assert decisions(new_fax)


def test_unknown_means_no_offer_only_the_whole_fax_again(client):
    plain, _ = broken(client, dcs=DCS_PLAIN, trainings=2)
    offer = client.get(f'/continuations/faxes/{plain}', headers=ADMIN).json()['offer']
    assert offer['available'] is False and 'cannot tell' in offer['reason'] and 'first_page' not in offer
    refused = client.post(f'/continuations/faxes/{plain}', headers=ADMIN, json={'first_page': 8})
    assert refused.status_code == 409
    # The built-in engine without error correction, on a call that trained once: no damaged page, so it counts.
    clean, _ = broken(client, dcs=DCS_PLAIN, trainings=1, confirmed=4, pages=6)
    assert client.get(f'/continuations/faxes/{clean}', headers=ADMIN).json()['offer']['first_page'] == 5
    # Phaxio reports no pages sent for a failed fax.
    cloud, _ = broken(client, route='phaxio')
    offer = client.get(f'/continuations/faxes/{cloud}', headers=ADMIN).json()['offer']
    assert offer['available'] is False and offer['reason'].startswith('Phaxio does not report')
    # An uncertain call: nothing is offered here; it waits for a person.
    uncertain, _ = broken(client, category='pages_unconfirmed', state='reconciliation_required')
    assert client.get(f'/continuations/faxes/{uncertain}', headers=ADMIN).json()['offer'] is None
    # Pages packed onto longer pages for the call: the count is not the document's pages.
    packed, packed_attempt = broken(client)
    with engine().begin() as connection:
        connection.execute(table('fax_page_changes').insert().values(
            id='change-1', job_id=packed, attempt_id=packed_attempt, route='sip', original_pages=20, sent_pages=8,
            layout='dense', pages_saved=12, created_at=datetime.utcnow()))
    offer = client.get(f'/continuations/faxes/{packed}', headers=ADMIN).json()['offer']
    assert offer['available'] is False and 'packed' in offer['reason']
    # A call shared with other faxes stays a person's decision about the whole call.
    shared, shared_attempt = broken(client)
    with engine().begin() as connection:
        connection.execute(table('outbound_events').insert().values(
            id='event-shared', job_id=shared, attempt_id=shared_attempt, kind='sent_together', details='{}',
            created_at=datetime.utcnow()))
    assert client.get(f'/continuations/faxes/{shared}', headers=ADMIN).json()['offer']['available'] is False


def publish_rules(client, document):
    current = client.get('/routing/rules', headers=ADMIN).json()
    saved = client.put('/routing/rules/draft', headers=ADMIN, json={
        'document': document, 'expected_version': current['draft']['version'] if current['draft'] else 0})
    assert saved.status_code == 200, saved.text
    active = current['active']['number'] if current['active'] else None
    published = client.post('/routing/rules/publish', headers=ADMIN, json={
        'expected_active_revision': active, 'expected_draft_version': saved.json()['version'], 'note': 'Synthetic'})
    assert published.status_code == 200, published.text


def test_the_continuation_goes_through_the_sending_rules_which_may_hold_it(client):
    job, attempt = broken(client)
    publish_rules(client, {'format': 1, 'limits': [{'id': 'l-all', 'name': 'Rule l-all', 'on': True, 'when': {},
                                                    'then': {'hold_for_approval': {}}}]})
    sent = client.post(f'/continuations/faxes/{job}', headers=ADMIN, json={'first_page': 8})
    assert sent.status_code == 200, sent.text
    new_fax = continuation_id(attempt)
    assert decisions(new_fax) == ['held']
    holds = {hold['job_id']: hold for hold in client.get('/routing/holds', headers=ADMIN).json()['holds']}
    assert holds[new_fax]['kind'] == 'approval'


def test_on_the_uncertain_item_it_sits_beside_send_again_and_settles_the_item(client):
    job, attempt = broken(client)
    assert CertaintyStore(engine()).feed(main.app.state.access_runtime.control) == 1
    [item] = client.get(f'/certainty/faxes/{job}', headers=ADMIN).json()['items']
    assert 'continue' in item['actions'] and item['continuation']['state'] == 'offered'
    assert item['continuation']['action'] == 'Send pages 8–20'
    assert item['continuation']['cost_text'] and NUMBER not in json.dumps(item['continuation'])
    # Sent's own action goes through the item, with the person's reason.
    offer = client.get(f'/continuations/faxes/{job}', headers=ADMIN).json()['offer']
    assert offer['open_item_id'] == item['id']
    assert client.post(f'/continuations/faxes/{job}', headers=ADMIN, json={'first_page': 8}).status_code == 409
    sent = client.post(f'/continuations/faxes/{job}', headers=ADMIN, json={
        'first_page': 8, 'reason': 'The machine confirmed pages 1 to 7', 'version': item['version']})
    assert sent.status_code == 200, sent.text
    new_fax = continuation_id(attempt)
    settled = sent.json()['item']
    assert settled['state'] == 'settled' and settled['outcome'] == 'not_delivered'
    assert settled['resend_fax_id'] == new_fax
    assert settled['state_text'].endswith('Pages 8–20 were sent as a new fax.')
    events = client.get(f"/certainty/items/{item['id']}/history", headers=ADMIN).json()['events']
    assert events[-1]['text'].endswith('Pages 8–20 were sent as a new fax.')
    assert sent.json()['continued_by']['fax_id'] == new_fax
    # The continuation is not "sent again in full": its link is Sent's continuation section.
    assert client.get(f'/certainty/faxes/{new_fax}', headers=ADMIN).json()['about'] is None
    with engine().connect() as connection:
        assert connection.execute(sa.text('SELECT item_id FROM fax_continuations')).scalar() == item['id']


def test_the_item_refuses_both_sends_at_once_and_send_again_after_a_continuation(client):
    job, _ = broken(client)
    CertaintyStore(engine()).feed(main.app.state.access_runtime.control)
    [item] = client.get(f'/certainty/faxes/{job}', headers=ADMIN).json()['items']
    from app.work.certainty_service import CertaintyInputError, CertaintyService
    service = CertaintyService(CertaintyStore(engine()), main.app.state.access_runtime,
                               values=lambda: main.app.state.configuration_runtime.manager.store.read().active.values)
    with pytest.raises(CertaintyInputError):
        service.settle(None, item['id'], outcome='not_delivered', reason='Both', version=item['version'],
                       send_again=True, continue_from=8, send=lambda **_: None)


def test_it_needs_a_person_who_may_see_the_fax(client):
    job, attempt = broken(client)
    before = fax_ids()
    # Nothing in the background sends it: the uncertain-fax worker runs, the offer stays an offer.
    store = CertaintyStore(engine())
    store.feed(main.app.state.access_runtime.control)
    store.close_delivered()
    store.record_checks()
    assert fax_ids() == before
    reader = client.post('/admin/api-keys', headers=ADMIN, json={'name': 'synthetic', 'scopes': ['inbound:read']})
    other = {'X-API-Key': reader.json()['token']}
    assert client.get(f'/continuations/faxes/{job}', headers=other).status_code == 404
    assert client.post(f'/continuations/faxes/{job}', headers=other, json={'first_page': 8}).status_code == 404
    assert fax_ids() == before and continuation_id(attempt) not in fax_ids()
