"""System → Diagnostics → "Sending routes": route problems, the 2-by-2 test and shared upstreams (brief 92, RF).

``GET /routing/families`` lists route problems (open first), recent 2-by-2 tests with what they show, and the
shared-upstream table. ``POST /routing/families/{id}/close`` closes a problem by hand. ``POST
/routing/families/tests`` records a test plan and sends nothing; ``POST /routing/families/tests/{id}/send/{cell}``
is the person's explicit action that sends one of its four test faxes. ``PUT /routing/upstreams/{provider}``
records what a provider is known to use upstream. The background watcher (``route_families.watch``) runs once a
minute and only reads call records and writes its own tables.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime
import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
import sqlalchemy as sa

from ..access.route_policy import require_permission
from ..config_runtime import run_lifecycle_step
from .background import installation_engine, lifespan_tasks, repeat
from . import route_families as families


UNAVAILABLE = 'Route problems are unavailable right now. Try again in a moment.'
WATCH_SECONDS = 60.0


class TestRefused(ValueError):
    """A route test that cannot be sent as asked; one plain sentence."""


def _background(app):
    def step():
        engine, _ = installation_engine(app)
        if engine is None:
            return False
        families.watch(engine)
        return False
    return [('faxbot-route-families', repeat(step, interval=WATCH_SECONDS, initial_delay=30.0,
                                             warning='Faxbot could not check its routes for shared problems just '
                                                     'now; it tries again in a minute.'))]


router = APIRouter(prefix='/routing', tags=['Delivery routes'], lifespan=lifespan_tasks(_background))


def _engine(request):
    engine, runtime = installation_engine(request.app)
    if engine is None or runtime is None or not families.available(engine):
        raise HTTPException(503, detail=UNAVAILABLE)
    return engine, runtime


def _values(request):
    return request.scope['faxbot.configuration'].active.values


def _label(values, key):
    from ..accounts import account_named
    account = account_named(values, key)
    return account.label if account is not None else key


def _actor(engine, identity):
    principal = getattr(getattr(identity, 'actor', None), 'principal_id', None)
    if not principal:
        return None, None
    principals = sa.table('access_principals', sa.column('id'), sa.column('display_name'))
    with engine.connect() as connection:
        name = connection.execute(sa.select(principals.c.display_name).where(
            principals.c.id == principal)).scalar_one_or_none()
    return principal, name


def _when(moment):
    from ..people_time import short
    return short(moment) if moment else None


def _day(text):
    """'10 October 2026' from a stored YYYY-MM-DD, or None."""
    if not text:
        return None
    try:
        day = datetime.strptime(text, '%Y-%m-%d')
    except ValueError:
        return None
    return f'{day.day} {day:%B %Y}'


def _incident_view(row, values):
    label = _label(values, row['account_key'])
    closed = row.get('closed')
    return {'id': row['id'], 'account': row['account_key'], 'account_label': label, 'transport': row['transport'],
            'phase': row['phase'], 'open': closed is None, 'change_cause': row['change_cause'],
            'since_text': _when(row['change_at']), 'closed_text': _when(closed['closed_at']) if closed else None,
            'closed_reason': closed['reason'] if closed else None, 'retired': closed['retired'] if closed else 0,
            'destinations': row['destinations'], 'sentence': families.incident_sentence(row, label),
            'advice': families.test_advice(row, label) if closed is None else None}


def _test_view(test, values):
    a, b = _label(values, test['route_a']), _label(values, test['route_b'])
    found = families.interpret(families.cell_states(test), route_a=a, route_b=b, number_a=test['number_a'],
                               number_b=test['number_b'])
    cells = []
    for cell in families.CELLS:
        account, number = families.cell_target(test, cell)
        item = test['cells'][cell]
        cells.append({'cell': cell, 'account': account, 'account_label': _label(values, account), 'number': number,
                      'state': item['state'] if item else 'not_sent', 'fax_id': item['job_id'] if item else None,
                      'sent_text': _when(item['sent_at']) if item else None})
    return {'id': test['id'], 'route_a': test['route_a'], 'route_b': test['route_b'], 'route_a_label': a,
            'route_b_label': b, 'number_a': test['number_a'], 'number_b': test['number_b'],
            'created_text': _when(test['created_at']), 'cells': cells, 'verdict': found['verdict'],
            'sentence': found['sentence']}


def _upstream_view(row):
    return {'provider': row['provider'], 'upstream': row['upstream'], 'source_url': row['source_url'],
            'source_day': _day(row['source_date']), 'note': row['note']}


def _choices(values):
    """Your sending accounts and your own numbers, for a test plan."""
    from ..accounts import sending_accounts
    from .own_numbers import account_numbers
    accounts = [{'key': account.key, 'label': account.label, 'provider': account.provider}
                for account in sending_accounts(values) if account.enabled]
    return accounts, sorted(account_numbers(values))


@router.get('/families', dependencies=[Depends(require_permission('settings:read'))])
async def list_families(request: Request):
    """Route problems (open first), recent 2-by-2 tests and the shared-upstream table."""
    engine, _ = _engine(request)
    values = _values(request)

    def read():
        rows = families.incidents(engine, limit=50)
        rows.sort(key=lambda row: (row['closed'] is not None, -row['opened_at'].timestamp()))
        accounts, numbers = _choices(values)
        return {'incidents': [_incident_view(row, values) for row in rows],
                'tests': [_test_view(test, values) for test in families.tests(engine, limit=10)],
                'upstreams': [_upstream_view(row) for row in families.upstreams(engine).values()
                              if row['upstream']],
                'accounts': accounts, 'numbers': numbers}
    return await run_lifecycle_step(read)


@router.post('/families/{incident_id}/close')
async def close_family(incident_id: str, request: Request, identity=Depends(require_permission('settings:write'))):
    """Close a route problem by hand, for example after fixing the trunk; its lessons are set aside."""
    engine, _ = _engine(request)
    values = _values(request)

    def close():
        actor, name = _actor(engine, identity)
        try:
            done = families.close(engine, incident_id, reason='person', actor_id=actor, actor_name=name)
        except LookupError:
            raise HTTPException(404, detail='No such route problem.') from None
        if not done:
            raise HTTPException(409, detail='This route problem has already ended.')
        [row] = [row for row in families.incidents(engine, limit=1000) if row['id'] == incident_id]
        return _incident_view(row, values)
    return await run_lifecycle_step(close)


class TestIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    route_a: str = Field(min_length=1, max_length=64)
    route_b: str = Field(min_length=1, max_length=64)
    number_a: str = Field(min_length=3, max_length=40)
    number_b: str = Field(min_length=3, max_length=40)
    incident_id: str | None = Field(default=None, max_length=40)


@router.post('/families/tests')
async def create_test(payload: TestIn, request: Request, identity=Depends(require_permission('settings:write'))):
    """Record a 2-by-2 test of two sending accounts against two of your own numbers. Nothing is sent."""
    engine, _ = _engine(request)
    values = _values(request)
    from .numbers import InvalidNumber, normalize_number
    accounts, numbers = _choices(values)
    keys = {account['key'] for account in accounts}
    for key in (payload.route_a, payload.route_b):
        if key not in keys:
            raise HTTPException(400, detail=f'{key} is not one of your sending accounts that is on.')
    country = getattr(values, 'fax_default_country', 'US') or 'US'
    chosen = []
    for number in (payload.number_a, payload.number_b):
        try:
            normal = normalize_number(number, country=country)
        except InvalidNumber as error:
            raise HTTPException(400, detail=str(error)) from None
        if normal not in numbers:
            raise HTTPException(400, detail=f'{normal} is not one of your own numbers. A 2-by-2 test sends only to '
                                            'numbers your accounts give you.')
        chosen.append(normal)

    def create():
        actor, name = _actor(engine, identity)
        try:
            row = families.create_test(engine, route_a=payload.route_a, route_b=payload.route_b, number_a=chosen[0],
                                       number_b=chosen[1], incident_id=payload.incident_id, actor_id=actor,
                                       actor_name=name)
        except ValueError as error:
            raise HTTPException(400, detail=str(error)) from None
        except sa.exc.IntegrityError:
            raise HTTPException(400, detail='That route problem is not recorded.') from None
        return _test_view(families.get_test(engine, row['id']), values)
    return await run_lifecycle_step(create)


@router.get('/families/tests/{test_id}', dependencies=[Depends(require_permission('settings:read'))])
async def get_test(test_id: str, request: Request):
    engine, _ = _engine(request)
    values = _values(request)

    def read():
        test = families.get_test(engine, test_id)
        if test is None:
            raise HTTPException(404, detail='No such test.')
        return _test_view(test, values)
    return await run_lifecycle_step(read)


def test_document(label, number, cell, when_text):
    """The one-page synthetic test fax for one cell."""
    from io import BytesIO
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
    output = BytesIO()
    pdf = canvas.Canvas(output, pagesize=letter)
    lines = [('Helvetica-Bold', 16, 'Faxbot route test'),
             ('Helvetica', 12, f'Sent by {label} to {number} ({cell.upper()} of a 2-by-2 test), {when_text}.'),
             ('Helvetica', 12, 'This page tests a route. It holds no document and needs no reply.')]
    y = 700
    for font, size, text in lines:
        pdf.setFont(font, size)
        pdf.drawString(72, y, text)
        y -= size + 18
    pdf.showPage()
    pdf.save()
    return output.getvalue()


def pin_step(engine, test_id, cell, account, label, actor, name):
    """The acceptance step that keeps the test fax on ``account`` only and records the cell's send, once."""
    def step(connection, now, job_id):
        from . import envelope as envelopes
        pinned = envelopes.load_on(connection, job_id)
        if pinned is None:
            raise TestRefused('Faxbot could not read the sending rules for this test fax, so it was not sent.')
        if pinned.decision.outcome != 'route':
            raise TestRefused('Your sending rules hold faxes to this number for approval, so a route test cannot '
                              'choose its route. Choose another of your numbers.')
        if not (pinned.automatic or account in pinned.envelope.accounts):
            raise TestRefused(f'Your sending rules do not let faxes to this number go by {label}. Choose another '
                              'account or number for the test.')
        decisions = envelopes.tables(connection)['fax_job_rule_decisions']
        sequence = connection.scalar(sa.select(sa.func.max(decisions.c.sequence)).where(
            decisions.c.job_id == job_id)) or 0
        envelope = replace(pinned.envelope, mode='one', accounts=(account,), local=False, direct=False, caps=(),
                           holds=(), strict_fallback=True)
        decision = replace(pinned.decision, outcome='route', reason=None, envelope=envelope)
        connection.execute(decisions.insert().values(
            id=uuid.uuid4().hex, job_id=job_id, sequence=sequence + 1, revisions=json.dumps(
                decision.revision_ids, sort_keys=True, separators=(',', ':')),
            facts=pinned.facts.to_json(), facts_digest=decision.facts_digest, decision=decision.compact().to_json(),
            dial_number=envelope.dial.number if envelope.dial else None,
            approval_id=envelope.dial.approval_id if envelope.dial else None, page_layout=envelope.page_layout,
            outcome='route', reason=f'Route test {cell.upper()}: sent by {account} only.'[:200],
            actor_principal_id=actor, created_at=now))
        try:
            families.record_send_on(connection, engine, test_id, cell, job_id, actor_id=actor, actor_name=name,
                                    now=now)
        except sa.exc.IntegrityError:
            raise TestRefused('This test fax was already sent. Start a new test to send it again.') from None
    return step


