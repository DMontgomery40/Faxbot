"""Measured fax codings (pages/coding.py): each lossless coding measured on the actual pages, and the choice.

The six fixture pages are AR's synthetic benchmark rasters (research corpus, 2026-10-07), the ones Codex
measured with libtiff 4.7.1 on 2026-10-08 (research/faxbot-next-experiments-2026-10-08/results.json, the
"baseline" variant). Exact bit counts are checked only on that libtiff; the order holds on any.
"""
from datetime import datetime
from pathlib import Path
import stat
from types import SimpleNamespace

from PIL import Image, features
import pytest

from api.tests.test_dense_pages import ATTEMPT, JOB, PEER, _send, installation  # noqa: F401 - fixture
from api.tests.test_hylafax_engine import engine  # noqa: F401 - fixture
from api.tests.test_schema import database  # noqa: F401 - fixture
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


def test_jbig_that_cannot_be_measured_stays_the_ssl_fax_engines_and_the_built_in_engine_gets_the_smallest(
        measured):
    """Loopback, 8 October 2026: three shaded pages to a JBIG machine took 58 s of transfer in JBIG, 108 s in MH."""
    text = coding.best_coding(frames('drawn_text'), frozenset({'MR', 'MMR', 'JBIG'}), ecm=True,
                              measured=measured['drawn_text'])
    assert (text.coding, text.measured, text.fallback, text.priced) == ('JBIG', False, 'MMR', 'MMR')
    assert text.bits_per_page == measured['drawn_text']['MMR']
    assert (text.request('hylafax'), text.request('builtin')) == ('JBIG', 'MMR')
    shaded = coding.best_coding(frames('shaded_0'), frozenset({'MR', 'MMR', 'JBIG'}), ecm=True,
                                measured=measured['shaded_0'])
    assert (shaded.coding, shaded.fallback, shaded.request('hylafax'), shaded.request('builtin')) == (
        'JBIG', 'MH', 'JBIG', 'MH')
    assert shaded.fallback_reason == 'MH: 20% shorter than MMR for these pages.'
    assert shaded.reason == ('JBIG where the receiving machine takes it (not measured here), otherwise MH: 20% '
                             'shorter than MMR for these pages.')
    usable = coding.usable_codings(ecm=True, configured='jbig')
    assert not usable.needs_request('JBIG') and usable.needs_request('MH')
    # The built-in engine has no JBIG: MMR is all it would take anyway; MH is not.
    assert not usable.needs_request('MMR', 'builtin') and usable.needs_request('MH', 'builtin')


# The chooser and the real predictor -------------------------------------------------------------------------

def card(minute='0.005'):
    return RateCard(None, 'sip-telnyx', 'outbound', 'Telnyx', 'USD', parse_amount(minute), parse_amount('0'),
                    parse_amount('0'), 6, 6, None, DAY, None)


def test_the_layout_chooser_prices_each_layout_with_its_measured_coding_on_the_real_predictor():
    """No stand-in: conversion.choose_layout through pages.decision to routing.predict, synthetic facts only."""
    facts = predict.RouteFacts('sip', 'Telnyx', DestinationClass(LOCAL, 'US', '+1', NUMBER), RateTerms(card()))
    pages = frames('scan_8')
    # No DIS on record: JBIG is left out (pages/coding.py JBIG_NOT_ON_RECORD), so MH, measured smallest, is priced.
    usable = coding.usable_codings(ecm=True, configured='jbig')
    assert usable.left_out['JBIG'] == coding.JBIG_NOT_ON_RECORD
    with predict.facts_source(lambda route, destination, now=None: facts):
        chosen = conversion.choose_layout(pages, route='sip', destination=NUMBER, limit='a4', dense_allowed=False,
                                          usable=usable)
        before = conversion.choose_layout(pages, route='sip', destination=NUMBER, limit='a4', dense_allowed=False)
    assert (chosen['coding'].coding, chosen['coding'].priced) == ('MH', 'MH') and before['coding'] is None
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


# The coding reaches each engine -------------------------------------------------------------------------------

def call_settings(ecm=True, compression='jbig'):
    from app import hylafax_engine
    return hylafax_engine.CallSettings(t38=True, max_rate=14400, ecm=ecm, fine=True, compression=compression)


def test_the_ssl_fax_engine_gets_the_coding_as_its_job_data_format(engine, tmp_path):  # noqa: F811
    from app import hylafax_engine
    values, server = engine
    image = tmp_path / 'fax.tiff'
    image.write_bytes(b'II*\x00synthetic')
    for coding_name, data_format in (('MH', 'G31D'), ('MR', 'G32D'), ('MMR', 'G4'), ('JBIG', 'JBIG')):
        settings = hylafax_engine.with_coding(call_settings(), coding_name)
        assert (settings.coding, settings.compression) == (coding_name, coding.SETTING[coding_name])
        server.commands.clear()
        hylafax_engine.create_job(values, tag=hylafax_engine.new_tag(), job_id=JOB, attempt_id=ATTEMPT,
                                  tiff_path=str(image), settings=settings, host='127.0.0.1',
                                  port=server.server_address[1]).discard()
        assert f'JPARM DATAFORMAT "{data_format}"' in server.commands, coding_name
    # No coding: the call's own compression (your setting, or what engine learning chose).
    assert hylafax_engine.with_coding(call_settings(compression='mr'), None) == call_settings(compression='mr')
    # MMR without error correction becomes MR; error correction is never turned on for it.
    lowered = hylafax_engine.with_coding(call_settings(ecm=False), 'MMR')
    assert (lowered.coding, lowered.compression, lowered.ecm) == ('MR', 'mr', False)
    with pytest.raises(ValueError):
        hylafax_engine.with_coding(call_settings(), 'T.6')


