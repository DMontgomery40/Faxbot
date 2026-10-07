"""Sending rules over HTTP: each scope's rules, its draft, check, publish, history, and the dry run.

The organization's and a workflow's rules need ``settings:read`` to read and
``settings:write`` to change. A mailbox's rules need ``mailboxes:read`` and
``mailboxes:manage``, which are granted at the installation, as for every other
mailbox change. Every route requires an identity; the scope's permission is
checked in the handler, because the scope is a parameter.
"""
from datetime import datetime, timedelta, timezone
import json

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
import sqlalchemy as sa

from ..access.http import require_identity
from ..config_runtime import run_lifecycle_step
from ..routing.background import installation_engine
from ..routing.database import DeliveryStoreError, read_connection, utcnow
from ..routing.numbers import InvalidNumber
from . import model
from .check import CheckContext, check, compile_for_replay, replay
from .compile import DocumentError
from .explain import FactsReader, accounts_from_values, explanation
from .evaluate import decide
from .store import RuleStore, RulesConflict, RulesInputError, compile_scopes, when
from . import text


router = APIRouter(prefix='/routing', tags=['Sending rules'])
MATCH_WINDOW_DAYS = 30


# Plumbing ----------------------------------------------------------------------------------------------------

def _engine(request):
    engine, _ = installation_engine(request.app)
    if engine is None:
        raise HTTPException(503, detail='Installation configuration is not ready.')
    return engine


def _access(request):
    from ..access.http import runtime
    return runtime(request)


def _store(request):
    try:
        return RuleStore(_engine(request), access_store=_access(request).store)
    except DeliveryStoreError:
        raise HTTPException(503, detail='Sending rules are unavailable.') from None


def _values(request):
    snapshot = request.scope.get('faxbot.configuration')
    if snapshot is None:
        raise HTTPException(503, detail='Installation configuration is not ready.')
    return snapshot.active.values


async def _call(operation):
    try:
        return await run_lifecycle_step(operation)
    except RulesConflict as error:
        raise HTTPException(409, detail=str(error)) from None
    except RulesInputError as error:
        detail = str(error)
        if error.problems:
            raise HTTPException(400, detail={'message': detail, 'errors': [_issue(p) for p in error.problems]}) \
                from None
        raise HTTPException(400, detail=detail) from None
    except DocumentError as error:
        raise HTTPException(400, detail={'message': 'Fix the problems in these rules first.',
                                         'errors': [_issue(p) for p in error.problems]}) from None
    except InvalidNumber as error:
        raise HTTPException(400, detail=str(error)) from None
    except DeliveryStoreError:
        raise HTTPException(503, detail='Sending rules are unavailable.') from None


def _issue(problem):
    return {'rule_id': problem.rule_id, 'message': problem.message, 'code': problem.code, 'path': problem.path}


def _mailbox_on(connection, mailbox_id):
    mailboxes = sa.table('mailboxes', sa.column('id'), sa.column('label'))
    return connection.execute(sa.select(mailboxes.c.label).where(mailboxes.c.id == mailbox_id)).scalar_one_or_none()


def _scope(request, value):
    """``(kind, scope_id, display name)`` for a ``scope`` parameter; 404 for a mailbox or workflow that doesn't exist."""
    try:
        kind, scope_id = model.parse_scope(value or model.ORGANIZATION)
    except ValueError:
        raise HTTPException(400, detail='Choose the organization, a mailbox or a workflow.') from None
    if kind == model.ORGANIZATION:
        return kind, scope_id, 'Organization'
    if kind == model.MAILBOX:
        with read_connection(_engine(request)) as connection:
            label = _mailbox_on(connection, scope_id)
        if label is None:
            raise HTTPException(404, detail='There is no such mailbox.')
        return kind, scope_id, label
    store = _store(request)
    names = {}
    for found in (store.active(model.ORGANIZATION), store.draft(model.ORGANIZATION)):
        if found is not None:
            for item in json.loads(found['document']).get('workflows') or ():
                if isinstance(item, dict):
                    names.setdefault(item.get('key'), item.get('name'))
    if scope_id not in names:
        raise HTTPException(404, detail='There is no such workflow. Add it to the organization’s workflows first.')
    return kind, scope_id, names[scope_id]


