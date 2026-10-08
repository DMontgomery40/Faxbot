"""Dense pages (migration 0028): original pages stacked onto long pages as far as the receiving machine allows,
cut where its limit falls, marked so a receiving Faxbot splits them back; the decision per billing model; what
each machine accepts, parsed from synthetic HylaFAX session logs; blank page bottoms left out for machines
without error correction; standard-resolution documents kept standard. SQLite and PostgreSQL. All synthetic."""
import base64
from contextlib import contextmanager
from datetime import datetime, timedelta
import json
import random
import shutil
from types import SimpleNamespace

from alembic import command
from alembic.config import Config
from PIL import Image, ImageDraw
import pytest
import sqlalchemy as sa

from api.app import schema, schema_dense_pages
from app import conversion, fax_negotiation, hylafax_records
from app.pages import capability, decision, marks, packing, resolution, sending, trim, unpack, views
from app.routing.costs import RateCard
from api.tests.test_schema import database, snapshot  # noqa: F401 - fixture
from api.tests.test_access_schema import at_revision
from api.tests.test_work_schema import without_later_access_changes
from api.tests.test_hylafax_scripts import engine, run  # noqa: F401 - fixture

JOB, ATTEMPT = 'a' * 32, 'b' * 32
NOW = datetime(2026, 10, 7, 9, 0, 0)
PEER = '+15555550199'
LETTER_ROWS = 2156  # 11 inches at 196 lines per inch
FINE = (204.0, 196.0)


def page(seed, height=LETTER_ROWS, width=1728, dpi=FINE, bottom_blank=0):
    """A synthetic page: black marks scattered over it (none in the last ``bottom_blank`` rows)."""
    rng = random.Random(seed)
    image = Image.new('1', (width, height), 1)
    draw = ImageDraw.Draw(image)
    for _ in range(120):
        x, y = rng.randrange(0, width - 40), rng.randrange(0, max(1, height - bottom_blank - 8))
        draw.rectangle((x, y, x + rng.randrange(1, 30), y + rng.randrange(0, 5)), fill=0)
    image.info['dpi'] = dpi
    return image


def same(a, b):
    return a.size == b.size and a.tobytes() == b.tobytes()


def rows(limit, dpi=196):
    return packing.limit_rows(limit, dpi)


# Rendering ----------------------------------------------------------------------------------------------------

def test_the_band_record_round_trips_and_a_damaged_rule_is_ignored():
    record = marks.Record(marks.START, 2, 5, 1728)
    row = marks.rule_row(record, 1728)
    assert marks.read_row(row, 1728) == record
    # One copy of the record damaged: the other copy still reads.
    damaged = bytearray(row)
    damaged[3] = 0x00
    assert marks.read_row(bytes(damaged), 1728) == record
    # Both copies damaged in the same cell: the CRC fails and nothing is read.
    damaged[3 + marks.RECORD_BITS] = 0x00
    damaged[5] = marks.ONE if row[5] == marks.ZERO else marks.ZERO
    damaged[5 + marks.RECORD_BITS] = damaged[5]
    assert marks.read_row(bytes(damaged), 1728) is None
    # An ordinary text row is never a rule.
    assert marks.read_row(page(1).tobytes()[100 * 216:101 * 216], 1728) is None


def test_whole_pages_stack_pixel_for_pixel_with_a_band_and_tag_before_each():
    originals = [page(seed) for seed in range(5)]
    layout = packing.layout_for(originals, 'unlimited')
    # Three letter pages fit one metre (the cap for unlimited); five go on two long pages.
    assert [[piece.original for piece in sheet.pieces] for sheet in layout.sheets] == [[0, 1, 2], [3, 4]]
    sent = packing.render(originals, layout)
    assert [image.size for image in sent] == [(1728, sheet.height()) for sheet in layout.sheets]
    y = packing.TOP_ROWS
    for piece in layout.sheets[0].pieces:
        band = sent[0].crop((0, y, 1728, y + marks.BAND_ROWS))
        record = marks.read_row(band.tobytes()[marks.GAP_TOP * 216:(marks.GAP_TOP + 1) * 216], 1728)
        assert record == marks.Record(marks.START, piece.original + 1, 5, 1728)
        y += marks.BAND_ROWS
        assert same(sent[0].crop((0, y, 1728, y + LETTER_ROWS)), originals[piece.original])
        y += LETTER_ROWS
    assert marks.tag_text(marks.Record(marks.START, 2, 5, 1728)) == 'page 2 of 5'
    # Deterministic: the same pages give the same bytes.
    again = packing.render(originals, packing.layout_for(originals, 'unlimited'))
    assert [image.tobytes() for image in again] == [image.tobytes() for image in sent]


@pytest.mark.parametrize('limit, expected', [('a4', [[0], [1], [2], [3]]), ('b4', [[0], [1], [2], [3]]),
                                              ('unlimited', [[0, 1, 2], [3]])])
def test_letter_pages_are_cut_at_page_boundaries_under_each_limit(limit, expected):
    originals = [page(seed) for seed in range(4)]
    layout = packing.layout_for(originals, limit)
    assert [[piece.original for piece in sheet.pieces] for sheet in layout.sheets] == expected
    assert all(sheet.height() <= rows(limit) for sheet in layout.sheets)
    assert all(piece.top == 0 and piece.rows == LETTER_ROWS for sheet in layout.sheets for piece in sheet.pieces)


@pytest.mark.parametrize('limit, per_sheet', [('a4', 2), ('b4', 3), ('unlimited', 6)])
def test_short_pages_share_a_page_even_under_a4(limit, per_sheet):
    # Receipts about a third of a letter page long: two fit A4 and three B4.
    originals = [page(seed, height=800) for seed in range(6)]
    layout = packing.layout_for(originals, limit)
    assert len(layout.sheets[0].pieces) == per_sheet
    assert all(sheet.height() <= rows(limit) for sheet in layout.sheets)


def test_a_page_longer_than_the_limit_is_split_at_a_white_row_and_marked_continued():
    tall = page(9, height=4000)
    # Text right down to where the A4 limit would cut, and one white gap between lines just above it.
    room = rows('a4') - packing.TOP_ROWS - marks.BAND_ROWS
    draw = ImageDraw.Draw(tall)
    draw.rectangle((0, room - packing.CUT_SEARCH_ROWS - 4, 12, room + 4), fill=0)
    draw.rectangle((0, room - 41, 1727, room - 39), fill=1)
    layout = packing.layout_for([page(1), tall, page(2, height=500)], 'a4')
    pieces = [(piece.original, piece.top, piece.rows, piece.kind) for sheet in layout.sheets for piece in sheet.pieces]
    assert pieces[0] == (0, 0, LETTER_ROWS, marks.START)
    # The cut falls on the white gap, not through a line of text.
    assert pieces[1][:2] == (1, 0) and pieces[1][3] == marks.START and pieces[1][2] == room - 39
    assert pieces[2][0] == 1 and pieces[2][3] == marks.CONTINUES and pieces[1][2] + pieces[2][2] == 4000
    # The last original fits under the rest of the long page.
    assert layout.sheets[-1].pieces[-1].original == 2
    assert all(sheet.height() <= rows('a4') for sheet in layout.sheets)


def test_pages_that_cannot_be_marked_or_resolved_are_not_packed():
    with pytest.raises(packing.NotPackable):
        packing.layout_for([page(1, width=600)], 'unlimited')
    with pytest.raises(packing.NotPackable):
        packing.layout_for([page(1), page(2, dpi=(204.0, 98.0))], 'unlimited')


# Receiving: split back into the originals ------------------------------------------------------------------------

