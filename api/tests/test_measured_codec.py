"""Measured fax codings (pages/coding.py): each lossless coding measured on the actual pages, and the choice.

The six fixture pages are AR's synthetic benchmark rasters (research corpus, 2026-10-07), the ones Codex
measured with libtiff 4.7.1 on 2026-10-08 (research/faxbot-next-experiments-2026-10-08/results.json, the
"baseline" variant). Exact bit counts are checked only on that libtiff; the order holds on any.
"""
from datetime import datetime
from pathlib import Path
import stat

from PIL import Image, features
import pytest

from app import conversion
from app.pages import coding
from app.routing import predict
from app.routing.costs import Money, RateCard, RateTerms, parse_amount
from app.routing.destinations import LOCAL, DestinationClass

FIXTURES = Path(__file__).resolve().parent / 'fixtures' / 'coding'
# Codex's measurements (results.json, baseline): {page: (MH, MR, MMR)} in bits.
CODEX = {'shaded_0': (698_656, 848_360, 873_336), 'form_5': (515_952, 701_656, 736_160),
         'light_7': (164_016, 154_544, 121_800), 'scan_8': (1_873_616, 2_623_960, 2_847_576),
         'photo': (1_567_736, 1_599_536, 1_580_600), 'drawn_text': (368_048, 248_832, 189_408)}
LIBTIFF_471 = features.version_codec('libtiff') == '4.7.1'
NUMBER = '+12025550123'
DAY = datetime(2026, 10, 8)


def frames(stem):
    return conversion.read_fax_frames(str(FIXTURES / f'{stem}.fine.g4.tiff'))


@pytest.fixture(scope='module')
def measured():
    return {stem: coding.measure(frames(stem), check=True) for stem in CODEX}


# Measuring ----------------------------------------------------------------------------------------------------

def test_the_six_corpus_pages_give_codexs_measured_ordering(measured):
    for stem, page in measured.items():
        mh, mr, mmr = (sum(page[name]) for name in ('MH', 'MR', 'MMR'))
        if LIBTIFF_471:
            assert (mh, mr, mmr) == CODEX[stem], stem
    shaded, form, scan, text = (measured[stem] for stem in ('shaded_0', 'form_5', 'scan_8', 'drawn_text'))
    # MH is smaller than MMR on shading, a tinted form and a noisy gray scan; MMR wins on black text.
    for page in (shaded, form, scan):
        assert sum(page['MH']) < sum(page['MR']) < sum(page['MMR'])
    assert sum(text['MMR']) < sum(text['MR']) < sum(text['MH'])
    # The old fixed ratio (MH = 2 x MMR) overestimates the gray scan's MH about threefold.
    assert 2.9 < sum(scan['MMR']) * predict.WIRE_FACTOR['MH'] / sum(scan['MH']) < 3.1


def test_measured_mmr_is_exactly_the_engines_page_bits(measured):
    for stem in CODEX:
        assert measured[stem]['MMR'] == conversion.frame_bits(frames(stem)), stem


def test_every_coding_round_trips_losslessly_and_bad_pages_are_refused():
    pages = frames('form_5') + frames('drawn_text')
    found = coding.measure(pages, check=True)  # check decodes every page again and compares every pixel
    assert set(found) >= {'MH', 'MR', 'MMR'} and all(len(bits) == 2 for bits in found.values())
    assert ('JBIG' in found) == (coding.jbig_encoder() is not None)
    with pytest.raises(coding.CodingRefused):
        coding.measure([Image.new('L', (1728, 100), 255)])
    with pytest.raises(coding.CodingRefused):
        coding.measure([])
    with pytest.raises(coding.CodingRefused):
        coding.measure(pages, codings=('MH', 'T.6'))


def test_mr_follows_the_resolution_k_parameter():
    """T.4 two-dimensional coding sends a one-dimensional line every 2 lines at standard and 4 at fine, so a page
    measured as fine has more two-dimensional lines: smaller on black text, where they win, larger on shading."""
    for stem, smaller_at_fine in (('drawn_text', True), ('shaded_0', False)):
        fine = frames(stem)
        standard = [page.copy() for page in fine]
        for page in standard:
            page.info['dpi'] = (204.0, 98.0)
        at_fine = sum(coding.measure(fine, codings=('MR',))['MR'])
        at_standard = sum(coding.measure(standard, codings=('MR',))['MR'])
        assert (at_fine < at_standard) == smaller_at_fine, stem


def test_the_measurement_is_kept_beside_the_attempt_files_for_retention(tmp_path, monkeypatch):
    pages = frames('drawn_text')
    cache = coding.cache_path(tmp_path / 'packed-job-attempt.tiff')
    assert cache.name == 'packed-job-attempt.coding.json'  # removed by pages.sending.cleanup with the attempt
    first = coding.measure_cached(pages, cache)
    assert stat.S_IMODE(cache.stat().st_mode) == 0o600
    monkeypatch.setattr(coding, 'measure', lambda *args, **kwargs: pytest.fail('measured again'))
    assert coding.measure_cached(pages, cache) == first
    cache.write_text('not json')
    monkeypatch.undo()
    assert coding.measure_cached(pages, cache) == first