def _permission(kind, write):
    if kind == model.MAILBOX:
        return 'mailboxes:manage' if write else 'mailboxes:read'
    return 'settings:write' if write else 'settings:read'


def _allowed(request, identity, kind, scope_id, write):
    """Whether the caller holds the scope's permission. Mailbox permissions are granted at the installation (as
    every mailbox change checks them), so a mailbox's rules follow them there."""
    from ..access.types import ResourceRef
    service = _access(request)
    with service.store.transaction() as connection:
        service.store.require_lock_on(connection)
        return service.control.authorize_on(connection, identity.actor, _permission(kind, write),
                                            ResourceRef('installation'), now=utcnow()).allowed


async def _require(request, identity, kind, scope_id, *, write=False):
    from ..access.types import AccessUnavailableError, AuthenticationError
    try:
        allowed = await run_lifecycle_step(lambda: _allowed(request, identity, kind, scope_id, write))
    except AuthenticationError:
        raise HTTPException(401, detail='Your sign-in has expired. Sign in again.') from None
    except AccessUnavailableError:
        raise HTTPException(503, detail='Access control is unavailable.') from None
    if not allowed:
        raise HTTPException(403, detail='You don’t have permission to change these rules.' if write
                            else 'You don’t have permission to see these rules.')


def _actor_name(request, identity):
    principals = sa.table('access_principals', sa.column('id'), sa.column('display_name'))
    principal = getattr(identity.actor, 'principal_id', None)
    if not principal:
        return None
    with read_connection(_engine(request)) as connection:
        return connection.execute(sa.select(principals.c.display_name).where(principals.c.id == principal)
                                  ).scalar_one_or_none()


# Views -------------------------------------------------------------------------------------------------------

def _revision_view(row, *, document=True):
    if row is None:
        return None
    view = {'number': row['number'], 'note': row['note'], 'actor_name': row['actor_name'],
            'created_at': when(row['created_at'])}
    if document:
        view['document'] = json.loads(row['document'])
    return view


def _draft_view(row, check_result=None):
    if row is None:
        return None
    stored = json.loads(row['check_result']) if row.get('check_result') else None
    return {'document': json.loads(row['document']), 'version': row['version'], 'base_revision': row['base_revision'],
            'actor_name': row['actor_name'], 'updated_at': when(row['updated_at']),
            'check': check_result if check_result is not None else stored}


def _choices(request, accounts):
    engine = _engine(request)
    principals = sa.table('access_principals', sa.column('id'), sa.column('kind'), sa.column('display_name'),
                          sa.column('enabled'))
    bindings = sa.table('access_key_bindings', sa.column('id'), sa.column('state'))
    keys = sa.table('api_keys', sa.column('id'), sa.column('name'))
    groups = sa.table('access_groups', sa.column('id'), sa.column('name'), sa.column('enabled'))
    mailboxes = sa.table('mailboxes', sa.column('id'), sa.column('label'))
    destinations = sa.table('delivery_destinations', sa.column('id'), sa.column('display_name'),
                            sa.column('phone_number'))
    with read_connection(engine) as connection:
        people = connection.execute(sa.select(principals.c.id, principals.c.display_name, principals.c.kind).where(
            principals.c.kind.in_(('user', 'integration')), principals.c.enabled == 1)
            .order_by(principals.c.display_name)).all()
        key_rows = connection.execute(sa.select(bindings.c.id, keys.c.name).join(keys, keys.c.id == bindings.c.id)
                                      .where(bindings.c.state == 'active').order_by(keys.c.name)).all()
        group_rows = connection.execute(sa.select(groups.c.id, groups.c.name).where(groups.c.enabled == 1)
                                        .order_by(groups.c.name)).all()
        mailbox_rows = connection.execute(sa.select(mailboxes.c.id, mailboxes.c.label).order_by(mailboxes.c.label)
                                          ).all()
        recipient_rows = connection.execute(sa.select(destinations.c.id, destinations.c.display_name,
                                                      destinations.c.phone_number)).all()
    # Saved recipients as Recipients lists them: the display name, else the number.
    recipients = sorted(({'id': row[0], 'name': (row[1] or '').strip() or row[2]} for row in recipient_rows),
                        key=lambda item: (item['name'].casefold(), item['id']))
    return {
        'accounts': [{'key': a.key, 'label': a.label or text.account_label(a.key), 'provider': a.provider,
                      'sends': a.sends, 'enabled': a.enabled, 'site': a.site} for a in accounts],
        'people': [{'id': row[0], 'name': row[1], 'kind': row[2]} for row in people],
        'keys': [{'id': row[0], 'name': row[1]} for row in key_rows],
        'groups': [{'id': row[0], 'name': row[1]} for row in group_rows],
        'mailboxes': [{'id': row[0], 'name': row[1]} for row in mailbox_rows],
        'recipients': recipients,
    }