@router.post('/families/tests/{test_id}/send/{cell}')
async def send_test_fax(test_id: str, cell: str, request: Request,
                        identity=Depends(require_permission('settings:write'))):
    """Send one of a test's four faxes: your explicit action, once per cell (you also need to be allowed to send
    faxes). Faxbot never sends them by itself."""
    from ..access.http import runtime as access_runtime
    from .submit import accept_generated_fax
    engine, runtime = _engine(request)
    revision = request.scope['faxbot.configuration'].active
    values = revision.values
    if cell not in families.CELLS:
        raise HTTPException(404, detail='Choose a1, a2, b1 or b2.')
    if revision.profile_id('outbound') is None:
        raise HTTPException(409, detail='Outbound fax delivery is disabled in this configuration.')
    access = access_runtime(request)

    def send():
        test = families.get_test(engine, test_id)
        if test is None:
            raise HTTPException(404, detail='No such test.')
        if test['cells'][cell] is not None:
            raise HTTPException(409, detail='This test fax was already sent. Start a new test to send it again.')
        account, number = families.cell_target(test, cell)
        label = _label(values, account)
        actor, name = _actor(engine, identity)
        document = test_document(label, number, cell, _when(datetime.utcnow()) or '')
        try:
            job_id = accept_generated_fax(runtime, access, identity.actor, revision, to_number=number,
                                          document=document, file_name=f'route-test-{cell}.pdf', pages=1,
                                          after=pin_step(engine, test_id, cell, account, label, actor, name))
        except TestRefused as error:
            raise HTTPException(409, detail=str(error)) from None
        return {'fax_id': job_id, 'test': _test_view(families.get_test(engine, test_id), values),
                'sentence': f'The test fax to {number} by {label} is on its way. Its result shows here when it ends.'}
    return await run_lifecycle_step(send)


class UpstreamIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    upstream: str | None = Field(default=None, max_length=120)
    source_url: str | None = Field(default=None, max_length=500)
    source_date: str | None = Field(default=None, max_length=10)
    note: str | None = Field(default=None, max_length=300)


@router.put('/upstreams/{provider}')
async def put_upstream(provider: str, payload: UpstreamIn, request: Request,
                       identity=Depends(require_permission('settings:write'))):
    """Record which carrier a provider uses upstream, with where that is published; empty means unknown."""
    engine, _ = _engine(request)

    def save():
        actor, name = _actor(engine, identity)
        try:
            row = families.set_upstream(engine, provider, payload.upstream, source_url=payload.source_url,
                                        source_date=payload.source_date, note=payload.note, actor_id=actor,
                                        actor_name=name)
        except ValueError as error:
            raise HTTPException(400, detail=str(error)) from None
        return _upstream_view(row)
    return await run_lifecycle_step(save)
