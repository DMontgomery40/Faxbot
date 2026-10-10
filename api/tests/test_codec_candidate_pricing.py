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
    # And each engine is asked for MH: the SSL Fax engine's job data format, the built-in engine's channel variable.
    from app import ami, hylafax_engine
    from api.tests.test_hylafax_records import values as engine_values
    call = hylafax_engine.CallSettings(t38=True, max_rate=14400, ecm=True, fine=True, compression='jbig')
    asked = hylafax_engine.with_coding(call, changed.coding.request('hylafax'))
    assert hylafax_engine._DATA_FORMATS[asked.compression] == 'G31D'
    builtin = hylafax_engine.with_coding(call, changed.coding.request('builtin'))
    configured = engine_values(FAX_DATA_DIR=str(tmp_path),
                               ASTERISK_INBOUND_SECRET='synthetic-inbound-secret-0123456789')
    fields = ami.originate_fields_for(configured, JOB, PEER, '/faxdata/x.tiff', attempt_id=ATTEMPT, call=builtin)
    assert 'FAXBOT_COMPRESSION=mh' in fields['Variable']


def _payload_page(size=40_000, layout='runs'):
    document = codec.Document(random.Random(20261010).randbytes(size), 'application/pdf', 'synthetic.pdf')
    return codec.encode_document(document, layout=layout, fec='medium', salt=b's' * 16, nonce=b'n' * 12).pages


def test_a_payload_page_measures_smaller_in_mh_and_mr_than_in_mmr():
    """N2's premise, on the shipped run-coded layout: the payload page is drawn so that its MH code is the payload,
    so the one-dimensional coding is smallest and MMR (the engines' own default under error correction) largest."""
    measured = coding.measure(_payload_page(), codings=('MH', 'MR', 'MMR'))
    mh, mr, mmr = (sum(measured[name]) for name in ('MH', 'MR', 'MMR'))
    assert mh < mr < mmr and mmr > 1.2 * mh, measured
    chosen = coding.best_coding(_payload_page(), {'MH', 'MR', 'MMR'}, ecm=True, measured=measured)
    assert chosen.coding == 'MH'


def test_where_faxbot_does_not_choose_the_coding_each_layout_is_priced_from_its_measured_size():
    """A provider draws the pages, so no coding is requested: the shapes carry every measured coding, and the
    predictor reads the one it expects for the call, never MR estimated as 1.35 x MMR (which, on a payload page that
    is already larger in MMR than in MR, prices it far above its cost)."""
    from api.tests.test_codec_delivery import Prediction, Shape
    seen = []

    def predict(route_key, destination, shape, *, now=None):
        seen.append(shape)
        bits = dict(shape.measured)['MR']
        return Prediction(shape.pages, sum(bits) / 14400 + 8 * shape.pages, shape.pages * 45_000, 'synthetic', False)
    frames = _payload_page(layout='grid') * 3  # any one-bit pages stand in for a three-page original here
    document = codec.Document(random.Random(7).randbytes(2_000), 'application/pdf', 'synthetic.pdf')
    from app.codec import decision
    choice = decision.choose(document, route_key='synthetic-provider', destination=PEER, pages_original=len(frames),
                             page_bits_original=[1] * len(frames), exact_raster=False, ecm_and_fine_seen=False,
                             provider_renders=True, tools=(predict, Shape), frames_original=frames)
    assert [shape.layout for shape in seen] == ['normal', 'codec']
    for shape in seen:
        assert set(dict(shape.measured)) == set(decision.UNCHOSEN_CODINGS)
        assert shape.page_bits == tuple(dict(shape.measured)['MMR'])
    assert choice.pages_encoded >= 1


def test_the_real_predictor_prices_an_unchosen_payload_page_from_its_measured_coding():
    """Companion with routing.predict itself: the call's expected coding (MR, learned from earlier calls) is priced
    from the payload page's measured MR size, not from 1.35 x its MMR size."""
    pages = _payload_page()
    measured = coding.measure(pages, codings=('MH', 'MR', 'MMR'))
    rate = RateCard(None, 'synthetic-sip', 'outbound', 'Synthetic SIP', 'USD', parse_amount('0.005'),
                    0, 0, 6, 6, None, datetime(2026, 10, 9), None)
    facts = predict.RouteFacts('sip', 'Synthetic SIP', DestinationClass(LOCAL, 'US', '+1', PEER), RateTerms(rate),
                               link=predict.Link(rate=14400, rate_calls=3, rate_scope='number', coding='MR'))
    shape = predict.Shape(pages=len(pages), page_bits=tuple(measured['MMR']), resolution='fine', layout='codec',
                          measured=measured)
    priced = predict.predict_from(facts, shape)
    estimated = predict.predict_from(facts, predict.Shape(pages=len(pages), page_bits=tuple(measured['MMR']),
                                                          resolution='fine', layout='codec'))
    data = sum(measured['MR']) / 14400
    assert abs(priced.seconds - (predict.SETUP_SECONDS + data + predict.PAGE_SECONDS * len(pages))) < 0.01
    assert estimated.seconds > priced.seconds * 1.3  # the fixed ratio overprices the payload page