def _matches(store, kind, scope_id):
    """Faxes each rule of this scope's active rules matched in the last 30 days, from stored decisions.

    Every rule starts at 0: stored decisions keep only the steps that decided something, so a rule that
    matched nothing never appears in them. A match a more specific or mandatory rule overrode still counts.
    """
    since = utcnow() - timedelta(days=MATCH_WINDOW_DAYS)
    decisions = store.decisions
    active = store.active(kind, scope_id)
    document = json.loads(active['document']) if active else {}
    counts = {rule.get('id'): 0 for section in ('limits', 'routes') for rule in document.get(section) or ()
              if isinstance(rule, dict) and rule.get('id')}
    with read_connection(store.engine) as connection:
        rows = connection.execute(sa.select(decisions.c.decision).where(
            decisions.c.sequence == 1, decisions.c.created_at >= since)
            .order_by(decisions.c.created_at.desc()).limit(20_000)).scalars()
        for row in rows:
            try:
                trace = json.loads(row).get('trace') or ()
            except ValueError:
                continue
            for step in trace:
                if step.get('scope') == kind and step.get('scope_id') == scope_id and step.get('rule_id') \
                        and step.get('kind') != 'preferred' and step.get('result') in ('matched', 'not_applied'):
                    counts[step['rule_id']] = counts.get(step['rule_id'], 0) + 1
    return counts


def _accounts(request):
    return accounts_from_values(_values(request))


def _context(request, store, kind, scope_id, accounts):
    engine = _engine(request)
    from ..routing.store import RouteStore
    routes = RouteStore(engine)
    principals = sa.table('access_principals', sa.column('id'))
    bindings = sa.table('access_key_bindings', sa.column('id'))
    groups = sa.table('access_groups', sa.column('id'))
    mailboxes = sa.table('mailboxes', sa.column('id'))
    destinations = sa.table('delivery_destinations', sa.column('id'))
    peers = sa.table('direct_peers', sa.column('phone_number'), sa.column('state'))
    with read_connection(engine) as connection:
        ids = lambda table: frozenset(connection.execute(sa.select(table.c.id)).scalars())  # noqa: E731
        context = dict(people=ids(principals), keys=ids(bindings), groups=ids(groups), mailboxes=ids(mailboxes),
                       recipients=ids(destinations),
                       partners=frozenset(connection.execute(sa.select(peers.c.phone_number).where(
                           peers.c.state == 'verified')).scalars()))
    from ..routing.costs import estimate_cost
    prices = {}
    for account in accounts:
        card = routes.card_for(account.key)
        prices[account.key] = (estimate_cost(card, 1), card.currency) if card is not None else None
    return CheckContext(accounts=accounts, prices=prices, matches_30_days=_matches(store, kind, scope_id), **context)


def _organization_document(store):
    """The organization's draft if one exists, else its active document: what lower scopes are checked against."""
    found = store.draft(model.ORGANIZATION) or store.active(model.ORGANIZATION)
    return json.loads(found['document']) if found else None


def _check_view(problems, replay_view=None):
    return {'errors': [_issue(p) for p in problems if p.level == 'error'],
            'warnings': [_issue(p) for p in problems if p.level == 'warning'], 'replay': replay_view}


