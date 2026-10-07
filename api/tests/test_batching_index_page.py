"""One index page instead of a separator before each document in a shared call (research D6, migration 0025).

The recipient's agreement, the call's fixed layout and page ranges, the index page itself, each fax's outcome
when the call breaks, charge shares and the pages saved. Synthetic faxes only; SQLite and PostgreSQL.
"""
from datetime import datetime, timedelta
from itertools import product
from pathlib import Path

import pytest
import sqlalchemy as sa

from api.tests.test_schema import database  # noqa: F401 - fixture
from api.tests.test_batching import (  # noqa: F401 - the sip fixture
    Ami, NUMBER, T0, accept, row, sip, state, transport, write_fax,
)
from api.app.outbound_store import OutboundStore
from api.app.outbound_worker import OutboundWorker
from api.app.request_identity import IdempotentReplay, RequestIdentity
from api.app.batching import image, money, policy, results
from api.app.batching import store as batching
from api.app.batching.outcomes import map_call


def index_on(sip, *, boundaries='index_page', **changes):
    configuration = sip[0]
    return batching.BatchingSettings(configuration.engine).save(
        NUMBER, enabled=True, boundaries=boundaries, boundaries_agreed=True, actor='principal:p1',
        actor_name='Owner', **changes)


def headers_on(sip, *, header='Synthetic Clinic', station='+15555550100', **changes):
    """Marks at the top of every page; returns the fixture with the settings that name the sender on every page."""
    configuration, delivery, snapshot, routes, data = sip
    current = configuration.apply(snapshot, snapshot.active.values.with_patch(
        {'fax_header': header, 'fax_station_id': station}), restart_required=False, actor='test')
    index_on(sip, boundaries='page_headers', **changes)
    return configuration, delivery, current, routes, data


def ranges(sip, jobs):
    return [(row(sip, job)['layout'], row(sip, job)['first_page'], row(sip, job)['last_page']) for job in jobs]


# The agreement ---------------------------------------------------------------------------------

def test_separators_stay_until_the_recipients_agreement_to_another_convention_is_recorded_with_who_and_when(sip):
    configuration, *_ = sip
    settings = batching.BatchingSettings(configuration.engine)
    assert settings.get(NUMBER)['boundaries'] == 'separators'
    with pytest.raises(batching.BatchingInputError) as refused:
        settings.save(NUMBER, enabled=True, boundaries='index_page', actor='principal:p1')
    assert str(refused.value) == 'Record that the recipient agreed to one index page before using it.'
    with pytest.raises(batching.BatchingInputError) as refused:
        settings.save(NUMBER, enabled=True, boundaries='page_headers', actor='principal:p1')
    assert str(refused.value) == 'Record that the recipient agreed to marks at the top of every page before using them.'
    with pytest.raises(batching.BatchingInputError):
        settings.save(NUMBER, enabled=False, boundaries='index_page', boundaries_agreed=True, actor='principal:p1')
    before = datetime.utcnow()
    setting, action = settings.save(NUMBER, enabled=True, boundaries='index_page', boundaries_agreed=True,
                                    actor='principal:p1', actor_name='Owner')
    assert action == 'changed' and setting['boundaries'] == 'index_page'
    agreement = settings.boundaries_agreement(NUMBER, 'index_page')
    assert (agreement['actor'], agreement['actor_name']) == ('principal:p1', 'Owner')
    assert before <= agreement['created_at'] <= datetime.utcnow()
    # A later change keeps the index page and does not record the agreement again.
    assert settings.save(NUMBER, enabled=True, actor='principal:p2', max_pages=20)[0]['boundaries'] == 'index_page'
    assert [(c['boundaries'], c['boundaries_agreed']) for c in settings.history(NUMBER)] == [
        ('index_page', 0), ('index_page', 1), ('separators', 0)]
    assert settings.save(NUMBER, enabled=True, boundaries='index_page', actor='principal:p2')[1] is None
    # Another convention is another agreement; going back to separators needs none.
    with pytest.raises(batching.BatchingInputError):
        settings.save(NUMBER, enabled=True, boundaries='page_headers', actor='principal:p2')
    assert settings.save(NUMBER, enabled=True, boundaries='page_headers', boundaries_agreed=True,
                         actor='principal:p2')[0]['boundaries'] == 'page_headers'
    assert settings.boundaries_agreement(NUMBER, 'page_headers')['actor'] == 'principal:p2'
    assert settings.save(NUMBER, enabled=True, boundaries='separators', actor='principal:p1')[0]['boundaries'] == (
        'separators')
    assert settings.boundaries_agreement(NUMBER, 'separators') is None
    # Turning sending together off goes back to separators; turning it on again needs a new agreement.
    settings.save(NUMBER, enabled=True, boundaries='index_page', boundaries_agreed=True, actor='principal:p1')
    assert settings.save(NUMBER, enabled=False, actor='principal:p1')[0]['boundaries'] == 'separators'
    assert settings.save(NUMBER, enabled=True, recipient_agreed=True, actor='principal:p1')[0]['boundaries'] == (
        'separators')
    with pytest.raises(batching.BatchingInputError):
        settings.save(NUMBER, enabled=True, boundaries='index_page', actor='principal:p1')
    for bad in ({'boundaries': 'stapled'}, {'boundaries_agreed': 1}):
        with pytest.raises(batching.BatchingInputError):
            settings.save(NUMBER, enabled=True, actor='principal:p1', **bad)


