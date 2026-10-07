"""Experimental payload codec in delivery: opt-in, the per-fax decision, sending and receiving."""
from collections import namedtuple
from email.message import EmailMessage
from pathlib import Path
import random

import pytest
from fastapi.testclient import TestClient

from app import codec, main
from app.codec import decision, receive, send
from app.conversion import tiff_to_pdf

BOOTSTRAP = 'synthetic-codec-bootstrap'
ADMIN = {'X-API-Key': BOOTSTRAP}
NUMBER = '+12025550123'
Prediction = namedtuple('Prediction', 'billed_pages seconds cost basis marginal')
Shape = namedtuple('Shape', 'pages page_bits resolution layout')


def per_page(price=45_000, rate=14_400):
    """A fake predictor for a per-page route: cost by pages, seconds by bits."""
    seen = []

    def predict(route_key, destination, shape, *, now=None):
        seen.append(shape)
        return Prediction(shape.pages, sum(shape.page_bits) / rate + 8 * shape.pages, shape.pages * price,
                          'synthetic per-page card', False)
    return (predict, Shape), seen


def per_minute(price_per_minute=5_000, rate=14_400):
    def predict(route_key, destination, shape, *, now=None):
        seconds = sum(shape.page_bits) / rate + 8 * shape.pages
        return Prediction(0, seconds, -(-int(seconds) // 60) * price_per_minute, 'synthetic per-minute card', False)
    return (predict, Shape), []


def _document(size, seed=1):
    return codec.Document(random.Random(seed).randbytes(size), 'application/pdf', 'scan.pdf')


# --- the decision ---------------------------------------------------------------------------------------------

def test_saves_compares_money_first_then_pages_and_seconds_and_never_guesses():
    normal = Prediction(23, 400.0, 23 * 45_000, 'card', False)
    assert decision.saves(normal, Prediction(1, 200.0, 45_000, 'card', False), 23, 1)
    assert not decision.saves(normal, Prediction(1, 200.0, 23 * 45_000, 'card', False), 23, 1)
    plan = Prediction(23, 400.0, 0, 'plan', True)
    assert decision.saves(plan, Prediction(1, 300.0, 0, 'plan', True), 23, 1)  # frees plan room
    assert not decision.saves(plan, Prediction(1, 500.0, 0, 'plan', True), 23, 1)  # but never more time
    unknown = Prediction(None, None, None, 'no card', False)
    assert not decision.saves(unknown, Prediction(None, None, None, 'no card', False), 23, 1)
    assert not decision.saves(None, normal, 23, 1)


def test_a_per_page_route_sends_a_long_document_as_one_encoded_page():
    tools, seen = per_page()
    choice = decision.choose(_document(12_000), route_key='sinch', destination=NUMBER, pages_original=23,
                             page_bits_original=[40_000] * 23, exact_raster=False, ecm_and_fine_seen=False,
                             provider_renders=True, tools=tools)
    assert choice.use and choice.layout == 'grid' and choice.sturdy is True and choice.pages_encoded == 1
    assert choice.sentence == 'Sent as 1 encoded page instead of 23 (experimental).'
    assert [shape.layout for shape in seen] == ['normal', 'codec']


def test_a_per_minute_route_keeps_normal_pages_when_encoded_ones_take_longer():
    tools, _ = per_minute()
    choice = decision.choose(_document(60_000), route_key='telnyx', destination=NUMBER, pages_original=2,
                             page_bits_original=[30_000, 30_000], exact_raster=True, ecm_and_fine_seen=True,
                             provider_renders=False, tools=tools)
    assert not choice.use
    assert choice.sentence == 'Encoded pages would not save on this route, so the fax goes as normal pages.'


def test_run_coded_pages_are_a_candidate_only_after_ecm_and_fine_were_seen():
    tools, _ = per_page()
    seen_before = decision.choose(_document(150_000), route_key='sip', destination=NUMBER, pages_original=40,
                                  page_bits_original=[40_000] * 40, exact_raster=True, ecm_and_fine_seen=True,
                                  provider_renders=False, tools=tools)
    assert seen_before.use and seen_before.layout == 'runs'
    never_seen = decision.choose(_document(150_000), route_key='sip', destination=NUMBER, pages_original=40,
                                 page_bits_original=[40_000] * 40, exact_raster=True, ecm_and_fine_seen=False,
                                 provider_renders=False, tools=tools)
    assert never_seen.use and never_seen.layout == 'grid' and never_seen.pages_encoded > seen_before.pages_encoded


def test_without_the_shared_predictor_nothing_is_encoded(monkeypatch):
    monkeypatch.setattr(decision, 'predictor', lambda: None)
    choice = decision.choose(_document(1000), route_key='sinch', destination=NUMBER, pages_original=3,
                             page_bits_original=[1, 2, 3], exact_raster=False, ecm_and_fine_seen=False,
                             provider_renders=True)
    assert not choice.use and 'cannot yet predict' in choice.sentence


# --- receiving ------------------------------------------------------------------------------------------------

def _payload_pdf(tmp_path, document, **options):
    encoded = codec.encode_document(document, **options)
    tiff = codec.write_tiff(encoded.pages, tmp_path / 'payload.tiff')
    tiff_to_pdf(str(tiff), str(tmp_path / 'payload.pdf'))
    return (tmp_path / 'payload.pdf').read_bytes(), encoded


class _Receipts:
    """An in-memory stand-in for the codec_receipts table."""

    def __init__(self, monkeypatch):
        self.rows = {}
        monkeypatch.setattr(receive, 'receipt_for', lambda engine, fax: self.rows.get(fax))
        monkeypatch.setattr(receive, 'record_receipt', self.record)

    def record(self, engine, fax, values, now=None):
        self.rows.setdefault(fax, {'inbound_fax_id': fax, **values})
        return self.rows[fax]


def test_a_received_payload_fax_is_decoded_checked_and_kept_beside_the_fax(tmp_path, monkeypatch):
    receipts = _Receipts(monkeypatch)
    original = tmp_path / 'original.pdf'
    from reportlab.pdfgen import canvas
    page = canvas.Canvas(str(original))
    page.drawString(72, 720, 'Synthetic referral letter, page one.')
    page.save()
    document = codec.Document(original.read_bytes(), 'application/pdf', 'referral.pdf')
    data, _ = _payload_pdf(tmp_path, document, layout='grid')
    receipt = receive.check_document(None, 'fax-1', data, folder=tmp_path)
    assert receipt['state'] == 'decoded' and receipt['document_sha256'] == document.sha256
    assert Path(receipt['document_path']).read_bytes() == document.data
    assert receive.sentence(receipt) == ('Carried an encoded document on 1 page; Faxbot decoded it and checked its '
                                         'fingerprint (experimental).')
    assert receive.check_document(None, 'fax-1', b'not even read', folder=tmp_path) is receipt  # written once
    attachment, note = receive.email_extras(None, {'inbound_fax_id': 'fax-1'}, data, folder=tmp_path)
    message = EmailMessage()
    message.set_content('A fax arrived.\n')
    message.add_attachment(data, maintype='application', subtype='pdf', filename='fax.pdf')
    receive.attach(message, attachment, note)
    names = [part.get_filename() for part in message.iter_attachments()]
    assert names == ['fax.pdf', 'decoded-referral.pdf'] and 'decoded it and attached' in message.get_body().get_content()
    assert receipts.rows['fax-1']['state'] == 'decoded'


def test_a_payload_fax_that_cannot_be_decoded_is_delivered_as_received_with_one_sentence(tmp_path, monkeypatch):
    _Receipts(monkeypatch)
    document = codec.Document(b'x' * 5000, 'text/plain', 'note.txt')
    data, encoded = _payload_pdf(tmp_path, document, layout='grid', secret='partner key one', fec='low')
    receipt = receive.check_document(None, 'fax-2', data, folder=tmp_path)
    assert receipt['state'] == 'failed'
    assert receipt['reason'] == ('The document is encrypted and no shared key is set for this sender, so the fax is '
                                 'delivered as received.')
    attachment, note = receive.email_extras(None, {'inbound_fax_id': 'fax-2'}, data, folder=tmp_path)
    assert attachment is None and note == receipt['reason']


def test_an_ordinary_fax_has_no_decode_result(tmp_path, monkeypatch):
    receipts = _Receipts(monkeypatch)
    from PIL import Image, ImageDraw
    page = Image.new('1', (1728, 2156), 1)
    ImageDraw.Draw(page).text((100, 100), 'An ordinary synthetic fax.', fill=0)
    page.save(tmp_path / 'plain.tiff', compression='group4', dpi=(204, 196))
    tiff_to_pdf(str(tmp_path / 'plain.tiff'), str(tmp_path / 'plain.pdf'))
    assert receive.check_document(None, 'fax-3', (tmp_path / 'plain.pdf').read_bytes(), folder=tmp_path) is None
    assert receipts.rows == {}


# --- over HTTP and through acceptance -------------------------------------------------------------------------

@pytest.fixture
def client(isolated_installation, monkeypatch):
    for name, value in {'REQUIRE_API_KEY': 'true', 'API_KEY': BOOTSTRAP, 'PUBLIC_API_URL': 'https://testserver',
                        'FAX_BACKEND': 'phaxio', 'MAX_REQUESTS_PER_MINUTE': '0'}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv('FAXBOT_CONSOLE_ORIGINS', 'https://testserver')
    with TestClient(main.app, base_url='https://testserver', headers={'Origin': 'https://testserver'}) as client:
        yield client


def test_a_number_opts_in_only_with_the_recipients_agreement_and_keeps_its_history(client):
    off = client.get(f'/codec/numbers/{NUMBER}', headers=ADMIN)
    assert off.status_code == 200, off.text
    assert off.json()['enabled'] is False and off.json()['state_sentence'] == (
        'Off: faxes to this number go as normal pages.')
    assert 'HIPAA' in off.json()['limits_text'] and 'Experimental' in off.json()['limits_text']
    refused = client.put(f'/codec/numbers/{NUMBER}', headers=ADMIN, json={'enabled': True})
    assert refused.status_code == 400
    assert refused.json()['detail'] == 'Record that the recipient agreed before turning encoded pages on.'
    on = client.put(f'/codec/numbers/{NUMBER}', headers=ADMIN, json={
        'enabled': True, 'recipient_agreed': True, 'fec': 'high', 'shared_key': 'synthetic partner key', 'version': 0})
    assert on.status_code == 200, on.text
    view = on.json()
    assert view['enabled'] and view['fec'] == 'high' and view['has_key'] and len(view['key_fingerprint']) == 8
    assert 'synthetic partner key' not in on.text
    assert view['agreement']['recipient_agreed'] is True
    assert client.put(f'/codec/numbers/{NUMBER}', headers=ADMIN, json={'enabled': True, 'version': 0}).status_code == 409
    gone = client.delete(f'/codec/numbers/{NUMBER}', headers=ADMIN)
    assert gone.status_code == 200 and gone.json()['enabled'] is False
    assert [change['action'] for change in gone.json()['history']] == ['off', 'on']


def test_an_opted_in_fax_on_a_per_page_route_goes_as_one_encoded_page(client, monkeypatch, tmp_path):
    tools, _ = per_page()
    monkeypatch.setattr(decision, 'predictor', lambda: tools)
    assert client.put(f'/codec/numbers/{NUMBER}', headers=ADMIN,
                      json={'enabled': True, 'recipient_agreed': True}).status_code == 200
    body = '\n'.join(f'Synthetic line {index} of a long letter.' for index in range(400)).encode()
    sent = client.post('/fax', headers=ADMIN, data={'to': NUMBER}, files={'file': ('letter.txt', body, 'text/plain')})
    assert sent.status_code == 202, sent.text
    job = sent.json()['id']
    detail = client.get(f'/codec/faxes/{job}', headers=ADMIN)
    assert detail.status_code == 200, detail.text
    pages, encoded = sent.json()['pages'], detail.json()['pages_encoded']
    assert 1 <= encoded < pages
    assert detail.json()['sentence'] == f'Sent as {encoded} encoded pages instead of {pages} (experimental).'
    root = Path(main.settings.fax_data_dir)
    original = (root / f'{job}.pdf').read_bytes()
    payload = root / f'{job}.payload-phaxio.pdf'
    assert payload.is_file() and payload.read_bytes() != original
    assert send.transmitted_pdf(str(root / f'{job}.pdf'), job, 'phaxio') == str(payload)
    assert send.transmitted_pdf(str(root / f'{job}.pdf'), job, 'sinch') == str(root / f'{job}.pdf')
    back, _ = codec.decode_images(codec.read_images(payload.read_bytes()))
    assert back.data == original  # the recipient gets exactly the accepted document


def test_codec_routes_need_their_permissions(client):
    response = client.post('/admin/api-keys', headers=ADMIN, json={'name': 'synthetic', 'scopes': ['fax:send']})
    sender = {'X-API-Key': response.json()['token']}
    assert client.get(f'/codec/numbers/{NUMBER}', headers=sender).status_code == 403
    assert client.put(f'/codec/numbers/{NUMBER}', headers=sender,
                      json={'enabled': True, 'recipient_agreed': True}).status_code == 403
