"""Error correction on a call to one number: one fact (routing/error_correction.py) that the predictor, the joint
route choice, the layout chooser and the dry run all price partial pages with.

Synthetic pages, installations and records only. A page that is MH-coded with error correction (both ends offer it)
pays a turnaround per partial page past its first, at 256-octet frames or 64-octet frames, wherever it is priced.
"""
from datetime import datetime, timedelta
import random
from types import SimpleNamespace
from uuid import uuid4

from PIL import Image
import pytest

from api.app.config_profiles import ProviderConfiguration
from api.app.pages import coding, sending
from api.app.routing import error_correction, predict as predictor
from api.app.routing.error_correction import Fact, decide
from api.app.routing.predict import Link, Shape, line_seconds
from api.tests.test_joint_optimizer import NUMBER, joint  # noqa: F401 (fixture)
from api.tests.test_schema import database  # noqa: F401 (fixture)

USABLE = sending._usable_codings
WHEN = datetime(2026, 10, 9, 12)


def noisy_page(rows, seed=3):
    """One fine page whose rows of scattered dots make its MH code large: 150 rows about 52,900 octets (past the
    16,384-octet block of 64-octet frames, inside one 65,536-octet block), 300 rows about 97,400 (two blocks)."""
    rng = random.Random(seed)
    frame = Image.new('1', (1728, 2156), 1)
    frame.info['dpi'] = (204.0, 196.0)
    pixels = frame.load()
    for y in range(100, 100 + rows):
        for x in range(0, 1728, 2):
            if rng.random() < 0.35:
                pixels[x, y] = 0
    return frame


# The fact itself (pure) ----------------------------------------------------------------------------------------

def test_faxbots_side_decides_first_then_the_newest_thing_the_receiving_machine_said():
    off = decide(False, limit=False)
    assert (off.used, off.why) == (False, 'limit')
    assert off.sentences() == ['Calls to this number go without error correction: you turned it off for this number.']
    assert decide(False).sentences()[0].endswith('it is off in your fax settings.')
    unknown = decide(True)
    assert unknown.used is None and unknown.sentences() == [
        'Faxbot has not yet seen whether its fax machine offers error correction.']
    # The built-in engine's frames hold the far DIS; a successful call that used error correction says it too.
    lacking = decide(True, views=[{'direction': 'outbound', 'far_ecm': False, 'when': WHEN}])
    assert (lacking.used, lacking.why) == (False, 'dis')
    assert lacking.sentences() == ['Its fax machine does not offer error correction, so calls to it go without.']
    negotiated = decide(True, views=[{'direction': 'outbound', 'far_ecm': None, 'status': 'SUCCESS', 'ecm': 'on',
                                      'when': WHEN}])
    assert (negotiated.used, negotiated.why) == (True, 'negotiated')
    assert negotiated.sentences()[0] == 'Calls to this number use error correction: the last fax to it used it.'
    assert negotiated.sentences()[1] == ("Each 64 KB of a page past the first adds about 3.3 seconds on the line "
                                         "(measured on Faxbot's own test calls).")
    # The SSL Fax engine's logged DIS, newer than the frames, wins; an older one does not.
    newer = SimpleNamespace(ecm=True, learned_at=WHEN + timedelta(hours=1))
    older = SimpleNamespace(ecm=True, learned_at=WHEN - timedelta(hours=1))
    assert decide(True, views=[{'far_ecm': False, 'when': WHEN}], capability=newer).used is True
    assert decide(True, views=[{'far_ecm': False, 'when': WHEN}], capability=older).used is False
    assert decide(True, capability=older).used is True
    # Received calls say nothing about how the number receives.
    assert decide(True, views=[{'direction': 'inbound', 'far_ecm': True, 'when': WHEN}]).used is None
    measured = Fact(True, True, True, 64, 4.1, 5)
    assert measured.sentences()[1] == ('Each 16 KB of a page past the first adds about 4.1 seconds on the line '
                                       '(measured on 5 of your calls).')
    assert measured.view() == {'used': True, 'frame_octets': 64, 'block_seconds': 4.1, 'block_calls': 5,
                               'sentences': measured.sentences()}
    with pytest.raises(ValueError):
        Fact(True, True, True, 128)


# The predictor -------------------------------------------------------------------------------------------------

