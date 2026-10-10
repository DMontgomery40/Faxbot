"""Line windows and receiver preflight between partners (brief 82; research CE4 and E; migration 0084).

Two installations go through the real direct signing (``test_peer_fax.peer_pair``): B, the running application, is
the receiver with limited lines; A, a direct service with its own delivery store, sends to B by phone. A's real
claim gate (``OutboundStore.claim`` → ``capacity.next_ready``) is what holds or releases each fax. Every number,
document and line count is synthetic. Not yet run against a real partner.
"""
from datetime import datetime, timedelta
import json
from uuid import uuid4

import pytest
import sqlalchemy as sa

from app import main
from api.app import schema, schema_partner_admission
from api.app.direct import admission, http as direct_http
from api.tests.test_digital_schema import _downgrade, _tables
from api.tests.test_direct_delivery import B_NUMBER, pdf_bytes
from api.tests.test_peer_fax import peer_pair  # noqa: F401 - fixture
from api.tests.test_routing_http import ADMIN
from api.tests.test_schema import database  # noqa: F401 - fixture


def by_phone(pair):
    """A's sending rule never lets a fax go directly, so a fax to B goes by a phone call (a real envelope)."""
    from types import SimpleNamespace
    from api.app.rules.store import RuleStore
    from api.tests.test_rules_delivery import publish, rule
    if not pair.get('by_phone'):
        publish(SimpleNamespace(rules=RuleStore(pair['a'].store.engine)),
                {'format': 1, 'limits': [rule('l-no-direct', {'never': ['direct']})]})
        pair['by_phone'] = True


def fax(pair, *, pages=2, urgent=False, send_by=None):
    """A fax to B that goes by phone, accepted the way POST /fax accepts one: its rules decision in the same
    transaction (the rule never allows direct delivery)."""
    from api.app.routing import rules_acceptance
    from api.tests.test_rules_delivery import ANNE
    by_phone(pair)
    job = uuid4().hex
    (pair['data'] / (job + '.pdf')).write_bytes(pdf_bytes('Synthetic referral'))
    engine = pair['a'].store.engine
    plan = rules_acceptance.prepare(engine, pair['snapshot'].active, actor=ANNE, destination=B_NUMBER, pages=pages,
                                    urgent=urgent)
    now = datetime.utcnow()
    with pair['configuration']._locked() as connection:
        pair['configuration']._accept_outbound_on(connection, pair['snapshot'].active, {
            'id': job, 'to_number': B_NUMBER, 'file_name': 'referral.pdf', 'tiff_path': '', 'status': 'queued',
            'pages': pages, 'created_at': now, 'updated_at': now,
            **({'urgent': 1} if urgent else {}), **({'send_by': send_by} if send_by else {})})
        rules_acceptance.recorder(plan, job, ANNE)(connection, now)
    return job


def take_part(pair, *, lines=1, reserve=0, refuse=(), reason=None, deadline='go', b_side=True):
    a, b = pair['a'], direct_http.service_for(main.app)
    admission.save_partner(a.store.engine, pair['b_on_a']['id'], take_part=True)
    if b_side:
        admission.save_settings(b.store.engine, lines=lines, reserve=reserve, deadline=deadline)
        admission.save_partner(b.store.engine, pair['a_on_b']['id'], take_part=True, refused_classes=refuse,
                               refusal_reason=reason)
    return a, b


def wait_of(pair, job, now=None):
    engine = pair['a'].store.engine
    with engine.connect() as connection:
        return admission.hold_on(connection, job, pair['values'], now or datetime.utcnow())


def claimed(pair, now=None):
    # The claim gate leaves a waiting fax alone for a few seconds (capacity.ADMISSION_RECHECK); step past that.
    now = now or datetime.utcnow()
    pair['delivery_now'] = max(pair.get('delivery_now') or now, now) + timedelta(seconds=6)
    claim = pair['delivery'].claim('worker-a', now=pair['delivery_now'])
    return None if claim is None else claim.job_id


def rows(engine, table, **where):
    found = sa.Table(table, sa.MetaData(), autoload_with=engine)
    with engine.connect() as connection:
        return [dict(row) for row in connection.execute(sa.select(found).where(
            *[found.c[key] == value for key, value in where.items()]).order_by(found.c.created_at)).mappings()]