def test_a_receiving_faxbot_splits_long_pages_back_into_the_original_pages_exactly():
    originals = [page(1), page(2, width=1216), page(3, height=4000), page(4, height=700)]
    sent = packing.render([*originals[:1], _padded(originals[1]), *originals[2:]], packing.layout_for(
        [*originals[:1], _padded(originals[1]), *originals[2:]], 'a4'))
    # The sending engine's header line: HylaFAX writes over the top rows of each page, spandsp adds rows above it.
    received = []
    for number, image in enumerate(sent):
        header = Image.new('1', (1728, 32), 0 if number % 2 else 1)
        copy = Image.new('1', (1728, image.height + 32), 1)
        copy.paste(header, (0, 0))
        copy.paste(image, (0, 32))
        received.append(copy)
    back = unpack.split_frames(received)
    assert back is not None and len(back) == 4
    assert same(back[0], originals[0]) and same(back[2], originals[2]) and same(back[3], originals[3])
    # A narrower original comes back at its own width.
    assert same(back[1], originals[1])


def _padded(frame):
    # render() pads by itself; this only keeps the narrower page's own width in its band record.
    return frame


def test_an_ordinary_fax_and_an_incomplete_packed_fax_are_delivered_as_received():
    assert unpack.split_frames([page(1), page(2)]) is None
    originals = [page(seed) for seed in range(5)]
    sent = packing.render(originals, packing.layout_for(originals, 'unlimited'))
    # A page lost on the way: Faxbot cannot be sure of the originals, so it keeps the fax whole.
    assert unpack.split_frames(sent[:1]) is None
    assert unpack.split_frames([sent[0], page(7)]) is None


def test_the_fax_image_files_round_trip_and_the_received_image_is_never_changed(tmp_path):
    originals = [page(seed) for seed in range(5)]
    conversion.write_fax_tiff(originals, str(tmp_path / 'fax.tiff'))
    assert conversion.pack_fax_image(str(tmp_path / 'fax.tiff'), str(tmp_path / 'packed.tiff'), 'unlimited') == (5, 2)
    # Under A4 letter pages cannot share a page: nothing is written.
    assert conversion.pack_fax_image(str(tmp_path / 'fax.tiff'), str(tmp_path / 'a4.tiff'), 'a4') is None
    assert not (tmp_path / 'a4.tiff').exists()
    with Image.open(tmp_path / 'packed.tiff') as image:
        # What fax engines expect: Group 4, white is zero, fine resolution.
        assert image.tag_v2[259] == 4 and image.tag_v2[262] == 0 and image.info['dpi'] == FINE
    received = (tmp_path / 'packed.tiff').read_bytes()
    assert conversion.split_received_image(str(tmp_path / 'packed.tiff'), str(tmp_path / 'split.tiff')) == 5
    assert (tmp_path / 'packed.tiff').read_bytes() == received
    back = conversion.read_fax_frames(str(tmp_path / 'split.tiff'))
    assert all(same(a, b) for a, b in zip(back, originals))
    assert conversion.split_received_image(str(tmp_path / 'fax.tiff'), str(tmp_path / 'none.tiff')) is None


# The decision, per billing model --------------------------------------------------------------------------------

def card(provider, *, per_page='0', per_minute='0', increment=60, minimum=0, monthly=None):
    from app.routing.costs import parse_amount
    return RateCard(None, provider, 'outbound', provider, 'USD', parse_amount(per_minute), parse_amount(per_page), 0,
                    increment, minimum, None, NOW, parse_amount(monthly) if monthly else None)


NORMAL = decision.Shape(5, (200_000,) * 5, 'fine', 'normal')
DENSE = decision.Shape(2, (601_200, 400_800), 'fine', 'dense')


def stand_in(rates=None):
    """The stand-in predictor on ``rates``: these tests pin its billing rules, whichever predictor is installed."""
    return lambda route_key, destination, shape: decision.stand_in_predict(route_key, destination, shape, card=rates)


def test_per_page_routes_pack_for_fewer_billed_pages():
    made = decision.decide('sinch', PEER, NORMAL, DENSE, predict=stand_in(card('sinch', per_page='0.045')))
    assert made.pack and made.billing == 'per_page'
    assert (made.normal.billed_pages, made.dense.billed_pages) == (5, 2)
    assert made.normal.cost.micros - made.dense.cost.micros == 3 * 45_000


def test_per_minute_routes_pack_for_fewer_page_exchanges_even_when_minutes_round_the_same():
    made = decision.decide('sip', PEER, NORMAL, DENSE, predict=stand_in(card('sip-telnyx', per_minute='0.005')))
    assert made.pack and made.billing == 'per_minute' and made.dense.billed_pages == 0
    # Three page boundaries fewer, about three seconds each, less the bands' few bits.
    assert made.seconds_saved == 8
    measured = decision.Shape(5, (200_000,) * 5, 'fine', 'normal', boundary_seconds=4.5)
    assert decision.decide('sip', PEER, measured, decision.Shape(2, (601_200, 400_800), 'fine', 'dense',
                                                                  boundary_seconds=4.5),
                           predict=stand_in(card('sip-telnyx', per_minute='0.005'))).seconds_saved == 13


def test_a_flat_plan_packs_for_room_under_the_plan_but_saves_no_money():
    made = decision.decide('humblefax', PEER, NORMAL, DENSE, predict=stand_in(card('humblefax', monthly='10.00')))
    assert made.pack and made.billing == 'plan' and made.dense.marginal
    assert made.normal.cost.micros == made.dense.cost.micros == 0


def test_no_card_keeps_the_cost_unknown_and_never_packs_for_more_pages_or_time():
    made = decision.decide('documo', PEER, NORMAL, DENSE, predict=stand_in())
    assert made.pack and made.billing == 'unpriced' and made.dense.cost is None and made.dense.billed_pages is None
    assert not decision.decide('sinch', PEER, NORMAL, decision.Shape(5, None, 'fine', 'dense'),
                               predict=stand_in(card('sinch', per_page='0.045'))).pack
    # More data on the long pages than the page boundaries save: the call would be longer.
    slower = decision.Shape(2, (5_000_000, 5_000_000), 'fine', 'dense')
    assert not decision.decide('sip', PEER, NORMAL, slower,
                               predict=stand_in(card('sip-telnyx', per_minute='0.005'))).pack


def test_the_shared_predictor_is_used_when_given():
    calls = []

    def predict(route_key, destination, shape):
        calls.append((route_key, destination, shape.layout))
        return decision.Prediction(shape.pages, 60.0, None, 'per_page', False)
    made = decision.decide('sinch', PEER, NORMAL, DENSE, predict=predict)
    assert made.pack and calls == [('sinch', PEER, 'normal'), ('sinch', PEER, 'dense')]


def test_the_layout_chooser_keeps_exactly_one_layout_the_cheapest():
    originals = [page(seed) for seed in range(5)]
    sinch = card('sinch', per_page='0.045')
    chosen = conversion.choose_layout(originals, route='sinch', destination=PEER, limit='unlimited',
                                      dense_allowed=True, card=sinch, describe_dense=lambda a, b: f'{b} of {a}')
    assert chosen['layout'] == 'dense' and len(chosen['pages']) == 2 and chosen['reason'] == '2 of 5'
    assert set(chosen['predictions']) == {'normal', 'dense'} and chosen['codec'] is None  # no codec candidate
    # A codec that sends fewer pages wins alone: its pages are made from the originals, never from packed pages.
    seen = []

    def codec(pages):
        seen.append(len(pages))
        return [page(99, height=500)], 'Sent as 1 encoded page instead of 5.'
    chosen = conversion.choose_layout(originals, route='sinch', destination=PEER, limit='unlimited',
                                      dense_allowed=True, codec=codec, card=sinch)
    assert seen == [5] and chosen['layout'] == 'codec' and len(chosen['pages']) == 1
    assert chosen['reason'] == 'Sent as 1 encoded page instead of 5.'
    # Encoded pages that cost more than the long pages are priced and left out: the long pages go alone.
    chosen = conversion.choose_layout(originals, route='sinch', destination=PEER, limit='unlimited',
                                      dense_allowed=True, card=sinch,
                                      codec=lambda pages: ([page(97), page(96), page(95)], 'Three encoded pages.'))
    assert chosen['layout'] == 'dense' and set(chosen['predictions']) == {'normal', 'dense', 'codec'}
    assert chosen['codec'] is None and len(chosen['pages']) == 2
    # Not allowed, or no saving under the limit: the pages go as they are.
    assert conversion.choose_layout(originals, route='sinch', destination=PEER, limit='unlimited',
                                    dense_allowed=False, card=sinch)['layout'] == 'normal'
    assert conversion.choose_layout(originals, route='sinch', destination=PEER, limit='a4',
                                    dense_allowed=True, card=sinch)['layout'] == 'normal'