def test_a_jbig_encoder_is_used_only_when_installed(tmp_path, monkeypatch):
    """JBIG is measured through jbigkit's pbmtojbg85 when it is installed: a stand-in shows the call."""
    script = tmp_path / 'pbmtojbg85'
    script.write_text('#!/bin/sh\nhead -c 1000 >/dev/null; cat >/dev/null; printf "%0123d" 0\n')
    script.chmod(0o755)
    monkeypatch.setattr(coding, 'jbig_encoder', lambda: (str(script), str(script)))
    found = coding.measure(frames('drawn_text'))
    assert found['JBIG'] == (8 * 123,)
    monkeypatch.setattr(coding, 'jbig_encoder', lambda: None)
    assert 'JBIG' not in coding.measure(frames('drawn_text'))


# Choosing -----------------------------------------------------------------------------------------------------

def test_the_smallest_usable_coding_is_chosen_and_says_by_how_much(measured):
    shaded = coding.best_coding(frames('shaded_0'), frozenset({'MR', 'MMR'}), ecm=True,
                                measured=measured['shaded_0'])
    assert (shaded.coding, shaded.compared) == ('MH', 'MMR')
    assert shaded.reason == 'MH: 20% shorter than MMR for these pages.'
    assert shaded.bits_per_page == measured['shaded_0']['MH']
    form = coding.best_coding(frames('form_5'), frozenset({'MR', 'MMR'}), ecm=True, measured=measured['form_5'])
    assert form.reason == 'MH: 30% shorter than MMR for these pages.'
    text = coding.best_coding(frames('drawn_text'), frozenset({'MR', 'MMR'}), ecm=True,
                              measured=measured['drawn_text'])
    assert (text.coding, text.compared) == ('MMR', 'MR')
    assert text.reason == 'MMR: 24% shorter than MR for these pages.'


def test_mmr_is_never_chosen_without_error_correction(measured):
    text = coding.best_coding(frames('drawn_text'), frozenset({'MR', 'MMR', 'JBIG'}), ecm=False,
                              measured=measured['drawn_text'])
    assert text.coding == 'MR' and text.reason == 'MR: 32% shorter than MH for these pages.'
    usable = coding.usable_codings(ecm=False, configured='jbig')
    assert usable.codings == frozenset({'MH', 'MR'})
    assert usable.left_out['MMR'] == 'MMR needs error correction, which is off for this call.'
    # The receiving machine has no error correction: MMR is left out even with Faxbot's on.
    usable = coding.usable_codings(ecm=True, far_ecm=False, configured='mmr')
    assert 'MMR' not in usable.codings and usable.ecm is True


def test_the_receiving_machines_capabilities_limit_the_choice(measured):
    from app.engine_frames import decode_dis
    # A DIS with two-dimensional coding (bit 16) and error correction (bit 27) but no T.6 (bit 31).
    fif = bytearray(8)
    fif[1] = 0x2C | 0x80  # V.17 and MR (bit 16 is octet 2's top bit)
    fif[3] = 0x04  # ECM (bit 27)
    dis = decode_dis(bytes([0xFF, 0x13, 0x80]).hex() + bytes(fif).hex())
    assert (dis['mr'], dis['ecm'], dis['mmr']) == (True, True, False)
    usable = coding.usable_codings(ecm=True, dis=dis, configured='jbig')
    assert usable.codings == frozenset({'MH', 'MR'}) and usable.receiver == frozenset({'MH', 'MR'})
    assert usable.left_out['MMR'] == 'The receiving machine does not take MMR.'
    text = coding.best_coding(frames('drawn_text'), usable.codings, ecm=usable.ecm, measured=measured['drawn_text'])
    assert text.coding == 'MR'
    # A machine whose DIS lists only MH gets MH, the coding every machine takes.
    only = coding.usable_codings(ecm=True, dis=decode_dis('ff1380' + '00' * 8), configured='jbig')
    assert only.codings == frozenset({'MH'})
    assert coding.best_coding(frames('drawn_text'), only.codings, ecm=True).reason == (
        'MH: the only coding this call can use.')
    # No DIS on record: every coding up to your setting may be asked for; the engine falls back on the call.
    unknown = coding.usable_codings(ecm=True, configured='mmr')
    assert unknown.codings == frozenset({'MH', 'MR', 'MMR'}) and unknown.receiver is None
    assert unknown.left_out == {'JBIG': 'JBIG is past the most compact coding allowed for this number (MMR).'}


def view(when, compression, status, verdict='remote_fax_failed'):
    return {'direction': 'outbound', 'answered': True, 'when': datetime(2026, 10, 1, when), 'status': status,
            'compression': compression, 'verdict': verdict}