# The call's layout and page ranges -------------------------------------------------------------

def test_a_call_formed_with_the_index_page_records_its_layout_and_each_faxs_own_pages(sip):
    _, delivery, *_ = sip
    index_on(sip)
    jobs = [accept(sip, pages=pages, at=T0 + timedelta(seconds=n)) for n, pages in enumerate((2, 1, 3))]
    claim = delivery.claim('worker', now=T0 + timedelta(minutes=10))
    assert [member.job_id for member in claim.members] == jobs
    # Page 1 is the index page; then each fax's own pages, with nothing between them.
    assert ranges(sip, jobs) == [('index_page', 2, 3), ('index_page', 4, 4), ('index_page', 5, 7)]
    # With separators (the setting off) the same faxes take 9 pages and each starts with its separator.
    delivery.split_batch(claim, separate=set(), now=T0 + timedelta(minutes=10))
    index_on(sip, boundaries='separators')
    for job in jobs:
        assert row(sip, job)['layout'] is None
    again = delivery.claim('worker', now=T0 + timedelta(minutes=11))
    assert [member.job_id for member in again.members] == jobs
    assert ranges(sip, jobs) == [('separators', 1, 3), ('separators', 4, 5), ('separators', 6, 9)]


def test_the_layout_is_fixed_when_the_call_forms_and_a_change_afterwards_does_not_move_it(sip):
    _, delivery, *_ = sip
    index_on(sip)
    jobs = [accept(sip, at=T0 + timedelta(seconds=n)) for n in range(2)]
    claim = delivery.claim('worker', now=T0 + timedelta(minutes=10))
    index_on(sip, boundaries='separators')
    assert ranges(sip, jobs) == [('index_page', 2, 2), ('index_page', 3, 3)]
    assert [member['layout'] for member in batching.call_members(sip[0].engine, claim.attempt_id)] == [
        'index_page', 'index_page']


def test_the_page_cap_counts_one_index_page_instead_of_a_separator_per_fax(sip):
    _, delivery, *_ = sip
    index_on(sip, max_pages=10)
    jobs = [accept(sip, pages=3, at=T0 + timedelta(seconds=n)) for n in range(4)]
    # 1 index page + 3 + 3 + 3 = 10 pages; separators would fit only two faxes (4 + 4).
    claim = delivery.claim('worker', now=T0 + timedelta(seconds=5))
    assert [member.job_id for member in claim.members] == jobs[:3]
    assert row(sip, jobs[3])['state'] == 'waiting'


def test_one_index_page_lists_at_most_its_documents_and_the_call_goes_when_it_is_full(sip):
    configuration, delivery, *_ = sip
    from api.app.routing.store import RouteStore
    RouteStore(configuration.engine).update_destination(NUMBER, max_calls=0)
    index_on(sip, max_pages=200)
    count = policy.INDEX_PAGE_DOCUMENTS
    jobs = [accept(sip, at=T0 + timedelta(seconds=n)) for n in range(count + 1)]
    claim = delivery.claim('worker', now=T0 + timedelta(seconds=30))  # due at once: the next fax does not fit
    assert [member.job_id for member in claim.members] == jobs[:count]
    assert row(sip, jobs[-1])['state'] == 'waiting'
    assert ranges(sip, jobs[:count])[-1] == ('index_page', count + 1, count + 1)


# The index page --------------------------------------------------------------------------------

def _layout(*pages):
    """Index-page rows as ``join_on`` records them, for faxes of these page counts."""
    rows, first = [], 2
    for number, count in enumerate(pages, start=1):
        rows.append({'id': f'job-{number}', 'attempt_id': f'attempt-{number}', 'first_page': first,
                     'last_page': first + count - 1, 'layout': 'index_page', 'pages': count})
        first += count
    return rows


def _pdf_text(path):
    from pypdf import PdfReader
    reader = PdfReader(str(path))
    return len(reader.pages), [line.strip() for line in reader.pages[0].extract_text().splitlines() if line.strip()]