def test_on_a_full_tie_the_simpler_layout_wins():
    def same_price(route_key, destination, shape):
        return decision.Prediction(0, 60.0, None, 'per_minute', False)

    def codec(pages):
        return list(pages), 'codec'
    originals = [page(seed) for seed in range(5)]
    chosen = conversion.choose_layout(originals, route='sip', destination=PEER, limit='unlimited',
                                      dense_allowed=True, codec=codec, predict=same_price)
    # Same cost and the same time: pages billed decide (none here), then normal before dense before codec.
    assert chosen['layout'] == 'normal'
    assert decision.rank(decision.Prediction(0, 60.4, None, 'per_minute', False),
                         decision.Shape(2, None, 'fine', 'dense')) < decision.rank(
        decision.Prediction(0, 60.9, None, 'per_minute', False), decision.Shape(2, None, 'fine', 'codec'))


def test_page_or_time_routes_bill_a_slow_long_page_as_more_than_one():
    slow = decision.Shape(1, (14_400 * 125,), 'fine', 'dense')
    assert decision.stand_in_predict('efax', PEER, slow, card=card('efax', per_page='0.10')).billed_pages == 3


# What each machine accepts: the session log, the record, the default -------------------------------------------

SENT_LOG = '\n'.join([
    'Oct 07 10:00:00.00: [  200]: SESSION BEGIN 000000021 15555550199',
    'Oct 07 10:00:01.00: [  200]: REMOTE best rate 14400 bit/s',
    'Oct 07 10:00:01.00: [  200]: REMOTE max A4 page width (215 mm)',
    'Oct 07 10:00:01.00: [  200]: REMOTE max unlimited page length',
    'Oct 07 10:00:01.00: [  200]: REMOTE best vres 7.7 line/mm',
    'Oct 07 10:00:01.00: [  200]: REMOTE best 20 ms, 10 ms/scanline',
    'Oct 07 10:00:02.00: [  200]: SEND training at v.17 14400 bit/s',
    'Oct 07 10:00:03.00: [  200]: TRAINING succeeded',
    'Oct 07 10:00:03.10: [  200]: SEND begin page',
    'Oct 07 10:00:13.00: [  200]: SEND end page',
    'Oct 07 10:00:13.10: [  200]: SEND send MPS (more pages, same document)',
    'Oct 07 10:00:14.40: [  200]: SEND recv MCF (message confirmation)',
    'Oct 07 10:00:16.40: [  200]: SEND begin page',
    'Oct 07 10:00:26.00: [  200]: SEND end page',
    'Oct 07 10:00:29.80: [  200]: SEND begin page',
    'Oct 07 10:00:39.00: [  200]: SEND end page',
    'Oct 07 10:01:00.00: [  200]: SESSION END']) + '\n'


def test_the_session_log_gives_the_receiving_machines_limits_and_the_time_between_pages(engine):
    spool, _, _, environment = engine
    path = spool / 'log' / 'c000000021'
    path.write_text(SENT_LOG)
    result = run('negotiation', environment, str(path))
    assert result.returncode == 0, result.stderr
    report = json.loads(base64.b64decode(result.stdout))
    assert {key: report[key] for key in ('page_length', 'page_width', 'fine', 'remote_ecm', 'scan_ms',
                                         'boundary_ms', 'boundaries')} == {
        'page_length': 'unlimited', 'page_width': 'A4', 'fine': 1, 'remote_ecm': 0, 'scan_ms': 10,
        'boundary_ms': 3600, 'boundaries': 2}
    assert fax_negotiation.page_capability(result.stdout) == {
        'max_length': 'unlimited', 'max_width': 'a4', 'fine': 1, 'ecm': 0, 'scan_ms': 10, 'boundary_ms': 3600,
        'boundaries': 2}
    # Error correction offered, and a log without the DIS reports nothing about the machine.
    path.write_text(SENT_LOG.replace('REMOTE best rate 14400 bit/s', 'REMOTE supports T.30 Annex A, 256-byte ECM'))
    assert fax_negotiation.page_capability(run('negotiation', environment, str(path)).stdout)['ecm'] == 1
    path.write_text('\n'.join(line for line in SENT_LOG.splitlines() if 'REMOTE max' not in line) + '\n')
    assert fax_negotiation.page_capability(run('negotiation', environment, str(path)).stdout) == {}


def test_anything_but_a_known_page_length_teaches_nothing():
    def encoded(value):
        return base64.b64encode(json.dumps(value).encode()).decode()
    assert fax_negotiation.page_capability(encoded({'page_length': 'A5'})) == {}
    assert fax_negotiation.page_capability('not base64') == {}
    assert fax_negotiation.page_capability(encoded({'page_length': 'B4', 'fine': True, 'boundary_ms': 900})) == {
        'max_length': 'b4'}


@pytest.fixture
def installation(database):
    schema.upgrade_schema(database)
    return capability.records_for(database)


def test_unknown_capability_falls_back_to_a4_and_never_trims(installation):
    known = installation.capability(PEER)
    assert (known.limit, known.learned, known.ecm) == ('a4', False, None)
    assert installation.capability('not a number').limit == 'a4'


def test_each_sent_call_adds_one_observation_and_the_newest_one_counts(installation, database):
    records = hylafax_records.records_for(database)
    first = {'max_length': 'b4', 'max_width': 'a4', 'fine': 1, 'ecm': 1, 'scan_ms': 0, 'boundary_ms': 3000,
             'boundaries': 4}
    records.record_page_capability(call_key=ATTEMPT, values=first, job_id=JOB, number=PEER, now=NOW)
    records.record_page_capability(call_key=ATTEMPT, values={**first, 'max_length': 'a4'}, number=PEER, now=NOW)
    later = {'max_length': 'unlimited', 'ecm': 0, 'scan_ms': 10, 'boundary_ms': 4000, 'boundaries': 1}
    records.record_page_capability(call_key='c' * 32, values=later, number=PEER, now=NOW + timedelta(hours=1))
    table = sa.Table('page_capability_observations', sa.MetaData(), autoload_with=database)
    with database.connect() as connection:
        assert connection.execute(sa.select(sa.func.count()).select_from(table)).scalar() == 2
    known = installation.capability(PEER)
    assert (known.limit, known.ecm, known.scan_ms, known.learned_at) == ('unlimited', False, 10,
                                                                        NOW + timedelta(hours=1))
    assert known.boundary_seconds == 3.5  # the median of the calls that measured it


def test_settings_default_on_and_the_rules_engine_can_ask(installation, database):
    assert installation.recipient_settings(PEER) == {'packing': 'allow', 'trim_blank': None}
    assert capability.long_pages_allowed(database, 'sip', PEER) == (True, 'route')
    # Cloud routes stay off until someone checks the provider, a route that fetches its PDF from Faxbot included
    # (it fetches the attempt's own pages); a rule may turn them on.
    assert capability.long_pages_allowed(database, 'sinch', PEER) == (False, 'route_off')
    assert capability.long_pages_allowed(database, 'sinch', PEER, rule='allow') == (True, 'rule')
    assert capability.long_pages_allowed(database, 'phaxio', PEER) == (False, 'route_off')
    assert capability.long_pages_allowed(database, 'phaxio', PEER, rule='allow') == (True, 'rule')
    installation.set_route_settings('sinch', long_pages=True, actor='person-1')
    assert capability.long_pages_allowed(database, 'sinch', PEER) == (True, 'route')
    installation.set_recipient_settings(PEER, packing='never', actor='person-1')
    assert capability.long_pages_allowed(database, 'sip', PEER) == (False, 'recipient')
    assert capability.long_pages_allowed(database, 'sip', '+15555550100', rule='never') == (False, 'rule')
    # Trimming: on for the installation unless turned off; a number's own choice wins.
    assert installation.trim_allowed(PEER) is True
    installation.set_route_settings('sip', trim_blank=False)
    assert installation.trim_allowed(PEER) is False
    installation.set_recipient_settings(PEER, trim_blank=True)
    assert installation.trim_allowed(PEER) is True and installation.recipient_packing(PEER) == 'never'
    installation.set_recipient_settings(PEER, packing='allow', trim_blank=None)
    table = sa.Table('recipient_page_settings', sa.MetaData(), autoload_with=database)
    with database.connect() as connection:
        assert connection.execute(sa.select(sa.func.count()).select_from(table)).scalar() == 0
    assert installation.set_route_settings('phaxio', long_pages=True)['long_pages'] is True
    with pytest.raises(ValueError):
        installation.set_route_settings('not a route', long_pages=True)