def _scope_names(request, store):
    names = {}
    with read_connection(_engine(request)) as connection:
        mailboxes = sa.table('mailboxes', sa.column('id'), sa.column('label'))
        names.update({row[0]: row[1] for row in connection.execute(sa.select(mailboxes.c.id, mailboxes.c.label))})
    document = _organization_document(store) or {}
    names.update({item['key']: item['name'] for item in document.get('workflows') or () if isinstance(item, dict)})
    return names


# Routes ------------------------------------------------------------------------------------------------------

@router.get('/rules')
async def get_rules(request: Request, scope: str = Query(default='organization', max_length=110),
                    identity=Depends(require_identity)):
    kind, scope_id, name = _scope(request, scope)
    await _require(request, identity, kind, scope_id)
    store = _store(request)
    can_write = True
    try:
        await _require(request, identity, kind, scope_id, write=True)
    except HTTPException:
        can_write = False
    accounts = _accounts(request)

    def read():
        organization = store.active(model.ORGANIZATION) if kind != model.ORGANIZATION else None
        return (store.active(kind, scope_id), store.draft(kind, scope_id), organization,
                _matches(store, kind, scope_id), _choices(request, accounts))
    active, draft, organization, matches, choices = await _call(read)
    return {'scope': {'kind': kind, 'id': scope_id or None, 'name': name}, 'active': _revision_view(active),
            'draft': _draft_view(draft), 'organization': _revision_view(organization), 'matches_30_days': matches,
            'can_write': can_write, 'choices': choices,
            'time_zone': getattr(_values(request), 'time_zone', '') or 'UTC'}


class DraftBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    document: dict
    expected_version: int = Field(ge=0)


@router.put('/rules/draft')
async def save_draft(body: DraftBody, request: Request, scope: str = Query(default='organization', max_length=110),
                     identity=Depends(require_identity)):
    kind, scope_id, _ = _scope(request, scope)
    await _require(request, identity, kind, scope_id, write=True)
    store, accounts = _store(request), _accounts(request)
    name = _actor_name(request, identity)

    def save():
        draft = store.save_draft(kind, scope_id, body.document, expected_version=body.expected_version,
                                 actor=identity.actor, actor_name=name)
        problems = check(kind, scope_id, body.document, _context(request, store, kind, scope_id, accounts),
                         organization=_organization_document(store) if kind != model.ORGANIZATION else None)
        return _draft_view(draft, _check_view(problems))
    return await _call(save)


@router.delete('/rules/draft')
async def discard_draft(request: Request, scope: str = Query(default='organization', max_length=110),
                        identity=Depends(require_identity)):
    kind, scope_id, _ = _scope(request, scope)
    await _require(request, identity, kind, scope_id, write=True)
    store = _store(request)
    removed = await _call(lambda: store.discard_draft(kind, scope_id, actor=identity.actor))
    return {'discarded': removed}


class CheckBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    replay: int = Field(default=200, ge=0, le=1000)


def _replay_entries(store, limit):
    entries = []
    for row in store.recent_decisions(limit):
        try:
            facts = model.Facts.from_json(row['facts'])
            before = model.Decision.from_json(row['decision'])
        except (ValueError, TypeError):
            continue
        entries.append((row['job_id'], row['to_number'], when(row['created_at']), facts, before))
    return entries