@pytest.mark.parametrize('pages, expected', [
    ((3,), ['This fax has 1 document on 4 pages, counting this index page.',
            'Document 1: pages 2–4 (3 pages)', 'Case 2026-117 · from Front Desk']),
    ((1, 4), ['This fax has 2 documents on 6 pages, counting this index page.',
              'Document 1: page 2 (1 page)', 'Case 2026-117 · from Front Desk',
              'Document 2: pages 3–6 (4 pages)', 'Faxbot 22222222']),
    ((2, 1, 3, 1, 2), ['This fax has 5 documents on 10 pages, counting this index page.',
                       'Document 1: pages 2–3 (2 pages)', 'Case 2026-117 · from Front Desk',
                       'Document 2: page 4 (1 page)', 'Faxbot 22222222',
                       'Document 3: pages 5–7 (3 pages)', 'Faxbot 33333333 · from Billing',
                       'Document 4: page 8 (1 page)', 'Faxbot 44444444',
                       'Document 5: pages 9–10 (2 pages)', 'Faxbot 55555555']),
])
def test_the_index_page_lists_each_documents_page_range_and_nothing_more(tmp_path, pages, expected):
    references = ['Case 2026-117', 'Faxbot 22222222', 'Faxbot 33333333', 'Faxbot 44444444', 'Faxbot 55555555']
    senders = ['Front Desk', None, 'Billing', None, None]
    rows = _layout(*pages)
    entries = [image.index_entry(n, r['first_page'], r['last_page'], references[n - 1], r['pages'], senders[n - 1])
               for n, r in enumerate(rows, start=1)]
    heading, summary = image.index_heading(len(rows), rows[-1]['last_page'])
    image.index_pdf(heading, summary, entries, tmp_path / 'index.pdf')
    count, text = _pdf_text(tmp_path / 'index.pdf')
    assert count == 1 and text == ['Index of documents in this fax', *expected]
    # The whole call: one index page, then each fax's own pages copied unchanged, in its stored range.
    jobs = [str(n) * 32 for n in range(1, len(pages) + 1)]
    for job, count in zip(jobs, pages):
        write_fax(tmp_path, job, count)
    members = [(job, count, 'unused separator line') for job, count in zip(jobs, pages)]
    out = image.build_call_image(tmp_path, 'a' * 32, members, index=(heading, summary, entries))
    assert image.page_count(out) == 1 + sum(pages) == rows[-1]['last_page']
    from PIL import Image, ImageChops
    with Image.open(out) as combined:
        for job, r in zip(jobs, rows):
            with Image.open(tmp_path / (job + '.tiff')) as original:
                for offset in range(r['pages']):
                    combined.seek(r['first_page'] - 1 + offset)
                    original.seek(offset)
                    assert ImageChops.difference(combined.convert('1'), original.convert('1')).getbbox() is None


def test_the_index_page_is_one_fine_resolution_fax_page_and_the_same_every_time(tmp_path):
    from PIL import Image, ImageChops
    rows = _layout(1, 2)
    entries = [image.index_entry(n, r['first_page'], r['last_page'], 'Faxbot 7f3a9c21', r['pages'], 'Front Desk')
               for n, r in enumerate(rows, start=1)]
    heading = image.index_heading(2, 4)
    for name in ('one', 'two'):
        (tmp_path / name).mkdir()
        (tmp_path / f'{name}.tiff').write_bytes(image.index_tiff(tmp_path / name, *heading, entries)[0])
    with Image.open(tmp_path / 'one.tiff') as one, Image.open(tmp_path / 'two.tiff') as two:
        assert getattr(one, 'n_frames', 1) == 1
        assert tuple(round(value) for value in one.info['dpi']) == (204, 196)  # fax fine resolution
        assert ImageChops.difference(one.convert('1'), two.convert('1')).getbbox() is None


def test_the_fullest_index_page_fits_one_page_and_one_more_document_is_refused_never_cut(tmp_path):
    # The widest text a separator can carry: a 100-character reference and an 80-character sender name.
    wide = [image.index_entry(n, 100 + n, 109 + n, 'W' * 100, 10, 'M' * 80)
            for n in range(1, policy.INDEX_PAGE_DOCUMENTS + 2)]
    heading = image.index_heading(len(wide), 199)
    (tmp_path / 'full').mkdir()
    data, _ = image.index_tiff(tmp_path / 'full', *heading, wide[:policy.INDEX_PAGE_DOCUMENTS])
    assert len(image._read_ifds(data)) == 1
    _, text = _pdf_text(tmp_path / 'full' / 'index.pdf')
    assert sum(line.startswith('Document ') for line in text) == policy.INDEX_PAGE_DOCUMENTS
    # Each detail too long for two lines ends with an ellipsis, so the cut shows on the page.
    assert sum(line.endswith('…') for line in text) == policy.INDEX_PAGE_DOCUMENTS
    with pytest.raises(ValueError):
        image.index_pdf(*heading, wide, tmp_path / 'over.pdf')


# Outcomes: the call's confirmed pages against the index-page layout ----------------------------

def outcome(found):
    return [(item.status, item.category) for item in found]


def test_an_incomplete_batch_that_broke_in_document_three_delivers_documents_one_and_two():
    # Pages: 1 index, 2-3 doc one, 4 doc two, 5-7 doc three, 8 doc four, 9-10 doc five.
    found = map_call(_layout(2, 1, 3, 1, 2), succeeded=False, confirmed_pages=6, failure_sentence='Busy.')
    assert outcome(found) == [('success', None), ('success', None), ('failed', 'partly_sent'),
                              ('failed', None), ('failed', None)]
    assert found[2].sentence == 'The call failed after 2 of its 3 pages; check before sending again.'
    assert found[3].sentence == found[4].sentence == 'The call failed before this fax was sent.'