# Lossless resolution matching -----------------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which('gs') is None, reason='Ghostscript renders PDF pages')
def test_a_standard_fax_turned_into_a_pdf_and_back_is_the_same_standard_raster(tmp_path):
    standard = [page(seed, height=1078, dpi=(204.0, 98.0)) for seed in range(2)]
    conversion.write_fax_tiff(standard, str(tmp_path / 'received.tiff'))
    conversion.tiff_to_pdf(str(tmp_path / 'received.tiff'), str(tmp_path / 'forward.pdf'))
    conversion.pdf_to_tiff(str(tmp_path / 'forward.pdf'), str(tmp_path / 'send.tiff'), match_resolution=True)
    back = conversion.read_fax_frames(str(tmp_path / 'send.tiff'))
    assert [frame.info['dpi'] for frame in back] == [(204.0, 98.0)] * 2
    assert all(same(a, b) for a, b in zip(back, standard))
    assert conversion.fax_image_resolution(str(tmp_path / 'send.tiff')) == 'standard'
    # An accepted fax's own image stays fine: faxes sent together share one call (batching/image.py).
    conversion.pdf_to_tiff(str(tmp_path / 'forward.pdf'), str(tmp_path / 'accepted.tiff'))
    assert conversion.fax_image_resolution(str(tmp_path / 'accepted.tiff')) == 'fine'


@pytest.mark.skipif(shutil.which('gs') is None, reason='Ghostscript renders PDF pages')
def test_a_forwarded_standard_fax_goes_at_standard_resolution_on_its_own_call(installation, database, tmp_path):
    standard = [page(seed, height=1078, dpi=(204.0, 98.0)) for seed in range(2)]
    conversion.write_fax_tiff(standard, str(tmp_path / 'received.tiff'))
    conversion.tiff_to_pdf(str(tmp_path / 'received.tiff'), str(tmp_path / f'{JOB}.pdf'))
    conversion.pdf_to_tiff(str(tmp_path / f'{JOB}.pdf'), str(tmp_path / f'{JOB}.tiff'))
    configuration = SimpleNamespace(provider_id='sip', manifest=None, traits={'requires_tiff': True})
    claim = SimpleNamespace(job_id=JOB, attempt_id=ATTEMPT, members=())
    changed = sending.prepare(database, SimpleNamespace(sip_fax_fine=True), configuration, claim, {'to_number': PEER},
                              tmp_path / f'{JOB}.pdf', tmp_path / f'{JOB}.tiff', now=NOW)
    assert (changed.original_pages, changed.sent_pages) == (2, 2)
    assert all(same(a, b) for a, b in zip(conversion.read_fax_frames(changed.tiff), standard))
    view = views.sent_view(database, JOB)
    assert view['sentences'] == ['Sent at standard resolution, as received.'] and view['seconds_saved'] >= 0
    # Faxes sent together are left alone: their shared call keeps one resolution.
    assert sending.prepare(database, SimpleNamespace(sip_fax_fine=True), configuration,
                           SimpleNamespace(job_id=JOB, attempt_id='c' * 32, members=('x',)), {'to_number': PEER},
                           tmp_path / f'{JOB}.pdf', tmp_path / f'{JOB}.tiff') is None


def test_what_the_engine_reports_after_a_send_is_found_for_the_next_send_to_that_number(installation, database):
    """The engine's result names the number as the call record holds it; the next send looks it up by the
    fax's canonical number. Both are the same string."""
    from app import hylafax_http, sip_calls
    calls = sip_calls.SipCallRecords(database)
    calls.record_submission({'JobID': JOB, 'AttemptID': ATTEMPT, 'Called': PEER, 'CallerID': '+15555550100'}, now=NOW)
    row = calls.for_attempt(ATTEMPT)[-1]
    report = base64.b64encode(json.dumps({'page_length': 'unlimited', 'page_width': 'A4', 'fine': 1, 'remote_ecm': 1,
                                          'scan_ms': 0, 'boundary_ms': 3100, 'boundaries': 3}).encode()).decode()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(configuration_runtime=SimpleNamespace(
        manager=SimpleNamespace(store=SimpleNamespace(engine=database))))))
    hylafax_http._record_engine(request, JOB, ATTEMPT, {'engine_id': '0123456789abcdef', 'commid': '000000031',
                                                        'why': 'done', 'pages': 3, 'negotiation_b64': report}, row)
    known = installation.capability(PEER)
    assert (known.limit, known.learned, known.ecm, known.boundary_seconds) == ('unlimited', True, True, 3.1)


def test_a_mixed_document_and_a_true_fine_page_stay_fine():
    doubled = Image.frombytes('1', (1728, 2156), b''.join(
        row * 2 for row in _rows(page(1, height=1078))))
    doubled.info['dpi'] = FINE
    assert resolution.standard_frames([doubled]) is not None
    # One really fine page keeps the whole document fine; a fine page alone is untouched.
    assert resolution.standard_frames([doubled, page(2)]) is None
    assert resolution.standard_frames([page(2)]) is None


def _rows(frame):
    data, stride = frame.tobytes(), (frame.width + 7) // 8
    return [data[row * stride:(row + 1) * stride] for row in range(frame.height)]


# Blank page bottoms ---------------------------------------------------------------------------------------------

def test_the_trim_keeps_every_non_blank_row_and_every_page_in_order():
    full = page(2)
    ImageDraw.Draw(full).line((0, full.height - 1, 1727, full.height - 1), fill=0)
    pages = [page(1, bottom_blank=1200), full, page(3, bottom_blank=900)]
    trimmed, count, dropped = trim.trim_frames(pages, [True, True, True])
    assert len(trimmed) == 3 and count == 2 and dropped > 2000
    for before, after in zip(pages, trimmed):
        kept = after.height
        assert _rows(before)[:kept] == _rows(after)
        assert all(row == b'\xff' * 216 for row in _rows(before)[kept:])
        assert trim.trailing_white_rows(after) <= trim.MARGIN_ROWS or after.height == before.height
    # A page that may not be trimmed (an image someone sent) stays exactly as it is.
    untouched, count, _ = trim.trim_frames(pages, [False, False, False])
    assert count == 0 and all(same(a, b) for a, b in zip(untouched, pages))


def test_only_pages_drawn_from_text_or_shapes_may_be_trimmed(tmp_path):
    conversion.write_fax_tiff([page(1)], str(tmp_path / 'scan.tiff'))
    conversion.tiff_to_pdf(str(tmp_path / 'scan.tiff'), str(tmp_path / 'scan.pdf'))
    (tmp_path / 'note.txt').write_text('Hello\n')
    conversion.txt_to_pdf(str(tmp_path / 'note.txt'), str(tmp_path / 'note.pdf'))
    assert trim.rendered_pages(str(tmp_path / 'scan.pdf')) == [False]
    assert trim.rendered_pages(str(tmp_path / 'note.pdf')) == [True]


# The send-time hook ---------------------------------------------------------------------------------------------

