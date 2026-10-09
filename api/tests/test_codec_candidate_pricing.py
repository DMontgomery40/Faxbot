"""Encoded candidates must reach measured pricing before an estimated price can reject them."""
from datetime import datetime
from pathlib import Path
import random
import shutil
import string
from types import SimpleNamespace

import pytest
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
import sqlalchemy as sa

from api.tests.test_dense_pages import (ATTEMPT, JOB, PEER, _fax_row, _filled, _new_attempt,
                                       installation, opt_in)  # noqa: F401 - fixture
from api.tests.test_schema import database  # noqa: F401 - fixture
from app import codec, conversion, hylafax_records
from app.codec.store import send_for
from app.pages import coding, sending
from app.routing import predict
from app.routing.costs import RateCard, RateTerms, parse_amount
from app.routing.destinations import LOCAL, DestinationClass


def synthetic_pdf(path):
    """One ordinary page with an embedded font; font bytes make encoded pricing consequential."""
    name = 'CodecPricingEmbedded'
    font = Path(__file__).resolve().parents[1] / 'app/forms/fonts/DejaVuSans.ttf'
    pdfmetrics.registerFont(TTFont(name, str(font)))
    document = canvas.Canvas(str(path), pagesize=(612, 792), invariant=1)
    document.setFont(name, 10)
    rng = random.Random(20261009)
    for line in range(40):
        text = 'SYNTHETIC ' + ''.join(rng.choices(string.ascii_uppercase + string.digits, k=56))
        document.drawString(36, 750 - line * 17, text)
    document.save()


def test_a_supported_encoded_candidate_reaches_measured_pricing_before_being_rejected(
        installation, database, tmp_path):  # noqa: F811
    """Old Group-4 prefilter rejected all candidates. MH encoding of the same PDF costs less and decodes exactly."""
    if not shutil.which('gs'):
        pytest.skip('Ghostscript renders the original PDF')
    pdf, tiff = tmp_path / 'document.pdf', tmp_path / 'document.tiff'
    synthetic_pdf(pdf)
    original = pdf.read_bytes()
    conversion.pdf_to_tiff(str(pdf), str(tiff))
    original_image = tiff.read_bytes()
    _fax_row(database, pages=1)
    _new_attempt(database, ATTEMPT, 1)
    opt_in(database, PEER)
    installation.set_recipient_settings(PEER, packing='never')
    previous = 'c' * 32
    jobs = sa.Table('fax_jobs', sa.MetaData(), autoload_with=database)
    with database.begin() as connection:
        connection.execute(jobs.insert().values(**_filled(jobs, {
            'id': previous, 'to_number': PEER, 'status': 'success', 'backend': 'sip', 'pages': 1})))
    hylafax_records.records_for(database).record_negotiation(
        direction='outbound', call_key='synthetic-prior-call', engine='hylafax', job_id=previous,
        number=PEER, values={'ecm': 'on', 'resolution': 'fine'})
    rate = RateCard(None, 'synthetic-sip', 'outbound', 'Synthetic SIP', 'USD', parse_amount('0.005'),
                    0, 0, 6, 6, None, datetime(2026, 10, 9), None)
    facts = predict.RouteFacts('sip', 'Synthetic SIP', DestinationClass(LOCAL, 'US', '+1', PEER),
                               RateTerms(rate), link=predict.Link(rate=14400, rate_calls=3,
                                                                 rate_scope='number', coding='MMR'))
    values = SimpleNamespace(sip_fax_fine=True, sip_fax_compression='mmr', fax_friendly_documents='never')
    configuration = SimpleNamespace(provider_id='sip', manifest=None, traits={'requires_tiff': True})
    claim = SimpleNamespace(job_id=JOB, attempt_id=ATTEMPT, members=())
    with predict.facts_source(lambda route, destination, now=None: facts):
        changed = sending.prepare(database, values, configuration, claim, {'to_number': PEER}, pdf, tiff)
    assert changed is not None and changed.tiff, 'Measured MH payload was discarded by the Group-4 prefilter'
    assert changed.coding.coding == 'MH'
    received, _ = codec.decode_images(conversion.read_fax_frames(changed.tiff))
    assert received.data == original
    saved = send_for(database, JOB)
    assert saved['pages_original'] == saved['pages_encoded'] == 1
    assert saved['cost_encoded_micros'] < saved['cost_original_micros']
    assert saved['seconds_encoded'] < saved['seconds_original']
    assert pdf.read_bytes() == original and tiff.read_bytes() == original_image
    assert coding.attempt_coding(database, ATTEMPT)['requested'] == 'MH'