def test_with_no_separator_between_faxes_a_break_at_a_boundary_leaves_the_next_fax_for_a_person():
    # Document two ended on page 4; document three's first page may have been on its way.
    found = map_call(_layout(2, 1, 3), succeeded=False, confirmed_pages=4)
    assert outcome(found) == [('success', None), ('success', None), ('failed', 'partly_sent')]
    assert found[2].sentence == 'The call failed as this fax began; check before sending again.'
    # Only the index page was confirmed: document one may have begun, the rest were never sent.
    assert outcome(map_call(_layout(2, 1), succeeded=False, confirmed_pages=1)) == [
        ('failed', 'partly_sent'), ('failed', None)]
    nothing = map_call(_layout(2, 1), succeeded=False, confirmed_pages=0, failure_sentence='The number was busy.')
    assert outcome(nothing) == [('failed', None)] * 2 and {item.sentence for item in nothing} == {'The number was busy.'}


def test_a_missing_page_on_a_call_reported_sent_leaves_the_faxes_after_it_for_a_person():
    # The engine said the call was sent but confirmed 9 of its 10 pages: document five is not delivered.
    found = map_call(_layout(2, 1, 3, 1, 2), succeeded=True, confirmed_pages=9)
    assert outcome(found) == [('success', None)] * 4 + [('unconfirmed', 'pages_unconfirmed')]
    assert outcome(map_call(_layout(2, 1), succeeded=True, confirmed_pages=4)) == [('success', None)] * 2
    assert outcome(map_call(_layout(2, 1), succeeded=True, confirmed_pages=None)) == [('success', None)] * 2
    assert outcome(map_call(_layout(2, 1), succeeded=False, confirmed_pages=None)) == [
        ('unconfirmed', 'pages_unconfirmed')] * 2


def _legacy_map_call(members, *, confirmed_pages):
    """The separator mapping before the index page existed (a failed call), kept to prove nothing moved."""
    found = []
    for member in members:
        first, last = member['first_page'], member['last_page']
        if confirmed_pages is None:
            found.append(('unconfirmed', 'pages_unconfirmed', None))
        elif last <= confirmed_pages:
            found.append(('success', None, None))
        elif confirmed_pages < first:
            found.append(('failed', None, 'The call failed before this fax was sent.'))
        else:
            arrived, own = confirmed_pages - first, last - first
            pages = '1 page' if own == 1 else f'{own} pages'
            found.append(('failed', 'partly_sent',
                          f'The call failed after {arrived} of its {pages}; check before sending again.' if arrived
                          else 'The call failed as this fax began; check before sending again.'))
    return found


@pytest.mark.parametrize('layout', [None, 'separators'])
def test_calls_with_separators_map_exactly_as_before(layout):
    for pages in product((1, 2, 3), repeat=3):
        members, first = [], 1
        for number, count in enumerate(pages, start=1):
            members.append({'id': f'job-{number}', 'attempt_id': f'a-{number}', 'first_page': first,
                            'last_page': first + count, 'layout': layout})
            first += count + 1
        for confirmed in [None, *range(1, first + 1)]:
            found = [(item.status, item.category, item.sentence)
                     for item in map_call(members, succeeded=False, confirmed_pages=confirmed)]
            assert found == _legacy_map_call(members, confirmed_pages=confirmed), (pages, confirmed)
        assert outcome(map_call(members, succeeded=True, confirmed_pages=first - 1)) == [('success', None)] * 3


def _submitted(sip, *pages, urgent_last=False):
    _, delivery, *_ = sip
    jobs = [accept(sip, pages=count, at=T0 + timedelta(seconds=n), urgent=urgent_last and n == len(pages) - 1)
            for n, count in enumerate(pages)]
    now = T0 + timedelta(minutes=10)
    claim = delivery.claim('worker', now=now)
    assert delivery.begin_submission(claim, now=now)
    delivery.record_receipt(claim, provider_sid=claim.job_id, status='in_progress', now=now)
    return jobs, claim


def test_a_broken_index_call_never_sends_a_partly_sent_fax_another_way(sip):
    configuration, delivery, *_ = sip
    index_on(sip)
    rerouted = []
    OutboundStore.fallback_policy = lambda job, attempt: rerouted.append(job) or False
    try:
        jobs, claim = _submitted(sip, 2, 1, 3, 1, 2)
        event = {'JobID': claim.job_id, 'AttemptID': claim.attempt_id, 'Status': 'FAILED', 'Pages': '6'}
        assert results.apply_fax_result(delivery, event) is True
    finally:
        OutboundStore.fallback_policy = None
    assert [state(sip, job) for job in jobs] == ['success', 'success', 'failed', 'failed', 'failed']
    with configuration.engine.connect() as connection:
        categories = [connection.scalar(sa.select(delivery.attempts.c.error_category).where(
            delivery.attempts.c.id == delivery.get(job)['attempt_id'])) for job in jobs]
    assert categories == [None, None, 'partly_sent', None, None]
    assert rerouted == jobs[3:]  # only the faxes that were never sent may go another way