@pytest.mark.asyncio
async def test_a_window_is_honoured_and_a_second_sender_queues_instead_of_calling_into_a_busy_line(peer_pair):  # noqa: F811
    pair = peer_pair
    a, b = take_part(pair, lines=1)
    first, second = fax(pair), fax(pair)
    # Before any answer, both wait: Faxbot is asking (no network call in the claim itself).
    assert wait_of(pair, first).sentence.startswith("Waiting: asking County Clinic's Faxbot whether it accepts")
    assert claimed(pair) is None
    await admission.Requester(a).step()
    # B granted the first fax its one partner line now, and the second a window after the first call's 3 minutes.
    issued = rows(b.store.engine, 'admission_grants', direction='issued')
    assert [row['outcome'] for row in issued] == ['granted', 'granted']
    assert issued[1]['earliest'] >= issued[0]['earliest'] + admission.DURATIONS['short']
    assert wait_of(pair, first) is None
    later = wait_of(pair, second)
    assert later.sentence.startswith("Waiting for County Clinic's line window until ")
    assert claimed(pair) == first
    assert claimed(pair) is None  # the second stays ready, unclaimed, never failed
    received = rows(a.store.engine, 'admission_grants', job_id=second)[0]
    assert claimed(pair, now=received['earliest'] + timedelta(seconds=1)) == second
    # Preflight said yes, which is not a receipt.
    answers = rows(a.store.engine, 'preflight_answers', direction='asked')
    assert {row['accepts'] for row in answers} == {1} and answers[0]['statement']
    view = pair['b_client'].get('/direct/admission', headers=ADMIN).json()
    assert view['not_a_receipt'].startswith('A line window is only permission to start a call')
    assert view['lines_sentence'] == 'Partners that take part share 1 line; 0 stay for other callers.'


@pytest.mark.asyncio
async def test_the_real_worker_waits_for_the_window_then_places_the_call(peer_pair):  # noqa: F811
    from api.app.outbound_worker import OutboundWorker
    from api.tests.test_peer_fax import transport
    pair = peer_pair
    a, _ = take_part(pair, lines=1)
    first, second = fax(pair), fax(pair)
    with a.store.engine.connect() as connection:
        assert admission.by_phone_on(connection, {'id': first}, pair['values']) is True
    await admission.Requester(a).step()
    routed, conventional = transport(pair)
    worker = OutboundWorker(pair['delivery'], routed)
    assert await worker.step() is True
    assert conventional.submissions == 1  # the first fax, by phone in its window; nothing delivered directly
    assert await worker.step() is False  # the second waits for its window, ready and unclaimed
    assert conventional.submissions == 1
    view = admission.fax_view(a.store.engine, first, pair['values'])
    assert any(sentence.endswith('That is not a receipt: it says nothing about whether this fax arrived.')
               for sentence in view['sentences'])
    assert any(sentence.startswith("Its call started in County Clinic's line window") for sentence in view['sentences'])
    waiting = admission.fax_view(a.store.engine, second, pair['values'])
    assert waiting['waiting'] and waiting['sentences'][0].startswith("Waiting for County Clinic's line window until")


@pytest.mark.asyncio
async def test_an_expired_window_is_asked_again_and_a_deadline_goes_without_waiting(peer_pair):  # noqa: F811
    pair = peer_pair
    a, b = take_part(pair, lines=1)
    occupying = fax(pair)
    await admission.Requester(a).step()
    # A fax due before its window would open goes at once: B's policy for deadlines is "go".
    soon = fax(pair, send_by=datetime.utcnow() + timedelta(minutes=2))
    await admission.Requester(a).step()
    assert rows(a.store.engine, 'admission_grants', job_id=soon)[0]['earliest'] > datetime.utcnow()
    assert wait_of(pair, soon) is None
    # The first fax's window passes unused: it waits and asks for a new one.
    window = rows(a.store.engine, 'admission_grants', job_id=occupying)[0]
    after = window['latest'] + timedelta(minutes=1)
    assert wait_of(pair, occupying, after).sentence.endswith('line window passed, so Faxbot is asking for a new one.')
    with a.store.engine.connect() as connection:
        assert 'grant' in admission.Requester(a)._needs(connection, occupying, after)


@pytest.mark.asyncio
async def test_an_uncertain_call_is_never_retried_on_a_new_window(peer_pair):  # noqa: F811
    pair = peer_pair
    a, b = take_part(pair, lines=2)
    job = fax(pair)
    await admission.Requester(a).step()
    claim = pair['delivery'].claim('worker-a')
    assert claim.job_id == job
    assert pair['delivery'].begin_submission(claim)
    pair['delivery'].record_uncertain(claim)  # the provider's answer was lost: the call may be on the line
    before = len(rows(b.store.engine, 'admission_grants'))
    await admission.Requester(a).step()
    await admission.Requester(a).step()
    assert len(rows(b.store.engine, 'admission_grants')) == before  # never asked again for an uncertain fax
    assert claimed(pair, now=datetime.utcnow() + timedelta(hours=1)) is None