def test_the_built_in_engine_gets_the_coding_as_a_channel_variable(tmp_path):
    from app import ami, hylafax_engine
    from api.tests.test_hylafax_records import values
    configured = values(FAX_DATA_DIR=str(tmp_path), ASTERISK_INBOUND_SECRET='synthetic-inbound-secret-0123456789')
    for coding_name, offered in (('MH', 'mh'), ('MR', 'mr'), ('MMR', 'mmr'), ('JBIG', 'mmr')):
        call = hylafax_engine.with_coding(call_settings(), coding_name)
        fields = ami.originate_fields_for(configured, JOB, PEER, '/faxdata/x.tiff', attempt_id=ATTEMPT, call=call)
        assert f'FAXBOT_COMPRESSION={offered}' in fields['Variable'], coding_name
        assert 'FAXBOT_ECM=yes' in fields['Variable']
    fields = ami.originate_fields_for(configured, JOB, PEER, '/faxdata/x.tiff', attempt_id=ATTEMPT,
                                      call=call_settings())
    assert 'FAXBOT_COMPRESSION' not in fields['Variable']
    with pytest.raises(ValueError):
        ami.prepare_originate_fields(JOB, PEER, '/faxdata/x.tiff', caller_id='', compression='t6')


# An attempt on the trunk: measured, recorded, said --------------------------------------------------------------

# Shaded areas kept as they are (pages/friendly.py lightens them where that saves; measured here unlightened).
KEEP_SHADING = SimpleNamespace(sip_fax_fine=True, fax_friendly_documents='never')

def dcs(compression):
    """A DCS frame (address, control, FCF, FIF) at 14,400 bit/s with this coding (T.30 Table 2)."""
    fif = bytearray(4)
    fif[1] = 0x20 | (0x80 if compression == 'MR' else 0)
    if compression == 'MMR':
        fif[3] = 0x04 | 0x40  # ECM and T.6
    return (bytes([0xFF, 0x13, 0x83]) + bytes(fif)).hex()


def add_frames(engine_db, attempt, compression):
    import sqlalchemy as sa
    table = sa.Table('fax_call_frames', sa.MetaData(), autoload_with=engine_db)
    with engine_db.begin() as connection:
        connection.execute(table.insert().values(id=f'out:{attempt}', direction='out', job_id=JOB,
                                                 attempt_id=attempt, number=PEER, status='SUCCESS',
                                                 dcs_first=dcs(compression), dcs_last=dcs(compression),
                                                 created_at=datetime(2026, 10, 8, 9, 1)))


def test_a_trunk_attempt_asks_for_the_measured_coding_records_it_and_the_sent_detail_says_it(
        installation, database, tmp_path):  # noqa: F811
    from app.pages import sending
    # Your compression setting stops at MMR, so JBIG is not usable and MH, measured smallest, is the request.
    values = SimpleNamespace(sip_fax_fine=True, fax_friendly_documents='never', sip_fax_compression='mmr')
    changed = _send(database, tmp_path, pages=frames('shaded_0'), values=values)
    # The pages go as they are; MH goes with the call, because the engine would otherwise take a larger coding.
    assert sending.unchanged(changed) and changed.coding.coding == 'MH'
    assert changed.coding.request('hylafax') == changed.coding.request('builtin') == 'MH'
    assert coding.cache_path(tmp_path / f'packed-{JOB}-{ATTEMPT}.tiff').is_file()
    record = coding.newest_coding(database, JOB)
    assert (record['requested'], record['measured'], record['compared'], record['pages']) == ('MH', 1, 'MMR', 1)
    assert record['receiver_known'] == 0 and record['negotiated'] is None
    assert set(record['bits']) >= {'MH', 'MR', 'MMR'} and record['bits']['MH'] < record['bits']['MMR']
    view = coding.sent_view(database, JOB)
    assert view['sentence'] == 'Sent with MH: 20% shorter than MMR for these pages.'
    assert view['measured_sentence'].startswith('Measured on these pages at 14,400 bit/s: MH about 49 seconds, MR ')
    # What the call used, from the built-in engine's frames; another coding than asked for is said as a fact.
    add_frames(database, ATTEMPT, 'MH')
    assert coding.sent_view(database, JOB)['negotiated'] == 'MH'
    assert coding.sent_sentence({**record, 'negotiated': 'MH'}, 'failed') == (
        'Tried with MH: 20% shorter than MMR for these pages.')
    assert coding.sent_sentence({**record, 'requested': 'MR', 'reason': 'MR: 5% shorter than MH for these pages.',
                                 'negotiated': 'MH'}, 'sending') == (
        'Going with MR: 5% shorter than MH for these pages. The call used MH.')
    # Recorded once per attempt: deciding the same attempt again keeps the first row.
    _send(database, tmp_path, pages=frames('shaded_0'), values=values)
    assert coding.attempt_coding(database, ATTEMPT)['id'] == record['id']