def _send(database, tmp_path, *, route='sip', pages=None, number=PEER, values=None, recipient=None, attempt=ATTEMPT):
    pages = pages or [page(seed) for seed in range(5)]
    pdf, tiff = tmp_path / f'{JOB}.pdf', tmp_path / f'{JOB}.tiff'
    conversion.write_fax_tiff(pages, str(tiff))
    conversion.tiff_to_pdf(str(tiff), str(pdf))
    configuration = SimpleNamespace(provider_id=route, manifest=None,
                                    traits={'requires_tiff': route in ('sip', 'freeswitch')})
    claim = SimpleNamespace(job_id=JOB, attempt_id=attempt, members=())
    job = {'to_number': number, **({'recipient_number': recipient} if recipient else {})}
    return sending.prepare(database, values or SimpleNamespace(sip_fax_fine=True), configuration, claim,
                           job, pdf, tiff if route in ('sip', 'freeswitch') else None, now=NOW)


def _learn(records, **values):
    records.record_observation(PEER, source='d' * 32, engine='hylafax',
                               values={'max_length': 'unlimited', 'ecm': 1, 'fine': 1, **values}, now=NOW)


def test_a_trunk_send_to_an_unlimited_machine_goes_packed_and_says_so(installation, database, tmp_path):
    _learn(installation)
    changed = _send(database, tmp_path)
    assert (changed.original_pages, changed.sent_pages, changed.pdf) == (5, 2, None)
    assert changed.tiff.endswith(f'packed-{JOB}-{ATTEMPT}.tiff')
    # The fax's own image is unchanged; the sent image splits back into it.
    assert len(conversion.read_fax_frames(str(tmp_path / f'{JOB}.tiff'))) == 5
    assert len(unpack.split_frames(conversion.read_fax_frames(changed.tiff))) == 5
    view = views.sent_view(database, JOB, str(tmp_path))
    assert view['sentences'] == ['Sent as 2 long pages instead of 5; the receiving machine accepts unlimited length.']
    assert view['layout'] == 'dense' and view['pages_saved'] == 3 and 5 <= view['seconds_saved'] <= 9
    # The attempt carries the pages it actually sent, for costing (fax_page_changes.sent_pages).
    table = sa.Table('fax_page_changes', sa.MetaData(), autoload_with=database)
    with database.connect() as connection:
        row = connection.execute(sa.select(table).where(table.c.attempt_id == ATTEMPT)).mappings().one()
    assert (row['layout'], row['sent_pages'], row['original_pages']) == ('dense', 2, 5)
    assert row['reason'] == view['sentences'][0]


def test_a_send_the_codec_makes_cheapest_records_the_codec_alone(installation, database, tmp_path, monkeypatch):
    _learn(installation)
    opt_in(database, PEER)  # the codec is a candidate only for a number whose recipient agreed
    monkeypatch.setattr(conversion, 'codec_pages', lambda pages, **_: (
        [page(98, height=600)], 'Sent as 1 encoded page instead of 5; the receiving Faxbot decodes it.'))
    changed = _send(database, tmp_path)
    assert (changed.original_pages, changed.sent_pages) == (5, 1)
    assert len(conversion.read_fax_frames(changed.tiff)) == 1
    view = views.sent_view(database, JOB)
    assert view['layout'] == 'codec'
    # The record keeps the codec's own sentence; the Sent detail words the layout for the attempt's state.
    assert _change(database)['reason'] == 'Sent as 1 encoded page instead of 5; the receiving Faxbot decodes it.'
    assert view['sentences'] == ['Sent as 1 encoded page instead of 5 (experimental).']


def test_nothing_changes_when_unknown_refused_or_not_worth_it(installation, database, tmp_path):
    # Unknown machine: A4, where letter pages cannot share a page.
    # The pages go as they are (the coding measured for them may still go with the call: pages/coding.py).
    assert sending.unchanged(_send(database, tmp_path))
    _learn(installation)
    installation.set_recipient_settings(PEER, packing='never')
    assert sending.unchanged(_send(database, tmp_path))
    installation.set_recipient_settings(PEER, packing='allow')
    # The trunk sends standard resolution: the SSL Fax engine would draw fine pages again.
    assert sending.unchanged(_send(database, tmp_path, values=SimpleNamespace(sip_fax_fine=False)))
    # Faxes sent together are left for batching.
    assert sending.prepare(database, SimpleNamespace(), SimpleNamespace(provider_id='sip', manifest=None, traits={}),
                           SimpleNamespace(job_id=JOB, attempt_id=ATTEMPT, members=('x',)), {'to_number': PEER},
                           tmp_path / 'x.pdf', None) is None
    assert not [path for path in tmp_path.glob('packed-*') if not path.name.startswith('packed-friendly-')
                and not path.name.endswith('.coding.json')]


def _fax_row(database, route='sip', pages=5):
    jobs = sa.Table('fax_jobs', sa.MetaData(), autoload_with=database)
    with database.begin() as connection:
        connection.execute(jobs.insert().values(**_filled(jobs, {
            'id': JOB, 'to_number': PEER, 'status': 'queued', 'created_at': NOW, 'updated_at': NOW, 'pages': pages,
            'backend': route})))


def _new_attempt(database, attempt_id, sequence):
    attempts = sa.Table('outbound_attempts', sa.MetaData(), autoload_with=database)
    with database.begin() as connection:
        connection.execute(attempts.insert().values(**_filled(attempts, {
            'id': attempt_id, 'job_id': JOB, 'sequence': sequence, 'phase': 'prepared', 'created_at': NOW})))


def _phase(database, attempt_id, phase):
    attempts = sa.Table('outbound_attempts', sa.MetaData(), autoload_with=database)
    with database.begin() as connection:
        connection.execute(attempts.update().where(attempts.c.id == attempt_id).values(phase=phase))


def opt_in(database, *numbers):
    """The recipient of each number agreed to encoded pages (codec/store.py)."""
    from app.codec.store import CodecSettings
    for number in numbers:
        CodecSettings(database).save(number, enabled=True, recipient_agreed=True, actor='principal:synthetic', now=NOW)


@contextmanager
def priced(**cards):
    """The real shared predictor (routing/predict.py) on synthetic facts: one rate card per route, nothing learned."""
    from app.routing import predict
    from app.routing.costs import RateTerms
    from app.routing.destinations import LOCAL, DestinationClass
    facts = {route: predict.RouteFacts(route, route.title(), DestinationClass(LOCAL, 'US', '+1', PEER),
                                       RateTerms(rates))
             for route, rates in cards.items()}
    with predict.facts_source(lambda route, destination, now=None: facts[route]):
        yield


def _change(database, attempt_id=ATTEMPT):
    table = sa.Table('fax_page_changes', sa.MetaData(), autoload_with=database)
    with database.connect() as connection:
        row = connection.execute(sa.select(table).where(table.c.attempt_id == attempt_id)).mappings().first()
    return dict(row) if row is not None else None


def test_encoded_pages_win_alone_when_they_cost_least_and_are_never_packed_or_trimmed(installation, database,
                                                                                      tmp_path):
    """The real codec and the real shared predictor: on a per-page route, one encoded page beats two long pages."""
    from app import codec
    from app.codec.store import send_for
    if not shutil.which('gs'):
        pytest.skip('Ghostscript renders the PDF for a cloud route')
    from app.routing.store import RouteStore
    _learn(installation)  # takes unlimited length: two long pages are possible
    installation.set_route_settings('sinch', long_pages=True)
    RouteStore(database, sip_preset=lambda: '').replace_cards([card('sinch', per_page='0.045')])
    _fax_row(database, 'sinch')
    opt_in(database, PEER)
    with priced(sinch=card('sinch', per_page='0.045')):
        changed = _send(database, tmp_path, route='sinch')
    assert (changed.original_pages, changed.sent_pages, changed.trimmed_pages) == (5, 1, 0)
    # What Sinch gets is the encoded page itself, which turns back into exactly the fax's own PDF.
    document, _ = codec.decode_images(codec.read_images(changed.pdf))
    assert document.data == (tmp_path / f'{JOB}.pdf').read_bytes()
    row = _change(database)
    assert (row['layout'], row['sent_pages'], row['original_pages'], row['billing']) == ('codec', 1, 5, 'per_page')
    assert row['trimmed_pages'] is None and row['resolution'] is None and row['page_limit'] is None
    details = send_for(database, JOB)
    assert (details['provider_id'], details['pages_encoded'], details['layout']) == ('sinch', 1, 'grid')
    assert details['cost_encoded_micros'] < details['cost_original_micros']
    # One sentence on the Sent detail, for the layout the attempt kept.
    assert views.sent_view(database, JOB)['sentences'] == ['Sent as 1 encoded page instead of 5 (experimental).']