@pytest.mark.asyncio
async def test_a_refusal_holds_the_fax_before_anything_is_sent(peer_pair):  # noqa: F811
    pair = peer_pair
    a, b = take_part(pair, lines=2, refuse=['general'], reason='Send referrals through our referral line instead.')
    job = fax(pair)
    posts = pair['to_b'].posts
    await admission.Requester(a).step()
    assert pair['to_b'].posts == posts + 1  # the preflight only: no window asked, no document sent
    held = wait_of(pair, job)
    assert held.sentence == ("Held: County Clinic doesn't accept general faxes at its fax number: Send referrals "
                             'through our referral line instead. Nothing was sent.')
    assert claimed(pair) is None
    assert rows(b.store.engine, 'direct_deliveries') == []


@pytest.mark.asyncio
async def test_permission_that_expires_before_the_call_is_asked_again(peer_pair):  # noqa: F811
    pair = peer_pair
    a, _ = take_part(pair, lines=2)
    job = fax(pair)
    await admission.Requester(a).step()
    answer = rows(a.store.engine, 'preflight_answers', job_id=job)[0]
    expired = answer['expires_at'] + timedelta(minutes=1)
    assert wait_of(pair, job, expired).sentence.endswith("answer about this fax has expired, so Faxbot is asking again.")
    with a.store.engine.connect() as connection:
        assert 'preflight' in admission.Requester(a)._needs(connection, job, expired)


@pytest.mark.asyncio
async def test_a_lost_answer_is_asked_again_and_never_counts_as_a_refusal(peer_pair):  # noqa: F811
    pair = peer_pair
    a, b = take_part(pair, lines=2)
    job = fax(pair)
    pair['to_b'].mode = 'lose_answer'
    await admission.Requester(a).step()
    assert rows(a.store.engine, 'preflight_answers', job_id=job) == []  # nothing kept from a lost answer
    assert wait_of(pair, job).sentence.startswith('Waiting: asking')
    pair['to_b'].mode = 'normal'
    await admission.Requester(a).step()
    # Asking again for the same fax returns the same open window: B issued one, not two.
    assert len([row for row in rows(b.store.engine, 'admission_grants', direction='issued')
                if row['outcome'] == 'granted']) == 1
    assert wait_of(pair, job) is None
    # A partner that never answers leaves the fax as it was before this feature, after the asking limit.
    later = datetime.utcnow() + admission.ASK_LIMIT + timedelta(minutes=1)
    other = fax(pair)
    pair['to_b'].mode = 'drop'
    assert wait_of(pair, other, later) is None


@pytest.mark.asyncio
async def test_legacy_callers_keep_their_reserve_and_an_older_partner_is_left_alone(peer_pair):  # noqa: F811
    pair = peer_pair
    a, b = take_part(pair, lines=None, reserve=1)
    # B's own lines with no trunk: one; the reserve keeps it for callers that don't take part.
    assert admission.partner_lines(b.values(), admission.settings(b.store.engine)) == 0
    job = fax(pair)
    await admission.Requester(a).step()
    assert rows(a.store.engine, 'admission_grants', job_id=job)[0]['outcome'] == 'not_taking_part'
    assert wait_of(pair, job) is None  # no partner line: the fax goes as before, by its own call
    legacy = pair['b_client'].get('/direct/admission', headers=ADMIN).json()['legacy']
    assert legacy['sentence'].endswith("came from callers that don't take part; your reserve lines carry them.")


class Older:
    """A's transport to a partner Faxbot from before line windows: it answers 404 to both requests."""
    async def request(self, method, url, **kwargs):
        return 404, {'detail': 'Not Found'}


@pytest.mark.asyncio
async def test_an_older_partner_answers_404_and_the_fax_goes_without_a_window(peer_pair):  # noqa: F811
    pair = peer_pair
    a, _ = take_part(pair, b_side=False)
    a.http = Older()
    job = fax(pair)
    await admission.Requester(a).step()
    assert rows(a.store.engine, 'preflight_answers', job_id=job)[0]['accepts'] is None
    assert rows(a.store.engine, 'admission_grants', job_id=job)[0]['outcome'] == 'not_taking_part'
    assert wait_of(pair, job) is None and claimed(pair) == job


def test_windows_are_sized_by_pages_and_slotted_around_granted_calls():
    assert admission.duration_class(3) == 'short' and admission.duration_class(16) == 'long'
    assert admission._first_slot(datetime(2026, 10, 10, 12, 0, 30), timedelta(minutes=3),
                                 [(datetime(2026, 10, 10, 12, 1), 'short')], 1) == datetime(2026, 10, 10, 12, 4)
    assert admission._first_slot(datetime(2026, 10, 10, 12, 0, 30), timedelta(minutes=3), [], 1) == datetime(
        2026, 10, 10, 12, 0, 30)


