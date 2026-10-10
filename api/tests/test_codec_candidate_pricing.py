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


# The receiving end of a fax to one of your own numbers (live pilot LC-P003, 2026-10-10) ---------------------------

def _receiving(minute='0.10'):
    """A receiving trunk the owner pays for: per minute, whole minutes."""
    from app.routing.predict import ReceivingLeg
    rate = RateCard(None, 'sip-telnyx', 'inbound', 'Telnyx receiving', 'USD', parse_amount(minute), 0, 0, 60, 60,
                    None, datetime(2026, 10, 10), None)
    return ReceivingLeg(rate, 'Telnyx')


def _choose(predict, receiving):
    from api.tests.test_codec_delivery import Shape
    from app.codec import decision
    frames = _payload_page(layout='grid') * 3
    document = codec.Document(random.Random(7).randbytes(2_000), 'application/pdf', 'synthetic.pdf')
    return decision.choose(document, route_key='synthetic-provider', destination=PEER, pages_original=len(frames),
                           page_bits_original=[1] * len(frames), exact_raster=False, ecm_and_fine_seen=False,
                           provider_renders=True, tools=(predict, Shape), frames_original=frames,
                           receiving=receiving)


def test_the_receiving_trunks_bill_can_turn_the_choice_back_to_normal_pages():
    """A per-page sender: the encoded pages save it pages, but take far longer on the line, and the owner's own
    receiving trunk bills that time. Without the receiving end they would win; with it the normal pages do."""
    from api.tests.test_codec_delivery import Prediction
    from app.routing.costs import Money

    def predict(route_key, destination, shape, *, now=None):
        if shape.layout == 'normal':
            return Prediction(shape.pages, 100.0, Money(shape.pages * 70_000, 'USD'), 'synthetic', False)
        return Prediction(shape.pages, 3_000.0, Money(shape.pages * 70_000, 'USD'), 'synthetic', False)
    assert _choose(predict, None).use is True
    chosen = _choose(predict, _receiving())
    assert chosen.use is False
    # On a monthly plan the room saved still counts when the receiving call costs no more.

    def plan(route_key, destination, shape, *, now=None):
        seconds = 100.0 if shape.layout == 'normal' else 90.0
        return Prediction(0, seconds, Money(0, 'USD'), 'synthetic', True)
    kept = _choose(plan, _receiving())
    assert kept.use is True
    assert kept.receiving == ('The receiving call on your Telnyx trunk adds about $0.20 (about $0.20 for the '
                              'normal pages).')


def test_for_a_number_that_is_not_yours_the_explanation_says_the_receiving_cost_is_unknown():
    from api.tests.test_codec_delivery import Prediction
    from app.routing.costs import Money
    from app.routing.predict import ReceivingLeg

    def predict(route_key, destination, shape, *, now=None):
        seconds = 100.0 if shape.layout == 'normal' else 60.0
        return Prediction(shape.pages, seconds, Money(shape.pages * 70_000, 'USD'), 'synthetic', False)
    chosen = _choose(predict, ReceivingLeg())
    assert chosen.use is True
    assert chosen.receiving == 'What the receiving end pays for the call is unknown.'