def test_a_duplicate_batch_result_changes_nothing_and_a_replayed_fax_joins_no_second_call(sip):
    configuration, delivery, *_ = sip
    index_on(sip)
    jobs, claim = _submitted(sip, 1, 2)
    event = {'JobID': claim.job_id, 'AttemptID': claim.attempt_id, 'Status': 'FAILED', 'Pages': '2'}
    assert results.apply_fax_result(delivery, event) is True
    after = [state(sip, job) for job in jobs]
    assert after == ['success', 'failed']
    histories = [len(delivery.history(job)) for job in jobs]
    # The same report again adds nothing at all.
    assert results.apply_fax_result(delivery, event) is True
    assert [len(delivery.history(job)) for job in jobs] == histories
    # A later contradicting report for the same call is kept as evidence and changes no outcome.
    assert results.apply_fax_result(delivery, {**event, 'Status': 'SUCCESS', 'Pages': '4'}) is True
    assert [state(sip, job) for job in jobs] == after
    assert [delivery.history(job)[-1]['kind'] for job in jobs] == ['late_observation', 'terminal_conflict']
    assert [row(sip, job)['batch_id'] for job in jobs] == [claim.attempt_id] * 2
    # The same send again is the first fax: nothing new waits and no second call carries it.
    intent = RequestIdentity.from_key('same-index-fax', principal_scope='key:front', fingerprint='f' * 64)
    first = accept(sip, request_identity=intent)
    with pytest.raises(IdempotentReplay) as replay:
        accept(sip, request_identity=intent)
    assert replay.value.job_id == first
    t = batching.tables(configuration.engine)
    with configuration.engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(t['outbound_batch_members']).where(
            t['outbound_batch_members'].c.state == 'waiting')) == 1


def test_an_urgent_fax_does_not_wait_and_takes_the_waiting_faxes_under_one_index_page(sip):
    _, delivery, *_ = sip
    index_on(sip)
    waiting = accept(sip, pages=2, at=T0)
    urgent = accept(sip, pages=1, at=T0 + timedelta(minutes=1), urgent=True)
    claim = delivery.claim('worker', now=T0 + timedelta(minutes=1))  # nine minutes before the wait ends
    assert [member.job_id for member in claim.members] == [waiting, urgent]
    assert ranges(sip, [waiting, urgent]) == [('index_page', 2, 3), ('index_page', 4, 4)]
    # An urgent fax with nothing waiting goes on its own: no index page and no separator.
    assert delivery.begin_submission(claim, now=T0 + timedelta(minutes=1))
    delivery.record_receipt(claim, provider_sid=claim.job_id, status='success', now=T0 + timedelta(minutes=1))
    alone = accept(sip, pages=1, at=T0 + timedelta(minutes=2), urgent=True)
    single = delivery.claim('worker', now=T0 + timedelta(minutes=2))
    assert single.job_id == alone and single.members == () and row(sip, alone)['layout'] is None


def test_the_faxs_details_say_which_layout_its_call_used_and_its_pages_in_it(sip):
    from api.app.batching import http
    _, delivery, *_ = sip
    index_on(sip)
    jobs = [accept(sip, pages=pages, at=T0 + timedelta(seconds=n)) for n, pages in enumerate((2, 3))]
    delivery.claim('worker', now=T0 + timedelta(minutes=10))
    view = http.summary(row(sip, jobs[1]))
    assert (view['layout'], view['call_first_page'], view['call_last_page']) == ('index_page', 4, 6)
    assert http.layout_sentence(view) == ('The index page at the start of the call lists this fax as document 2 '
                                          f'of 2, pages 4–6, under Faxbot {jobs[1][:8]}.')
    separators = {**row(sip, jobs[0]), 'layout': None, 'first_page': 1, 'last_page': 3}
    view = http.summary(separators)
    assert (view['layout'], view['call_first_page'], view['call_last_page']) == ('separators', 2, 3)
    assert http.layout_sentence(view) == f'Its separator page says Faxbot {jobs[0][:8]} (document 1 of 2).'
    assert http.layout_sentence(http.summary({**separators, 'state': 'waiting'})) is None


@pytest.mark.asyncio
async def test_the_worker_sends_one_index_page_then_each_faxs_pages(sip):
    _, delivery, _, _, data = sip
    index_on(sip)
    earlier = datetime.utcnow() - timedelta(minutes=11)
    jobs = [accept(sip, pages=pages, files=True, at=earlier + timedelta(seconds=n)) for n, pages in enumerate((2, 1))]
    ami = Ami()
    assert await OutboundWorker(delivery, transport(sip, ami)).step() is True
    [(job_id, _, tiff, attempt)] = ami.calls
    assert job_id == jobs[0] and image.page_count(tiff) == 1 + 2 + 1
    event = {'JobID': jobs[0], 'AttemptID': attempt, 'Status': 'SUCCESS', 'Pages': '4'}
    assert results.apply_fax_result(delivery, event) is True
    assert [state(sip, job) for job in jobs] == ['success', 'success'] and not Path(tiff).exists()