def test_the_retry_picture_uses_only_measured_calls(database):  # noqa: F811
    schema.upgrade_schema(database)
    peer = {'organization': 'County Clinic', 'phone_number': B_NUMBER}
    assert admission.advice(database, peer).startswith('Nothing measured yet: no calls to County Clinic')
    now = datetime.utcnow()
    with database.begin() as connection:
        for index in range(30):
            job = f'job-{index:02d}'
            connection.exec_driver_sql(
                "INSERT INTO fax_jobs (id, to_number, file_name, tiff_path, status, backend, created_at, updated_at) "
                "VALUES (?, ?, 'a.pdf', '', 'success', 'phaxio', ?, ?)" if database.dialect.name == 'sqlite' else
                "INSERT INTO fax_jobs (id, to_number, file_name, tiff_path, status, backend, created_at, updated_at) "
                "VALUES (%s, %s, 'a.pdf', '', 'success', 'phaxio', %s, %s)", (job, B_NUMBER, now, now))
            # Each fax met a busy line once in three calls: 10 of 30 first calls ended before any page.
            attempts = sa.Table('outbound_attempts', sa.MetaData(), autoload_with=connection)
            connection.execute(attempts.insert().values(id=f'att-{index:02d}', job_id=job, sequence=1, phase='failed'
                                                        if index < 10 else 'success', created_at=now,
                                                        ended_before_data=1 if index < 10 else None))
    sentence = admission.advice(database, peer, now)
    assert sentence == ('In the last 30 days, 30 faxes to County Clinic (1.0 a day) met a busy line or no answer on 10 '
                        'of 30 calls (33%), so its line is offered 1.5 calls a day: arrivals ÷ (1 − 0.33). Line '
                        'windows turn those retries into a queue.')


def test_0084_is_the_head_and_its_downgrade_keeps_what_partners_agreed(database):  # noqa: F811
    schema.upgrade_schema(database)
    assert schema_partner_admission.REVISION == '0084_partner_admission' == schema.HEAD
    assert schema.COURT_PROFILE == '0083_court_profile'
    assert schema_partner_admission.TABLES <= schema.STRICT_TABLES and schema_partner_admission.TABLES <= _tables(database)
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD
    _downgrade(database, '0083_court_profile')
    assert not schema_partner_admission.TABLES & _tables(database)
    schema.upgrade_schema(database)
    admission.save_settings(database, lines=2)
    with pytest.raises(schema.SchemaUpgradeError, match='cannot be undone'):
        _downgrade(database, '0083_court_profile')


def test_the_command_line_shows_and_sets_line_windows(monkeypatch, tmp_path):
    import httpx
    from typer.testing import CliRunner
    from app.cli.commands.admission import windows
    view = {'settings': {'lines': None, 'reserve': 1, 'window_minutes': 10, 'urgent': 'go', 'deadline': 'go'},
            'partner_lines': 1, 'lines_sentence': 'Partners that take part share 1 line; 1 stays for other callers.',
            'policy_sentence': 'Urgent faxes start without a window.', 'legacy': {'calls': 0, 'sentence': '0 calls.'},
            'not_a_receipt': 'A line window is only permission to start a call, never a receipt or a delivery.',
            'partners': [{'peer_id': 'p' * 32, 'organization': 'County Clinic', 'take_part': False,
                          'refused_classes': [], 'refusal_reason': None, 'version': 0, 'grants': [], 'preflights': [],
                          'advice': 'Nothing measured yet.'}]}
    sent = []

    def handle(request):
        if request.method == 'PUT':
            sent.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json=view)
    monkeypatch.setenv('FAXBOT_CLI_CONFIG', str(tmp_path / 'absent.toml'))
    import typer
    from app.cli import main as cli_main
    # The group's home is recipients partners windows (cli/nouns.py); this wrapper runs the same group under the
    # real root options, so the test does not depend on that registration line.
    wrapper = typer.Typer()
    wrapper.callback()(cli_main.main)
    wrapper.add_typer(windows, name='windows')
    with httpx.Client(transport=httpx.MockTransport(handle), base_url='https://faxbot.example') as http:
        def run(*args):
            return CliRunner().invoke(wrapper, ['--url', 'https://faxbot.example', '--key', 'synthetic', 'windows',
                                                *args], obj={'client_factory': lambda *_: (http, False)})
        shown = run('show')
        assert shown.exit_code == 0, shown.output
        assert 'only permission to start a call' in ' '.join(shown.output.split())
        assert run('partner', 'County Clinic', 'on', '--refuse', 'billing',
                   '--reason', 'Use our billing portal.').exit_code == 0
        assert sent[-1] == ('/direct/admission/partners/' + 'p' * 32, {
            'take_part': True, 'refused_classes': ['billing'], 'refusal_reason': 'Use our billing portal.'})
        assert run('settings', '--lines', 'trunk', '--reserve', '2').exit_code == 0
        assert sent[-1][1]['reserve'] == 2 and sent[-1][1]['lines'] is None
        assert run('settings', '--urgent', 'soon').exit_code != 0
    assert windows.info.help.startswith('Line windows and preflight')