def test_dense_pages_win_when_encoded_ones_would_cost_more_and_nothing_is_encoded(installation, database, tmp_path):
    """Per minute, the encoded page carries more bits than the two long pages: dense pages go, never both."""
    from app.codec.store import send_for
    _learn(installation)
    _fax_row(database)
    opt_in(database, PEER)
    with priced(sip=card('sip', per_minute='0.005', increment=1)):
        changed = _send(database, tmp_path)
    assert (changed.original_pages, changed.sent_pages) == (5, 2)
    assert len(unpack.split_frames(conversion.read_fax_frames(changed.tiff))) == 5  # long pages, not encoded ones
    assert _change(database)['layout'] == 'dense' and send_for(database, JOB) is None


def test_a_number_that_did_not_agree_never_gets_encoded_pages(installation, database, tmp_path):
    toll_free = '+18005550199'
    _fax_row(database, 'sinch')
    if not shutil.which('gs'):
        pytest.skip('Ghostscript renders the PDF for a cloud route')
    with priced(sinch=card('sinch', per_page='0.045')):
        assert _send(database, tmp_path, route='sinch') is None  # nobody agreed: nothing is even drawn
        # An approved toll-free number dialed for a recipient who agreed: both must agree.
        opt_in(database, PEER)
        assert _send(database, tmp_path, route='sinch', number=toll_free, recipient=PEER) is None
        opt_in(database, toll_free)
        changed = _send(database, tmp_path, route='sinch', number=toll_free, recipient=PEER)
    assert changed.sent_pages == 1 and _change(database)['layout'] == 'codec'


def test_a_shared_key_that_cannot_be_read_never_sends_the_document_unencrypted(installation, database, tmp_path,
                                                                                 monkeypatch):
    from app.codec import send as codec_send
    _learn(installation)
    _fax_row(database)
    opt_in(database, PEER)
    real = codec_send.CodecSettings.get
    monkeypatch.setattr(codec_send.CodecSettings, 'get', lambda self, number: {**real(self, number), 'has_key': True})
    with priced(sip=card('sip', per_page='0.045')):
        changed = _send(database, tmp_path)  # no key seal: the key cannot be opened
    assert changed.sent_pages == 2 and _change(database)['layout'] == 'dense'


def test_a_retry_onto_another_route_decides_again_and_the_sent_detail_follows_it(installation, database, tmp_path):
    from app.codec.store import send_for
    if not shutil.which('gs'):
        pytest.skip('Ghostscript renders the PDF for a cloud route')
    first, second = '1' * 32, '2' * 32
    _learn(installation)
    _fax_row(database, 'sinch')
    opt_in(database, PEER)
    _new_attempt(database, first, 1)
    with priced(sinch=card('sinch', per_page='0.045'), sip=card('sip', per_minute='0.005', increment=1)):
        assert _send(database, tmp_path, route='sinch', attempt=first).sent_pages == 1  # encoded, on Sinch
        # One sentence for each state of the attempt.
        assert views.sent_view(database, JOB)['sentences'] == ['Going as 1 encoded page instead of 5 (experimental).']
        _phase(database, first, 'failed')
        assert views.sent_view(database, JOB)['sentences'] == [
            'Tried as 1 encoded page instead of 5; the call failed.']
        _new_attempt(database, second, 2)
        retried = _send(database, tmp_path, attempt=second)  # the same fax over the phone line, billed by time
    assert (retried.original_pages, retried.sent_pages) == (5, 2)
    assert (_change(database, first)['layout'], _change(database, second)['layout']) == ('codec', 'dense')
    # The codec's details stay those of the first attempt; the Sent detail follows the second.
    assert send_for(database, JOB)['provider_id'] == 'sinch'
    assert views.sent_view(database, JOB)['sentences'] == [
        'Going as 2 long pages instead of 5; the receiving machine accepts unlimited length.']
    _phase(database, second, 'success')
    assert views.sent_view(database, JOB)['sentences'] == [
        'Sent as 2 long pages instead of 5; the receiving machine accepts unlimited length.']
    _phase(database, second, 'failed')
    assert views.sent_view(database, JOB)['sentences'] == ['Tried as 2 long pages instead of 5; the call failed.']
    _phase(database, second, 'cancelled')
    assert views.sent_view(database, JOB)['sentences'] == [
        'Prepared as 2 long pages instead of 5; the fax was cancelled.']


def test_encoded_pages_are_never_lightened(installation, database, tmp_path, monkeypatch):
    """The pages as they are would be lightened on a call billed by time; encoded pages win and go as made."""
    from app.pages import friendly
    _learn(installation)
    _fax_row(database)
    opt_in(database, PEER)
    lightened = []

    def lighten(root, job_id, pdf, source, request):
        lightened.append(job_id)
        request.result = friendly.Result(5, 2, 1_000_000, 500_000)
        return source  # the same pages, said to be lightened
    monkeypatch.setattr(friendly, 'lightened_pages', lighten)
    monkeypatch.setattr(friendly, 'should_lighten', lambda *args, **kwargs: (True, 'always'))
    monkeypatch.setattr(conversion, 'codec_pages', lambda pages, **_: (
        [page(98, height=600)], 'Sent as 1 encoded page instead of 5 (experimental).'))
    with priced(sip=card('sip', per_page='0.045')):
        changed = _send(database, tmp_path)
    assert lightened == [JOB] and changed.sent_pages == 1 and _change(database)['layout'] == 'codec'
    assert friendly.run_for(database, JOB) is None  # no lightened attempt is recorded
    assert views.sent_view(database, JOB)['sentences'] == ['Sent as 1 encoded page instead of 5 (experimental).']


def test_page_settings_of_the_chosen_recipient_follow_an_approved_toll_free_dial(installation, database, tmp_path):
    toll_free = '+18005550199'
    _learn(installation)
    installation.record_observation(toll_free, source='e' * 32, engine='hylafax',
                                    values={'max_length': 'unlimited', 'ecm': 1, 'fine': 1}, now=NOW)
    # The machine at the toll-free number takes unlimited length, but the person chose never for the recipient.
    installation.set_recipient_settings(PEER, packing='never')
    assert _send(database, tmp_path, number=toll_free, recipient=PEER) is None
    assert not list(tmp_path.glob('packed-*.tiff'))  # nothing packed (lightening keeps its own record)
    # Never on the toll-free number itself counts too; with neither set, the call is packed.
    installation.set_recipient_settings(PEER, packing='allow')
    installation.set_recipient_settings(toll_free, packing='never')
    assert _send(database, tmp_path, number=toll_free, recipient=PEER) is None
    installation.set_recipient_settings(toll_free, packing='allow')
    changed = _send(database, tmp_path, number=toll_free, recipient=PEER)
    assert (changed.original_pages, changed.sent_pages) == (5, 2)