def test_lc_p003_through_the_real_predictor_counts_both_ends():
    """The real predictor with P003's facts (HumbleFax's plan, the receiving speed 9,600 bit/s and MR learned from
    calls into the number): the encoded pages are priced near the 1,352 s the call took, and the receiving trunk's
    bill for them ($0.0736) is said in the explanation beside what the normal pages would have cost it."""
    from app.routing.costs import Money
    from app.routing.destinations import DestinationClass
    plan_card = RateCard(None, 'humblefax', 'outbound', 'HumbleFax', 'USD', 0, 0, 0, 60, 0, None,
                         datetime(2026, 10, 10), parse_amount('10', whole_digits=4))
    facts = predict.RouteFacts('humblefax', 'HumbleFax', DestinationClass(LOCAL, 'US', '+1', PEER),
                               RateTerms(plan_card), link=predict.Link(rate=9600, rate_calls=1,
                                                                      rate_scope='receiver', coding='MR'))
    seen = {}

    def priced(route_key, destination, shape, *, now=None):
        # The real predictor, on P003's measured sizes (the encoded pages 1.84 Mbit of MR each, the 26 normal
        # pages 0.59 Mbit each, as the pilot's dry run measured them) rather than these stand-in frames.
        if shape.layout == 'codec':
            shape = predict.Shape(7, (850_000,) * 7, 'fine', 'codec',
                                  measured={'MR': (1_836_343,) * 7, 'MMR': (850_000,) * 7})
        else:
            shape = predict.Shape(26, (511_183,) * 26, 'fine', 'normal',
                                  measured={'MR': (594_127,) * 26, 'MMR': (511_183,) * 26})
        seen[shape.layout] = predict.predict_from(facts, shape)
        return seen[shape.layout]
    chosen = _choose(priced, _receiving('0.0032'))
    assert abs(seen['codec'].seconds - 1352) / 1352 < 0.03
    assert seen['codec'].cost == Money(0, 'USD') and seen['codec'].marginal
    assert chosen.use is True  # still shorter on the line, so cheaper on the receiving trunk too
    assert chosen.receiving.startswith('The receiving call on your Telnyx trunk adds about $0.0736 (about $0.')


def test_the_outer_layout_chooser_measures_a_providers_candidates_and_counts_the_receiving_trunk():
    """``conversion.choose_layout`` decides between the pages as they are and the encoded pages too. For a provider
    (no coding chosen) with encoded pages among the candidates, each candidate's MH, MR and MMR are measured, from
    the codec's own measurements when it made them (one ``memo``), so neither is priced from 1.35 x MMR; and the
    owner's receiving trunk's bill decides between them, while the predictions returned stay the sender's own."""
    from app.pages import decision as pages_decision
    from app.routing.costs import Money
    normal = _payload_page(layout='grid') * 3
    encoded = _payload_page(layout='runs')
    memo, shapes = {}, []
    from app.codec.decision import unchosen_measured
    unchosen_measured(normal, memo)  # what the codec measured on the same pages
    calls = []

    def counting(frames, **kwargs):
        calls.append(len(frames))
        return {'MH': [1] * len(frames), 'MR': [1] * len(frames), 'MMR': [1] * len(frames)}

    def predict(route_key, destination, shape):
        shapes.append(shape)
        seconds = 100.0 if shape.layout == 'normal' else 3_000.0
        return pages_decision.Prediction(shape.pages, seconds, Money(shape.pages * 70_000, 'USD'), 'per_page', False)
    import app.pages.coding as codings
    original_measure = codings.measure
    try:
        codings.measure = counting
        chosen = conversion.choose_layout(normal, route='synthetic-provider', destination=PEER, limit='a4',
                                          dense_allowed=False, codec=lambda frames: (encoded, 'Encoded.'),
                                          predict=predict, receiving=_receiving(), memo=memo)
    finally:
        codings.measure = original_measure
    assert calls == [len(encoded)]  # the normal pages' measurement came from the codec's memo
    assert all(set(shape.measured) == {'MH', 'MR', 'MMR'} for shape in shapes)
    assert chosen['layout'] == 'normal'  # 50 receiving minutes cost more than the pages saved
    assert chosen['predictions']['codec'].cost == Money(len(encoded) * 70_000, 'USD')
    without = conversion.choose_layout(normal, route='synthetic-provider', destination=PEER, limit='a4',
                                       dense_allowed=False, codec=lambda frames: (encoded, 'Encoded.'),
                                       predict=predict, memo=memo)
    assert without['layout'] == 'codec'


def test_a_missing_predictor_is_an_error_not_a_quiet_fallback(monkeypatch):
    """routing.predict is part of Faxbot: if it cannot be imported, that is an integration failure to see, never a
    reason to send every fax as normal pages without saying so (00-common, guards and broad excepts)."""
    import sys
    from app.codec import decision
    monkeypatch.setitem(sys.modules, 'app.routing.predict', None)
    with pytest.raises(ImportError):
        decision.predictor()