def _approximate_entries(request, store, limit, accounts):
    """Recent faxes accepted before rules existed, rebuilt with today's preferences: approximate."""
    if limit <= 0:
        return []
    from ..routing.store import RouteStore
    jobs = sa.table('fax_jobs', sa.column('id'), sa.column('to_number'), sa.column('pages', sa.Integer()),
                    sa.column('created_at', sa.DateTime()), sa.column('urgent', sa.Integer()),
                    sa.column('send_by_call', sa.Integer()))
    decisions = store.decisions
    with read_connection(store.engine) as connection:
        rows = connection.execute(sa.select(jobs).where(~sa.exists().where(decisions.c.job_id == jobs.c.id))
                                  .order_by(jobs.c.created_at.desc(), jobs.c.id.desc()).limit(limit)).mappings().all()
    reader = FactsReader(store.engine, _values(request), RouteStore(store.engine))
    entries = []
    for row in rows:
        try:
            facts = reader.read(to_number=row['to_number'], accounts=accounts, pages=row['pages'] or 1,
                                urgent=bool(row['urgent']), by_call=bool(row['send_by_call']),
                                accepted_at=row['created_at'])
        except (InvalidNumber, ValueError):
            continue
        facts = model.Facts.from_json(json.dumps({**json.loads(facts.to_json()), 'approximate': True}))
        entries.append((row['id'], row['to_number'], when(row['created_at']), facts, None))
    return entries


@router.post('/rules/draft/check')
async def check_draft(body: CheckBody, request: Request, scope: str = Query(default='organization', max_length=110),
                      identity=Depends(require_identity)):
    kind, scope_id, _ = _scope(request, scope)
    await _require(request, identity, kind, scope_id)
    store, accounts = _store(request), _accounts(request)

    def run():
        draft = store.draft(kind, scope_id)
        if draft is None:
            raise RulesInputError('There is no draft to check. Change a rule first.')
        document = json.loads(draft['document'])
        organization = _organization_document(store) if kind != model.ORGANIZATION else None
        problems = check(kind, scope_id, document, _context(request, store, kind, scope_id, accounts),
                         organization=organization)
        replay_view = None
        if body.replay and not any(p.level == 'error' for p in problems):
            compiled = compile_for_replay(kind, scope_id, document, store.compiled_active())
            entries = _replay_entries(store, body.replay)
            entries += _approximate_entries(request, store, body.replay - len(entries), accounts)
            checked, changed = replay(entries, compiled, accounts)
            names = _scope_names(request, store)
            replay_view = {
                'checked': checked, 'changed': len(changed),
                'approximate': sum(1 for entry in entries if entry[4] is None),
                'sentence': (f'{len(changed)} of your last {checked} faxes would go differently.' if changed
                             else f'None of your last {checked} faxes would go differently.' if checked
                             else 'There are no sent faxes to compare yet.'),
                'items': [{'job_id': item.job_id, 'to_number': item.to_number, 'accepted_at': item.accepted_at,
                           'before': text.decision_sentence(item.before, accounts, names),
                           'after': text.decision_sentence(item.after, accounts, names),
                           'approximate': item.approximate} for item in changed]}
        result = _check_view(problems, replay_view)
        store.record_check(kind, scope_id, draft['version'], result)
        return result
    return await _call(run)


class PublishBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_active_revision: int | None = Field(default=None, ge=1)
    expected_draft_version: int = Field(ge=1)
    note: str | None = Field(default=None, max_length=200)


@router.post('/rules/publish')
async def publish(body: PublishBody, request: Request, scope: str = Query(default='organization', max_length=110),
                  identity=Depends(require_identity)):
    kind, scope_id, _ = _scope(request, scope)
    await _require(request, identity, kind, scope_id, write=True)
    store, accounts = _store(request), _accounts(request)
    name = _actor_name(request, identity)

    def run():
        draft = store.draft(kind, scope_id)
        if draft is not None:
            organization = _organization_document(store) if kind != model.ORGANIZATION else None
            problems = [p for p in check(kind, scope_id, json.loads(draft['document']),
                                         _context(request, store, kind, scope_id, accounts),
                                         organization=organization) if p.level == 'error']
            if problems and draft['version'] == body.expected_draft_version:
                raise RulesInputError('Fix the problems in these rules before publishing them.', problems)
        return _revision_view(store.publish(kind, scope_id, expected_active_revision=body.expected_active_revision,
                                            expected_draft_version=body.expected_draft_version, note=body.note,
                                            actor=identity.actor, actor_name=name), document=False)
    return await _call(run)