def test_a_coding_that_failed_twice_to_the_number_is_left_out(measured):
    views = [view(1, 'MMR', 'SUCCESS', None), view(2, 'MMR', 'FAILED'), view(3, 'MMR', 'FAILED')]
    usable = coding.usable_codings(ecm=True, views=views, configured='mmr')
    assert 'MMR' not in usable.codings and usable.failed == frozenset({'MMR'})
    assert usable.left_out['MMR'] == ('Faxes to this number with MMR failed 2 times in a row after its fax machine '
                                      'answered.')
    text = coding.best_coding(frames('drawn_text'), usable.codings, ecm=True, measured=measured['drawn_text'])
    assert text.coding == 'MR' and usable.needs_request('MR')  # left alone, the engine would take MMR again
    # A later success with it ends that; a failure before the machine answered never counts.
    later = views + [view(4, 'MMR', 'SUCCESS', None)]
    assert 'MMR' in coding.usable_codings(ecm=True, views=later, configured='mmr').codings
    busy = [view(1, 'MMR', 'SUCCESS', None), view(2, 'MMR', 'FAILED', 'busy'), view(3, 'MMR', 'FAILED', 'busy')]
    assert 'MMR' in coding.usable_codings(ecm=True, views=busy, configured='mmr').codings
    # MH, which every call needs, is never left out.
    mh = [view(1, 'MH', 'FAILED'), view(2, 'MH', 'FAILED'), view(3, 'MH', 'FAILED')]
    assert coding.failing(mh) == {} and 'MH' in coding.usable_codings(ecm=True, views=mh).codings
    # Engine learning's own choice for the number (one step more robust) is a ceiling too.
    assert coding.usable_codings(ecm=True, configured='jbig', learned='mr').codings == frozenset({'MH', 'MR'})


def test_jbig_that_cannot_be_measured_is_kept_only_where_mmr_measured_smallest(measured):
    text = coding.best_coding(frames('drawn_text'), frozenset({'MR', 'MMR', 'JBIG'}), ecm=True,
                              measured=measured['drawn_text'])
    assert (text.coding, text.measured, text.bits_per_page) == ('JBIG', False, measured['drawn_text']['MMR'])
    shaded = coding.best_coding(frames('shaded_0'), frozenset({'MR', 'MMR', 'JBIG'}), ecm=True,
                                measured=measured['shaded_0'])
    assert shaded.coding == 'MH'
    usable = coding.usable_codings(ecm=True, configured='jbig')
    assert not usable.needs_request('JBIG') and usable.needs_request('MH')


# The chooser and the real predictor -------------------------------------------------------------------------

def card(minute='0.005'):
    return RateCard(None, 'sip-telnyx', 'outbound', 'Telnyx', 'USD', parse_amount(minute), parse_amount('0'),
                    parse_amount('0'), 6, 6, None, DAY, None)


def test_the_layout_chooser_prices_each_layout_with_its_measured_coding_on_the_real_predictor():
    """No stand-in: conversion.choose_layout through pages.decision to routing.predict, synthetic facts only."""
    facts = predict.RouteFacts('sip', 'Telnyx', DestinationClass(LOCAL, 'US', '+1', NUMBER), RateTerms(card()))
    pages = frames('scan_8')
    usable = coding.usable_codings(ecm=True, configured='jbig')
    with predict.facts_source(lambda route, destination, now=None: facts):
        chosen = conversion.choose_layout(pages, route='sip', destination=NUMBER, limit='a4', dense_allowed=False,
                                          usable=usable)
        before = conversion.choose_layout(pages, route='sip', destination=NUMBER, limit='a4', dense_allowed=False)
    assert chosen['coding'].coding == 'MH' and before['coding'] is None
    measured, estimated = chosen['predictions']['normal'], before['predictions']['normal']
    assert 'from the measured size of each page in MH' in measured.basis
    assert 'with MR estimated from a fixed ratio to MMR' in estimated.basis
    # Measured MH on the noisy scan: 1,873,616 bits, about 130 s at 14,400 bit/s, against the old estimate.
    assert measured.seconds < estimated.seconds and measured.cost.micros < estimated.cost.micros
    assert isinstance(measured.cost, Money) and measured.p90_seconds > measured.seconds


def test_a_shape_takes_measured_bits_only_for_every_page():
    shape = predict.Shape(1, (10,), 'fine', 'normal', {'MH': (5,), 'MMR': (10,)}, 'MH')
    assert shape.bits_for('MH') == (5,) and shape.bits_for('MR') is None
    assert shape == predict.Shape(1, (10,), 'fine', 'normal', (('MH', (5,)), ('MMR', (10,))), 'MH')
    for bad in ({'MH': (5, 6)}, {'T.6': (5,)}, {'MH': (-1,)}, 7):
        with pytest.raises(predict.ShapeRefused):
            predict.Shape(1, (10,), 'fine', 'normal', bad)