# Marks at the top of every page (research M20) ---------------------------------------------------

def _marks_layout(*pages):
    rows, first = [], 1
    for number, count in enumerate(pages, start=1):
        rows.append({'id': f'job-{number}', 'attempt_id': f'attempt-{number}', 'first_page': first,
                     'last_page': first + count - 1, 'layout': 'page_headers', 'pages': count})
        first += count
    return rows


@pytest.mark.parametrize('pages', [(3,), (1, 4), (2, 1, 3, 1, 2)])
def test_each_page_gets_its_documents_line_above_it_and_its_own_pixels_unchanged(tmp_path, pages):
    from PIL import Image, ImageChops
    references = ['Case 2026-117', 'Faxbot 22222222', 'Faxbot 33333333', 'Faxbot 44444444', 'Faxbot 55555555']
    senders = ['Front Desk', None, 'Billing', None, None]
    jobs = [str(n) * 32 for n in range(1, len(pages) + 1)]
    for job, count in zip(jobs, pages):
        write_fax(tmp_path, job, count)
    marks = [[image.page_mark(n, len(pages), page, count, references[n - 1], senders[n - 1])
              for page in range(1, count + 1)] for n, count in enumerate(pages, start=1)]
    assert marks[0][0] == f'Document 1 of {len(pages)} · page 1 of {pages[0]} · Case 2026-117 · from Front Desk'
    if len(pages) == 5:
        assert marks[2] == [f'Document 3 of 5 · page {n} of 3 · Faxbot 33333333 · from Billing'
                            for n in (1, 2, 3)]
        assert marks[3] == ['Document 4 of 5 · page 1 of 1 · Faxbot 44444444']
    members = [(job, count, 'unused separator line') for job, count in zip(jobs, pages)]
    out = image.build_call_image(tmp_path, 'c' * 32, members, marks=marks)
    # No page is added: the call is exactly the documents' own pages, each in its stored range.
    assert image.page_count(out) == sum(pages) == _marks_layout(*pages)[-1]['last_page']
    with Image.open(out) as combined:
        call_page = 0
        for job, lines in zip(jobs, marks):
            with Image.open(tmp_path / (job + '.tiff')) as original:
                for offset, line in enumerate(lines):
                    combined.seek(call_page)
                    original.seek(offset)
                    assert combined.tag_v2[262] == 0 and combined.tag_v2[259] == 4  # white is zero, Group 4
                    assert tuple(round(value) for value in combined.info['dpi']) == (204, 196)
                    page = combined.convert('L')
                    assert page.size == (original.width, original.height + image.MARK_ROWS)
                    # Above the page: exactly the line for this page. Below it: the page's own pixels.
                    band = page.crop((0, 0, page.width, image.MARK_ROWS))
                    expected = image.mark_band(page.width, line).convert('L')
                    assert ImageChops.difference(band, expected).getbbox() is None
                    body = page.crop((0, image.MARK_ROWS, page.width, page.height))
                    assert ImageChops.difference(body, original.convert('L')).getbbox() is None
                    call_page += 1


def test_a_page_mark_is_drawn_black_on_white_and_differs_for_each_page(tmp_path):
    from PIL import ImageChops
    one = image.mark_band(1728, image.page_mark(2, 5, 1, 4, 'Faxbot 7f3a9c21', 'Front Desk'))
    two = image.mark_band(1728, image.page_mark(2, 5, 2, 4, 'Faxbot 7f3a9c21', 'Front Desk'))
    assert one.size == (1728, image.MARK_ROWS) and one.mode == '1'
    black = one.histogram()[0]
    assert 0 < black < one.width * one.height // 4  # some ink, mostly white
    assert ImageChops.difference(one, two).getbbox() is not None
    assert ImageChops.difference(one, image.mark_band(1728, image.page_mark(2, 5, 1, 4, 'Faxbot 7f3a9c21',
                                                                           'Front Desk'))).getbbox() is None
    # The longest line a page can carry still fits: it is cut with an ellipsis, never drawn off the page.
    longest = image.mark_band(1728, image.page_mark(199, 199, 200, 200, 'W' * 100, 'M' * 80))
    assert longest.crop((1728 - image.MARK_MARGIN + 1, 0, 1728, image.MARK_ROWS - 6)).histogram()[0] == 0