def test_shading_to_an_unknown_machine_asks_the_built_in_engine_for_mh_and_leaves_the_ssl_fax_engine_its_own(
        installation, database, tmp_path):  # noqa: F811
    """No DIS on record for the machine (the lead's rules, 2026-10-08): JBIG is not priced, MH (measured smallest)
    prices the call and goes to the built-in engine, and the SSL Fax engine is asked for nothing, so it negotiates
    the most compact coding itself (JBIG where the machine offers it) and Faxbot learns the machine from it."""
    from app.pages import sending
    changed = _send(database, tmp_path, pages=frames('shaded_0'), values=KEEP_SHADING)
    assert sending.unchanged(changed) and changed.coding.coding == 'MH' and changed.coding.measured
    assert (changed.coding.request('hylafax'), changed.coding.request('builtin')) == (None, 'MH')
    record = coding.newest_coding(database, JOB)
    assert (record['requested'], record['measured'], record['compared']) == ('MH', 1, 'MMR')
    assert coding.sent_view(database, JOB)['sentence'] == 'Sent with MH: 20% shorter than MMR for these pages.'
    add_frames(database, ATTEMPT, 'MH')
    view = coding.sent_view(database, JOB)
    assert (view['engine'], view['negotiated']) == ('builtin', 'MH')
    assert view['sentence'] == 'Sent with MH: 20% shorter than MMR for these pages.'


def test_black_text_to_an_unknown_machine_goes_with_the_usual_settings_and_still_says_why(
        installation, database, tmp_path):  # noqa: F811
    # MMR measured smallest and JBIG is left out (no DIS on record): MMR is what both engines take anyway, so
    # nothing goes with the call and the SSL Fax engine keeps its own negotiation (JBIG where the machine offers it).
    assert _send(database, tmp_path, pages=frames('drawn_text'), values=KEEP_SHADING) is None
    view = coding.sent_view(database, JOB)
    assert view['requested'] == 'MMR' and view['measured'] is True
    assert view['sentence'] == 'Sent with MMR: 24% shorter than MR for these pages.'
    add_frames(database, ATTEMPT, 'MMR')
    assert coding.sent_view(database, JOB)['sentence'] == 'Sent with MMR: 24% shorter than MR for these pages.'


def test_a_fax_service_route_measures_nothing_and_records_nothing(installation, database, tmp_path):  # noqa: F811
    assert _send(database, tmp_path, route='sinch', pages=frames('shaded_0'), values=KEEP_SHADING) is None
    assert coding.newest_coding(database, JOB) is None and coding.sent_view(database, JOB) is None


# The SSL Fax engine honours the job's coding ----------------------------------------------------------------

def test_the_engines_job_controls_give_faxsend_the_coding_the_job_asked_for(tmp_path):
    """hylafax/bin/jobcontrol: faxsend's software conversion ignores a job's own desireddf (HylaFAX+ 7.0.11
    faxd/FaxSend.c++), so the job controls hand MH (0), MR (1) or MMR (3) back as DesiredDF; JBIG (4), the
    default, is left to the engine. Run with an explicit PATH and stand-in spool, as faxq runs it."""
    import os
    import shutil
    import subprocess
    root = Path(__file__).resolve().parents[2]
    script = root / 'hylafax' / 'bin' / 'jobcontrol'
    assert os.access(script, os.X_OK)
    assert 'JobControlCmd:\t\t/usr/local/lib/faxbot-engine/jobcontrol' in (root / 'hylafax' / 'entrypoint.sh').read_text()
    tools, spool = tmp_path / 'tools', tmp_path / 'spool'
    (spool / 'sendq').mkdir(parents=True)
    tools.mkdir()
    for tool in ('sh', 'sed', 'tail'):
        (tools / tool).symlink_to(shutil.which(tool))

    def run(job, desired=None):
        if desired is not None:
            (spool / 'sendq' / f'q{job}').write_text(f'jobid:{job}\ndesiredbr:5\ndesireddf:{desired}\ndesiredec:2\n')
        found = subprocess.run([str(tools / 'sh'), str(script), str(job)], cwd=spool, env={'PATH': str(tools)},
                               capture_output=True, text=True, timeout=30)
        assert found.returncode == 0 and found.stderr == '', found
        return found.stdout
    assert run(7, 0) == 'DesiredDF: 0\n'  # MH, as Faxbot asked for these pages
    assert run(8, 1) == 'DesiredDF: 1\n'
    assert run(9, 3) == 'DesiredDF: 3\n'
    assert run(10, 4) == ''  # JBIG: the engine keeps its own choice
    assert run(11, 2) == ''  # MR with uncompressed mode is never asked for
    assert run(12) == ''  # no job file
    assert run('12; rm -rf /') == ''  # not a job ID