def test_blank_space_off_for_the_chosen_recipient_keeps_page_bottoms_on_a_toll_free_dial(installation, database,
                                                                                          tmp_path):
    toll_free = '+18005550199'
    installation.record_observation(toll_free, source='e' * 32, engine='hylafax',
                                    values={'max_length': 'a4', 'ecm': 0, 'fine': 1, 'scan_ms': 10}, now=NOW)
    installation.set_recipient_settings(PEER, trim_blank=False)
    (tmp_path / 'note.txt').write_text('\n'.join(f'line {number}' for number in range(20)) + '\n')
    conversion.txt_to_pdf(str(tmp_path / 'note.txt'), str(tmp_path / f'{JOB}.pdf'))
    if not shutil.which('gs'):
        pytest.skip('Ghostscript renders the text page')
    conversion.pdf_to_tiff(str(tmp_path / f'{JOB}.pdf'), str(tmp_path / f'{JOB}.tiff'))
    configuration = SimpleNamespace(provider_id='sip', manifest=None, traits={'requires_tiff': True})
    claim = SimpleNamespace(job_id=JOB, attempt_id=ATTEMPT, members=())

    def send():
        return sending.prepare(database, SimpleNamespace(sip_fax_fine=True), configuration, claim,
                               {'to_number': toll_free, 'recipient_number': PEER}, tmp_path / f'{JOB}.pdf',
                               tmp_path / f'{JOB}.tiff', now=NOW)
    assert send() is None
    installation.set_recipient_settings(PEER, trim_blank=None)
    assert send().trimmed_pages == 1


def test_a_machine_without_error_correction_gets_rendered_pages_without_their_blank_bottom(installation, database,
                                                                                           tmp_path):
    _learn(installation, max_length='a4', ecm=0, scan_ms=10)
    (tmp_path / 'note.txt').write_text('\n'.join(f'line {number}' for number in range(20)) + '\n')
    conversion.txt_to_pdf(str(tmp_path / 'note.txt'), str(tmp_path / f'{JOB}.pdf'))
    conversion.pdf_to_tiff(str(tmp_path / f'{JOB}.pdf'), str(tmp_path / f'{JOB}.tiff')) if shutil.which('gs') else None
    if not shutil.which('gs'):
        pytest.skip('Ghostscript renders the text page')
    configuration = SimpleNamespace(provider_id='sip', manifest=None, traits={'requires_tiff': True})
    claim = SimpleNamespace(job_id=JOB, attempt_id=ATTEMPT, members=())
    changed = sending.prepare(database, SimpleNamespace(sip_fax_fine=True), configuration, claim, {'to_number': PEER},
                              tmp_path / f'{JOB}.pdf', tmp_path / f'{JOB}.tiff', now=NOW)
    assert changed.trimmed_pages == 1 and changed.sent_pages == 1
    before = conversion.read_fax_frames(str(tmp_path / f'{JOB}.tiff'))[0]
    after = conversion.read_fax_frames(changed.tiff)[0]
    assert after.height < before.height and _rows(before)[:after.height] == _rows(after)
    assert views.sent_view(database, JOB)['sentences'] == [
        'Blank space at the bottom of 1 page was left out; the receiving machine has no error correction.']


def test_an_image_page_and_an_ecm_machine_are_never_trimmed(installation, database, tmp_path):
    _learn(installation, max_length='a4', ecm=0, scan_ms=10)
    assert _send(database, tmp_path, pages=[page(1, bottom_blank=1500)]) is None  # an image PDF page
    _learn(installation, max_length='a4', ecm=1)
    assert _send(database, tmp_path, pages=[page(1, bottom_blank=1500)]) is None


def test_cloud_routes_send_a_packed_pdf_only_once_turned_on(installation, database, tmp_path):
    _learn(installation)
    assert _send(database, tmp_path, route='sinch') is None
    installation.set_route_settings('sinch', long_pages=True)
    if not shutil.which('gs'):
        pytest.skip('Ghostscript renders the PDF for a cloud route')
    changed = _send(database, tmp_path, route='sinch')
    assert changed.tiff is None and changed.pdf.endswith('.pdf') and changed.sent_pages == 2
    assert conversion.validate_pdf(changed.pdf) == 2
    # Phaxio fetches the PDF from Faxbot, which serves the attempt's own pages: off until turned on, like Sinch.
    assert _send(database, tmp_path, route='phaxio') is None
    installation.set_route_settings('phaxio', long_pages=True)
    fetched = _send(database, tmp_path, route='phaxio', attempt='c' * 32)
    assert fetched.pdf.endswith(f'packed-{JOB}-{"c" * 32}.pdf') and fetched.sent_pages == 2
    assert sending.fetched_pdf(tmp_path / f'{JOB}.pdf', JOB, f'https://x/fax/{JOB}/pdf?token=t&attempt={"c" * 32}') \
        == tmp_path / f'packed-{JOB}-{"c" * 32}.pdf'


def test_savings_count_packed_pages_on_delivered_sends_per_billing_model(installation, database, tmp_path):
    from app.routing.savings import savings as all_savings
    from app.routing.store import RouteStore
    _learn(installation)
    installation.record_change(job_id=JOB, attempt_id=ATTEMPT, number=PEER, route='sinch', original_pages=5,
                               sent_pages=2, capability=installation.capability(PEER), billing='per_page',
                               seconds_saved=9, layout='dense', now=NOW)
    routes = RouteStore(database, sip_preset=lambda: '')
    routes.replace_cards([card('sinch', per_page='0.045')])
    _attempt(database, ATTEMPT, 'sinch')
    part = views.savings(routes, database, since=NOW - timedelta(days=30), days=30)
    assert (part['faxes'], part['pages_saved'], part['saved']) == (1, 3, {'USD': 135_000})
    assert part['sentence'] == '3 pages saved by packing on 1 fax, saving about $0.135.'
    # Pages saved by encoding (experimental) are counted apart: another delivered attempt sent 1 encoded page for 5.
    encoded_attempt = 'e' * 32
    tables = {name: sa.Table(name, sa.MetaData(), autoload_with=database)
              for name in ('outbound_attempts', 'delivery_attempt_costs')}
    with database.begin() as connection:
        connection.execute(tables['outbound_attempts'].insert().values(**_filled(tables['outbound_attempts'], {
            'id': encoded_attempt, 'job_id': JOB, 'sequence': 2})))
        connection.execute(tables['delivery_attempt_costs'].insert().values(**_filled(
            tables['delivery_attempt_costs'], {
                'id': encoded_attempt, 'job_id': JOB, 'destination': PEER, 'route': 'sinch', 'route_reason': 'configured',
                'provider_id': 'sinch', 'billing_checks': 0, 'outcome': 'success', 'created_at': NOW})))
    installation.record_change(job_id=JOB, attempt_id=encoded_attempt, number=PEER, route='sinch', original_pages=5,
                               sent_pages=1, billing='per_page', seconds_saved=0, layout='codec', now=NOW)
    assert views.savings(routes, database, since=NOW - timedelta(days=30), days=30)['pages_saved'] == 3
    encoding = views.savings(routes, database, since=NOW - timedelta(days=30), days=30, layout='codec')
    assert (encoding['faxes'], encoding['pages_saved'], encoding['saved']) == (1, 4, {'USD': 180_000})
    assert encoding['sentence'] == '4 pages saved by encoding on 1 fax, saving about $0.18.'
    every = all_savings(routes, database, now=NOW + timedelta(days=1))
    assert (every['packing']['pages_saved'], every['encoding']['pages_saved']) == (3, 4)
    assert every['total'] == {'USD': 135_000 + 180_000}
    nothing = views.savings(routes, database, since=NOW + timedelta(days=1), days=7, layout='codec')
    assert nothing['sentence'] == 'No faxes went as encoded pages in the last 7 days.'


def _attempt(database, attempt_id, route):
    """A delivered attempt's cost row (and the fax and attempt it belongs to)."""
    tables = {name: sa.Table(name, sa.MetaData(), autoload_with=database)
              for name in ('fax_jobs', 'outbound_attempts', 'delivery_attempt_costs')}
    jobs, attempts, costs = tables['fax_jobs'], tables['outbound_attempts'], tables['delivery_attempt_costs']
    required = {column.name for column in attempts.c if not column.nullable and column.server_default is None}
    del required
    with database.begin() as connection:
        connection.execute(jobs.insert().values(**_filled(jobs, {
            'id': JOB, 'to_number': PEER, 'status': 'SUCCESS', 'created_at': NOW, 'updated_at': NOW, 'pages': 5,
            'backend': route})))
        connection.execute(attempts.insert().values(**_filled(attempts, {'id': attempt_id, 'job_id': JOB})))
        connection.execute(costs.insert().values(**_filled(costs, {
            'id': attempt_id, 'job_id': JOB, 'destination': PEER, 'route': route, 'route_reason': 'configured',
            'provider_id': route, 'billing_checks': 0, 'outcome': 'success', 'created_at': NOW})))