def test_marks_at_the_top_of_every_page_add_no_page_and_count_only_the_faxs_own_pages(sip):
    sip = headers_on(sip, max_pages=7)
    _, delivery, *_ = sip
    jobs = [accept(sip, pages=pages, at=T0 + timedelta(seconds=n)) for n, pages in enumerate((2, 1, 3, 2))]
    # 2 + 1 + 3 = 6 pages fit 7 and the next 2 do not; separators would have needed 9 for the same three faxes.
    claim = delivery.claim('worker', now=T0 + timedelta(seconds=5))
    assert [member.job_id for member in claim.members] == jobs[:3]
    assert ranges(sip, jobs[:3]) == [('page_headers', 1, 2), ('page_headers', 3, 3), ('page_headers', 4, 6)]


def test_without_a_header_naming_the_sender_on_every_page_the_call_uses_separators_and_says_why(sip):
    from api.app.batching import http
    # 47 CFR 68.318(d): no sending number in the header, so no call may rely on page marks alone.
    sip = headers_on(sip, station='')
    _, delivery, snapshot, *_ = sip
    assert not policy.header_identifies_sender(snapshot.active.values)
    jobs = [accept(sip, at=T0 + timedelta(seconds=n)) for n in range(2)]
    delivery.claim('worker', now=T0 + timedelta(minutes=10))
    assert [layout for layout, *_ in ranges(sip, jobs)] == ['separators', 'separators']
    setting = batching.BatchingSettings(sip[0].engine).get(NUMBER)
    assert setting['boundaries'] == 'page_headers'  # the choice stays; only these calls use separators
    assert http._boundaries_sentence(setting, snapshot.active.values) == policy.HEADER_NEEDS
    assert not policy.header_identifies_sender(snapshot.active.values.with_patch(
        {'fax_header': ' ', 'fax_station_id': '+15555550100'}))
    # The shipped header text names the software, not the sender.
    assert not policy.header_identifies_sender(snapshot.active.values.with_patch(
        {'fax_header': 'Faxbot', 'fax_station_id': '+15555550100'}))
    assert not policy.header_identifies_sender(snapshot.active.values.with_patch(
        {'fax_station_id': '+10000000000'}))  # the placeholder is not a sending number
    assert policy.header_identifies_sender(snapshot.active.values.with_patch({'fax_station_id': '+15555550100'}))


@pytest.mark.asyncio
async def test_a_shared_call_with_page_marks_never_goes_with_a_header_that_does_not_name_the_sender(sip):
    from api.app.routing.store import RouteStore
    configuration, delivery, *_ = sip
    RouteStore(configuration.engine).update_destination(NUMBER, max_calls=0)  # calls at once to one number
    earlier = datetime.utcnow() - timedelta(minutes=12)
    # Accepted while the settings printed no sending number: these faxes' calls print the header they were sent with.
    unnamed = [accept(sip, pages=1, files=True, at=earlier + timedelta(seconds=n)) for n in range(2)]
    good = headers_on(sip)
    jobs = [accept(good, pages=pages, files=True, at=earlier + timedelta(minutes=1, seconds=n))
            for n, pages in enumerate((2, 1))]
    ami = Ami()
    worker = OutboundWorker(delivery, transport(good, ami))
    # The first call would mark pages under a header that names no sending number: it is split, nothing sent.
    await worker.step()
    assert ami.calls == [] and [row(sip, job)['state'] for job in unnamed] == ['separate', 'separate']
    # The faxes accepted with the sender named go together, marked at the top of every page, with no page added.
    while not ami.calls:
        assert await worker.step() is True
    [(job_id, _, tiff, attempt)] = ami.calls
    assert job_id == jobs[0] and image.page_count(tiff) == 3
    assert [layout for layout, *_ in ranges(good, jobs)] == ['page_headers'] * 2


def test_with_page_marks_a_call_that_breaks_maps_each_faxs_own_pages():
    # Pages: 1-2 doc one, 3 doc two, 4-6 doc three; nothing between documents.
    found = map_call(_marks_layout(2, 1, 3), succeeded=False, confirmed_pages=4)
    assert outcome(found) == [('success', None), ('success', None), ('failed', 'partly_sent')]
    assert found[2].sentence == 'The call failed after 1 of its 3 pages; check before sending again.'
    boundary = map_call(_marks_layout(2, 1, 3), succeeded=False, confirmed_pages=2)
    assert outcome(boundary) == [('success', None), ('failed', 'partly_sent'), ('failed', None)]
    # Nothing confirmed: no fax was sent, exactly as a single fax whose call failed (and a call that never connected).
    nothing = map_call(_marks_layout(2, 1), succeeded=False, confirmed_pages=0, failure_sentence='The number was busy.')
    assert outcome(nothing) == [('failed', None)] * 2 and {item.sentence for item in nothing} == {'The number was busy.'}
    assert outcome(map_call(_marks_layout(2, 1, 3), succeeded=True, confirmed_pages=5)) == [
        ('success', None), ('success', None), ('unconfirmed', 'pages_unconfirmed')]