def test_mh_and_mr_pay_partial_pages_when_the_fact_says_the_call_uses_error_correction():
    from app.pages.coding import ECM_BLOCK_SECONDS
    octets = 52_886  # inside one 256-octet-frame block, past three 64-octet-frame block edges

    def seconds(coding_name, fact, ecm=None):
        shape = Shape(1, None, 'fine', 'normal', {coding_name: (octets * 8,)}, coding_name, ecm=ecm)
        return line_seconds(shape, Link(ecm=fact))[0]
    plain = seconds('MH', None)
    assert seconds('MH', Fact(True, True, True)) == pytest.approx(plain)
    # 827 frames of 64 octets against 207 of 256, each with 7 octets of framing.
    framing = (-(-octets // 64) - -(-octets // 256)) * 7 * 8 / predictor.TYPICAL_RATE
    assert framing == pytest.approx(620 * 7 * 8 / 14400)
    at_64 = seconds('MH', Fact(True, True, True, 64))
    assert at_64 - plain == pytest.approx(3 * ECM_BLOCK_SECONDS + framing)
    assert seconds('MR', Fact(True, True, True, 64)) - seconds('MR', None) == pytest.approx(at_64 - plain)
    # A measured turnaround replaces the loopback's figure; a call without error correction pays none.
    assert seconds('MH', Fact(True, True, True, 64, 4.0, 3)) - plain == pytest.approx(3 * 4.0 + framing)
    assert seconds('MMR', Fact(False, None, False, 64)) == pytest.approx(seconds('MMR', None, ecm=False))
    # What the shape says outright still wins over the number's fact.
    assert seconds('MH', Fact(True, True, True, 64), ecm=False) == pytest.approx(plain)
    # Without a fact, only MMR and JBIG are assumed to use it (as before).
    assert seconds('MH', None, ecm=True) == pytest.approx(plain)


def test_the_page_size_estimate_pays_partial_pages_too():
    bits = 600_000 * 8  # a page Faxbot prepared at 600 KB of T.6: past several blocks in any coding
    shape = Shape(1, (bits,), 'fine', 'normal', coding='MH')
    plain, _ = line_seconds(shape, Link())
    corrected, how = line_seconds(shape, Link(ecm=Fact(True, True, True)))
    assert corrected > plain and how.endswith('more where a page needs more than one error-correction block')


def test_every_adapter_carries_every_field_the_predictor_reads():
    from api.app.pages import decision
    page = decision.Shape(2, (1, 2), 'fine', 'dense', measured={'MH': (8, 9)}, coding='MH', ecm=True)
    carried = predictor.shape_with(page)
    assert (carried.pages, carried.page_bits, carried.resolution, carried.layout, carried.coding, carried.ecm) == (
        2, (1, 2), 'fine', 'dense', 'MH', True)
    assert carried.bits_for('MH') == (8, 9)
    assert predictor.shape_with(carried, pages=1, page_bits=(3,), measured=None).pages == 1


# One fact, every path ------------------------------------------------------------------------------------------

def _paths(env, monkeypatch, frames, tmp_path):
    """The predicted seconds of these pages on the trunk, the pages as they are, from the dry run, the joint route
    choice's account evaluation and the layout chooser."""
    from api.app import conversion
    from api.app.routing.predict_facts import facts_for
    from api.app.routing.predict_http import _coded_view
    monkeypatch.setattr(sending, '_usable_codings', USABLE)  # the real codings and fact, not the fixture's
    engine, values = env.engine, env.configuration.read().active.values
    claim = SimpleNamespace(job_id=uuid4().hex, attempt_id=uuid4().hex, members=())
    tiff, pdf = tmp_path / f'{claim.job_id}.tiff', tmp_path / f'{claim.job_id}.pdf'
    conversion.write_fax_tiff(frames, str(tiff))
    conversion.tiff_to_pdf(str(tiff), str(pdf))
    pages = conversion.read_fax_frames(str(tiff))
    facts = facts_for('sip', NUMBER, values=values, engine=engine)
    measured = coding.measure(pages)
    dry = _coded_view(engine, values, NUMBER, facts, pages, measured, 'fine')
    account = sending.account_for(engine, values, ProviderConfiguration('sip', traits={'requires_tiff': True}),
                                  NUMBER, key='sip')
    evaluated = sending.evaluate(engine, values, account, claim, {'to_number': NUMBER, 'pages': len(pages)}, pdf,
                                 tiff, compare=True)
    worker = next(option for option in evaluated.options if option.rendering == 'as_is' and option.layout == 'normal')
    usable = coding.usable_for(engine, values, NUMBER, capability=evaluated.permission.cap)
    chosen = conversion.choose_layout(pages, route='sip', destination=NUMBER, limit='a4', dense_allowed=False,
                                      usable=usable, predict=account.predict())
    return {'facts': facts, 'usable': usable, 'dry': dry, 'worker': worker,
            'chooser': chosen['predictions']['normal'].seconds}


@pytest.mark.parametrize('frame, rows, edges', [(64, 150, 3), (256, 150, 0), (256, 300, 1), (64, 300, 5)])
def test_the_dry_run_the_joint_choice_and_the_chooser_price_the_same_partial_pages(joint, monkeypatch, tmp_path,  # noqa: F811
                                                                                 frame, rows, edges):
    fact = Fact(True, True, True, frame)
    monkeypatch.setattr(error_correction, 'fact_for', lambda *args, **kwargs: fact)
    found = _paths(joint, monkeypatch, [noisy_page(rows)], tmp_path)
    assert found['facts'].link.ecm is fact and found['usable'].error_correction is fact
    assert found['usable'].frame_octets == frame
    assert found['dry']['coding']['coding'] == 'MH' and found['worker'].coding == 'MH'
    seconds = found['chooser']
    assert found['worker'].seconds == pytest.approx(seconds)
    assert found['dry']['seconds'] == round(seconds, 1)
    # Against the same pages priced with the shape saying "no error correction": the partial pages and framing.
    octets = -(-coding.measure([noisy_page(rows)], codings=('MH',))['MH'][0] // 8)
    plain = line_seconds(Shape(1, None, 'fine', 'normal', {'MH': (octets * 8,)}, 'MH', ecm=False),
                         found['facts'].link)[0]
    extra = error_correction.frame_seconds((octets * 8,), frame, found['facts'].link.typical_rate)
    assert seconds - plain == pytest.approx(edges * fact.turnaround + extra, abs=0.01)


def test_the_real_fact_from_the_receiving_machines_records_reaches_every_path(joint, monkeypatch, tmp_path):  # noqa: F811
    """No stand-in: the fixture recorded that the receiving machine offers error correction (its logged DIS), and
    the trunk's error correction is on, so a 97 KB MH page pays one partial page wherever it is priced."""
    found = _paths(joint, monkeypatch, [noisy_page(300)], tmp_path)
    fact = found['facts'].link.ecm
    assert (fact.ours, fact.far, fact.used, fact.why, fact.frame_octets) == (True, True, True, 'dis', 256)
    assert found['usable'].error_correction == fact
    assert 'error-correction block' in found['dry']['basis']
    assert found['worker'].seconds == pytest.approx(found['chooser'])
    assert found['dry']['seconds'] == round(found['chooser'], 1)