def _filled(table, values):
    return {**{column.name: _filler(column) for column in table.c if not column.nullable
               and column.server_default is None and column.name not in values}, **values}


def _filler(column):
    if isinstance(column.type, sa.DateTime):
        return NOW
    if isinstance(column.type, sa.Integer):
        return 1
    return 'x'


# Migration 0028 ------------------------------------------------------------------------------------------------

def _downgrade(engine, revision):
    config = Config(str(schema.API_DIRECTORY / 'alembic.ini'))
    config.set_main_option('script_location', str(schema.API_DIRECTORY / 'alembic'))
    with engine.connect() as connection:
        config.attributes['connection'] = connection
        command.downgrade(config, revision)


# The migration chain follows merge order: 0028 comes after 0030 (receiving accounts).
PRIOR = '0030_receiving_accounts'


def test_dense_pages_follows_receiving_accounts():
    assert schema_dense_pages.REVISION == '0028_dense_pages'
    assert schema.RECEIVING_RULES == PRIOR


def test_0028_adds_its_tables_keeps_every_row_and_downgrades(database):
    at_revision(database, PRIOR)
    before = snapshot(database)
    schema.upgrade_schema(database)
    after = snapshot(database)
    assert after['alembic_version'] == [{'version_num': schema.HEAD}]
    for name, rows_before in before.items():
        if name != 'alembic_version':
            # Each earlier table keeps every row on its original columns; later revisions may add columns.
            rows, kept = (without_later_access_changes(name, rows_before),
                          without_later_access_changes(name, after[name]))
            assert ([{key: row[key] for key in rows[0]} for row in kept] == rows) if rows else kept == [], name
    assert all(after[name] == [] for name in schema_dense_pages.ORDER)
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD
    _downgrade(database, PRIOR)
    assert not set(schema_dense_pages.ORDER) & set(sa.inspect(database).get_table_names())
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == PRIOR
    schema.upgrade_schema(database)
    assert set(schema_dense_pages.ORDER) <= set(sa.inspect(database).get_table_names())


# Over HTTP and the command line ---------------------------------------------------------------------------------

def test_the_console_and_command_line_read_and_change_page_settings(monkeypatch, tmp_path):
    from api.tests.test_cli import BOOTSTRAP, Cli, _serve
    for client in _serve(monkeypatch, tmp_path):
        admin = {'X-API-Key': BOOTSTRAP}
        url = '/routing/destinations/' + PEER + '/pages'
        view = client.get(url, headers=admin).json()
        assert (view['page_limit'], view['learned'], view['packing'], view['trim_blank']) == ('a4', False, 'allow',
                                                                                              None)
        assert view['capability_sentence'].startswith('Faxbot does not know yet how long a page')
        changed = client.put(url, headers=admin, json={'packing': 'never'}).json()
        assert changed['packing'] == 'never' and changed['trim_blank'] is None
        assert client.put(url, headers=admin, json={'packing': 'sometimes'}).status_code == 422
        assert client.get('/routing/destinations/not-a-number/pages', headers=admin).status_code == 400
        routes = {item['route']: item for item in client.get('/routing/page-routes', headers=admin).json()['routes']}
        assert routes['sip']['long_pages'] and routes['sip']['trim_blank'] is True
        assert not routes['sinch']['long_pages'] and not routes['phaxio']['long_pages']
        # Phaxio fetches the document from Faxbot, which serves the attempt's own pages: off until checked.
        assert routes['phaxio']['long_pages_possible'] and routes['phaxio']['sentence'] == (
            'Off until you check that Phaxio sends long pages without shrinking them: turn it on, fax a few pages '
            'to one of your own numbers on your phone line, and compare what arrives.')
        turned = client.put('/routing/page-routes/phaxio', headers=admin, json={'long_pages': True})
        assert turned.status_code == 200 and turned.json()['long_pages'] is True
        assert client.put('/routing/page-routes/unknown', headers=admin, json={'long_pages': True}).status_code == 404
        cli = Cli(client)
        result = cli('recipients', 'set', PEER, '--pages-per-sheet', 'machine', '--blank-space', 'off')
        assert result.exit_code == 0, (result.stdout, result.stderr)
        shown = ' '.join(cli('recipients', 'show', PEER).stdout.split())
        assert 'Pages per sheet as the receiving machine allows' in shown
        assert 'Blank space at the bottom of pages off' in shown
        result = cli('providers', 'long-pages', 'sinch', '--long-pages', 'on')
        assert result.exit_code == 0 and 'Sinch gets several pages on one long page' in result.stdout
        listed = ' '.join(cli('providers', 'long-pages').stdout.split())
        assert 'sinch' in listed and 'Phaxio gets several pages on one long page' in listed
        assert cli('providers', 'long-pages', 'sinch', '--long-pages', 'maybe').exit_code != 0


def test_a_fax_encoded_at_acceptance_by_an_earlier_build_gets_its_original_image_back_once(installation, database,
                                                                                          tmp_path, caplog):
    """Upgrade safety: the old path wrote encoded pages over <job>.tiff (and payload PDFs beside the fax) and
    recorded codec_sends at acceptance. Before the next attempt chooses, the image is made again from the PDF."""
    import logging
    from app import codec
    from app.codec.store import record_send
    if not shutil.which('gs'):
        pytest.skip('Ghostscript draws the original image again')
    pdf, tiff = tmp_path / f'{JOB}.pdf', tmp_path / f'{JOB}.tiff'
    (tmp_path / 'letter.txt').write_text('\n'.join(f'Synthetic line {index} of a letter.' for index in range(60)))
    conversion.txt_to_pdf(str(tmp_path / 'letter.txt'), str(pdf))
    conversion.pdf_to_tiff(str(pdf), str(tmp_path / 'original.tiff'))
    original = conversion.read_fax_frames(str(tmp_path / 'original.tiff'))
    # The old acceptance path: encoded pages over the fax image, a payload PDF for a provider, and the send row.
    encoded = codec.encode_document(codec.Document(pdf.read_bytes(), 'application/pdf', 'document.pdf'))
    codec.write_tiff(encoded.pages, tiff)
    (tmp_path / f'{JOB}.payload-sinch.pdf').write_bytes(b'%PDF-1.4 encoded pages')
    _fax_row(database, 'sip', pages=len(original))
    with database.begin() as connection:
        record_send(connection, database, JOB, {
            'phone_number': PEER, 'provider_id': 'sip', 'layout': 'grid', 'resolution': 'fine', 'fec': 'medium',
            'pages_original': len(original), 'pages_encoded': encoded.page_count, 'document_sha256': '0' * 64,
            'encrypted': 0, 'format_version': 1}, NOW)
    configuration = SimpleNamespace(provider_id='sip', manifest=None, traits={'requires_tiff': True})

    def attempt(attempt_id):
        return sending.prepare(database, SimpleNamespace(sip_fax_fine=True), configuration,
                               SimpleNamespace(job_id=JOB, attempt_id=attempt_id, members=()), {'to_number': PEER},
                               pdf, tiff, now=NOW)
    with caplog.at_level(logging.WARNING):
        attempt('c' * 32)
        restored = conversion.read_fax_frames(str(tiff))
        assert len(restored) == len(original) and all(same(a, b) for a, b in zip(restored, original))
        assert not (tmp_path / f'{JOB}.payload-sinch.pdf').exists()
        attempt('d' * 32)  # once: nothing is left to restore
    said = [record.getMessage() for record in caplog.records if record.name == 'app.codec.send']
    assert said == [f'Fax {JOB}: its fax image held encoded pages written when it was accepted by an earlier build; '
                    'it was made again from the original document before this attempt.',
                    f'Fax {JOB}: an unused encoded-pages PDF from an earlier build was removed.']