def test_the_faxs_details_say_its_pages_carry_its_mark(sip):
    from api.app.batching import http
    sip = headers_on(sip)
    _, delivery, *_ = sip
    jobs = [accept(sip, pages=pages, at=T0 + timedelta(seconds=n)) for n, pages in enumerate((2, 3))]
    delivery.claim('worker', now=T0 + timedelta(minutes=10))
    view = http.summary(row(sip, jobs[1]))
    assert (view['layout'], view['call_first_page'], view['call_last_page']) == ('page_headers', 3, 5)
    assert http.layout_sentence(view) == ('A line at the top of each of its pages marks it as document 2 of 2 '
                                          f'(pages 3–5 of the call), under Faxbot {jobs[1][:8]}.')


# Charge shares and pages saved -----------------------------------------------------------------

def _priced_call(sip, *pages, status='SUCCESS', confirmed=None):
    from api.app.routing.capture import CostRecorder
    _, delivery, _, routes, _ = sip
    jobs, claim = _submitted(sip, *pages)
    for job in jobs:
        routes.record_decision(attempt_id=delivery.get(job)['attempt_id'], job_id=job, destination=NUMBER,
                               route='sip', reason='configured', provider_id='sip')
    total = row(sip, jobs[-1])['last_page']  # every page of the call, whatever its layout
    results.apply_fax_result(delivery, {'JobID': claim.job_id, 'AttemptID': claim.attempt_id, 'Status': status,
                                        'Pages': str(total if confirmed is None else confirmed)})
    CostRecorder(routes, observed_seconds=lambda target: 100).step()
    return jobs, claim


def test_each_fax_pays_for_its_own_pages_and_the_index_page_is_shared_by_them(sip):
    configuration, _, _, routes, _ = sip
    index_on(sip)
    jobs, claim = _priced_call(sip, 1, 2, 1)
    assert routes.decision(claim.attempt_id)['estimated_cost_micros'] == 10_000  # 100 s: two whole minutes
    shares = [money.share(routes, configuration.engine, batching.member(configuration.engine, job))['amount_micros']
              for job in jobs]
    # Split 1 : 2 : 1 by the faxes' own pages; with separators it would have been 2 : 3 : 2.
    assert shares == [2500, 5000, 2500] and sum(shares) == 10_000


@pytest.mark.parametrize('boundaries, left_out', [('index_page', 2), ('page_headers', 3)])
def test_separator_pages_left_out_are_counted_apart_from_calls_saved_and_the_total_does_not_change(
        sip, boundaries, left_out):
    from api.app.routing.savings import savings
    configuration, _, _, routes, _ = sip
    sip = headers_on(sip) if boundaries == 'page_headers' else sip
    index_on(sip, boundaries=boundaries)
    _priced_call(sip, 1, 2, 1)
    found = money.savings(routes, configuration.engine, NUMBER)
    # Separate calls: 1 + 2 + 1 minutes at $0.005 = $0.02; the shared call was billed $0.01. Separators
    # would have made it 7 pages (4 minutes by estimate) instead of 5 with an index page or 4 with page
    # marks (3 minutes either way): $0.005.
    assert found['separator_pages'] == {'calls': 1, 'pages_saved': left_out, 'priced_calls': 1,
                                        'saved': {'USD': 5_000}}
    assert found['calls_saved'] == 2 and found['saved'] == {'USD': 5_000}
    assert found['saved']['USD'] + found['separator_pages']['saved']['USD'] == 20_000 - 10_000
    assert money.separator_pages_sentence(found['separator_pages']) == (
        f'An index page or marks at the top of every page left out {left_out} separator pages in 1 shared call, '
        'about $0.005 saved (estimate).')
    report = savings(routes, configuration.engine)
    assert report['separator_pages']['pages_saved'] == left_out
    assert report['separator_pages']['saved'] == report['sending_together']['saved'] == {'USD': 5_000}
    assert report['total'] == {'USD': 10_000}
    assert report['separator_pages']['sentence'] == money.separator_pages_sentence(found['separator_pages'])


def test_pages_saved_count_only_calls_that_delivered_every_fax_and_unknown_prices_stay_unknown(sip):
    configuration, _, _, routes, _ = sip
    index_on(sip)
    _priced_call(sip, 1, 2, status='FAILED', confirmed=2)  # document two never arrived whole
    found = money.savings(routes, configuration.engine, NUMBER)
    assert found['calls'] == 1 and found['separator_pages']['calls'] == 0
    assert money.separator_pages_sentence(found['separator_pages']) == (
        'No shared call in the last 30 days left out its separator pages.')
    _priced_call(sip, 1, 1)
    routes.replace_cards([])  # no rate card: the pages are counted, their price is not
    found = money.savings(routes, configuration.engine, NUMBER)
    assert found['separator_pages'] == {'calls': 1, 'pages_saved': 1, 'priced_calls': 0, 'saved': {}}
    assert money.separator_pages_sentence(found['separator_pages']) == (
        'An index page or marks at the top of every page left out 1 separator page in 1 shared call. '
        "Some of those pages have no price, because your carrier's prices are not entered in Costs.")