@router.get('/rules/revisions')
async def list_revisions(request: Request, scope: str = Query(default='organization', max_length=110),
                         identity=Depends(require_identity)):
    kind, scope_id, _ = _scope(request, scope)
    await _require(request, identity, kind, scope_id)
    store = _store(request)
    rows = await _call(lambda: store.history(kind, scope_id))
    return {'revisions': [_revision_view(row, document=False) for row in rows]}


async def _one_revision(request, identity, scope, number):
    kind, scope_id, _ = _scope(request, scope)
    await _require(request, identity, kind, scope_id)
    store = _store(request)
    row = await _call(lambda: store.revision(kind, scope_id, number))
    if row is None:
        raise HTTPException(404, detail=f'There is no version {number} of these rules.')
    return row


@router.get('/rules/revisions/{number}')
async def get_revision(number: int, request: Request, scope: str = Query(default='organization', max_length=110),
                       identity=Depends(require_identity)):
    return _revision_view(await _one_revision(request, identity, scope, number))


DIFF_SECTIONS = ('limits', 'routes', 'lists', 'labels', 'regions', 'sites', 'workflows')


def diff_documents(before, after):
    """Changes between two documents, by section: added, removed, changed or moved."""
    changes = []
    for section in DIFF_SECTIONS:
        old, new = before.get(section), after.get(section)
        if section == 'labels':
            for label in sorted(set(old or ()) - set(new or ())):
                changes.append({'change': 'removed', 'section': section, 'id': label, 'name': label,
                                'before': label, 'after': None})
            for label in sorted(set(new or ()) - set(old or ())):
                changes.append({'change': 'added', 'section': section, 'id': label, 'name': label,
                                'before': None, 'after': label})
            continue
        key = 'id' if section in ('limits', 'routes') else 'key'
        if isinstance(old, dict) or isinstance(new, dict):
            old_items = {k: {**v, 'key': k} for k, v in (old or {}).items()}
            new_items = {k: {**v, 'key': k} for k, v in (new or {}).items()}
            old_order, new_order = sorted(old_items), sorted(new_items)
        else:
            old_items = {item.get(key): item for item in old or () if isinstance(item, dict)}
            new_items = {item.get(key): item for item in new or () if isinstance(item, dict)}
            old_order, new_order = list(old_items), list(new_items)
        common = [k for k in old_order if k in new_items]
        moved = [k for k in new_order if k in old_items] != common
        for identity in old_order:
            if identity not in new_items:
                item = old_items[identity]
                changes.append({'change': 'removed', 'section': section, 'id': identity,
                                'name': item.get('name', identity), 'before': item, 'after': None})
        for identity in new_order:
            item = new_items[identity]
            if identity not in old_items:
                changes.append({'change': 'added', 'section': section, 'id': identity,
                                'name': item.get('name', identity), 'before': None, 'after': item})
            elif old_items[identity] != item:
                changes.append({'change': 'changed', 'section': section, 'id': identity,
                                'name': item.get('name', identity), 'before': old_items[identity], 'after': item})
            elif moved and old_order.index(identity) != new_order.index(identity) \
                    and section in ('limits', 'routes'):
                changes.append({'change': 'moved', 'section': section, 'id': identity,
                                'name': item.get('name', identity), 'before': old_order.index(identity) + 1,
                                'after': new_order.index(identity) + 1})
    return changes


@router.get('/rules/revisions/{first}/diff/{second}')
async def diff_revisions(first: int, second: int, request: Request,
                         scope: str = Query(default='organization', max_length=110),
                         identity=Depends(require_identity)):
    before = await _one_revision(request, identity, scope, first)
    after = await _one_revision(request, identity, scope, second)
    return {'from': first, 'to': second,
            'changes': diff_documents(json.loads(before['document']), json.loads(after['document']))}


@router.post('/rules/revisions/{number}/restore')
async def restore_revision(number: int, request: Request, scope: str = Query(default='organization', max_length=110),
                           identity=Depends(require_identity)):
    kind, scope_id, _ = _scope(request, scope)
    await _require(request, identity, kind, scope_id, write=True)
    store = _store(request)
    name = _actor_name(request, identity)
    return _draft_view(await _call(lambda: store.restore(kind, scope_id, number, actor=identity.actor,
                                                         actor_name=name)))


class ExplainBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    to: str = Field(max_length=64)
    pages: int | None = Field(default=1, ge=0, le=10_000)
    size_bytes: int | None = Field(default=0, ge=0)
    as_: str | None = Field(default=None, alias='as', max_length=100)
    mailbox: str | None = Field(default=None, max_length=100)
    workflow: str | None = Field(default=None, max_length=64)
    urgent: bool = False
    real_call: bool = False
    case_packet: bool = False
    labels: list[str] = Field(default_factory=list, max_length=50)
    at: datetime | None = None
    source: str | dict = 'active'
    scope: str | None = Field(default=None, max_length=110)


def _sender(request, identity, value):
    """``(principal, kind, key binding)`` for "me", a person or integration ID, or an integration key's ID."""
    if value in (None, '', 'me'):
        credential = getattr(identity.actor, 'credential', None)
        binding = getattr(credential, 'binding_id', None)
        return getattr(identity.actor, 'principal_id', None), 'key' if binding else 'person', binding
    principals = sa.table('access_principals', sa.column('id'), sa.column('kind'))
    bindings = sa.table('access_key_bindings', sa.column('id'), sa.column('principal_id'))
    with read_connection(_engine(request)) as connection:
        kind = connection.execute(sa.select(principals.c.kind).where(principals.c.id == value)).scalar_one_or_none()
        if kind is not None:
            return value, 'person' if kind == 'user' else 'key', None
        principal = connection.execute(sa.select(bindings.c.principal_id).where(bindings.c.id == value)
                                       ).scalar_one_or_none()
    if principal is None:
        raise HTTPException(400, detail='Choose a person or an integration that exists, or yourself.')
    return principal, 'key', value


def _local_to_utc(moment, zone_name):
    from ..people_time import zone
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=zone(zone_name))
    return moment.astimezone(timezone.utc).replace(tzinfo=None, microsecond=0)


@router.post('/explain')
async def explain_route(body: ExplainBody, request: Request, identity=Depends(require_identity)):
    """Which route a fax would take, and why. Sends nothing and writes nothing."""
    kind, scope_id, _ = _scope(request, body.scope)
    await _require(request, identity, kind, scope_id)
    store, values = _store(request), _values(request)
    accounts = accounts_from_values(values)
    principal, sender_kind, binding = _sender(request, identity, body.as_)
    from ..routing.store import RouteStore
    reader = FactsReader(store.engine, values, RouteStore(store.engine))
    at = _local_to_utc(body.at, getattr(values, 'time_zone', '')) if body.at is not None else None

    def run():
        facts = reader.read(to_number=body.to, accounts=accounts, pages=body.pages or 0,
                            size_bytes=body.size_bytes or 0, principal_id=principal, sender_kind=sender_kind,
                            key_id=binding, mailbox_id=body.mailbox, workflow=body.workflow, labels=body.labels,
                            urgent=body.urgent, by_call=body.real_call, case_packet=body.case_packet, accepted_at=at)
        compiled = store.compiled_active()
        if body.source == 'draft':
            draft = store.draft(kind, scope_id)
            if draft is None:
                raise RulesInputError('There is no draft to try. Change a rule first.')
            compiled = compile_for_replay(kind, scope_id, json.loads(draft['document']), compiled)
        elif isinstance(body.source, dict):
            number = body.source.get('revision')
            row = store.revision(kind, scope_id, number) if isinstance(number, int) else None
            if row is None:
                raise RulesInputError('There is no such version of these rules.')
            compiled = dict(compiled)
            compiled.update(compile_scopes({model.scope_name(kind, scope_id): row,
                                            **({model.ORGANIZATION: store.active(model.ORGANIZATION)}
                                               if kind != model.ORGANIZATION and store.active(model.ORGANIZATION)
                                               else {})}))
        elif body.source != 'active':
            raise RulesInputError('Try the active rules, the draft or an earlier version.')
        decision = decide(compiled, facts, accounts)
        return explanation(decision, facts, accounts, scope_names=_scope_names(request, store))
    return await _call(run)
