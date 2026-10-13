"""The California court profile over HTTP (``court/``): Delivery setup → Routing rules, Send a fax and Sent.

- ``GET /court/profile``: where the profile applies (mailboxes and routing rules), the rules it rests on with their
  sources, the court holidays it counts, and which courts take direct fax filing.
- ``PUT /court/profile/attachments``: attach the profile to a mailbox or a routing rule, or take it off.
- ``GET /court/faxes/{fax}``: what Sent details say about a court fax; ``GET /court/faxes/{fax}/record``: its
  transmission record with a draft proof, as a PDF.
- ``POST /court/deadline``: a deadline you worked out from a fax service date, extended by two court days.
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool
import sqlalchemy as sa

from ..access.http import private_operation, require_identity
from ..access.route_policy import require_permission
from ..config_runtime import run_lifecycle_step
from . import record, rules, store

router = APIRouter(prefix='/court', tags=['Court profile'])
UNAVAILABLE = 'The court profile is unavailable right now. Try again in a moment.'


class AttachIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: str = Field(pattern='^(mailbox|rule)$')
    target: str = Field(min_length=1, max_length=100)
    on: StrictBool


class DeadlineIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    deadline: date


def _engine(request):
    from ..routing.background import installation_engine
    engine, runtime = installation_engine(request.app)
    if engine is None or runtime is None:
        raise HTTPException(503, detail=UNAVAILABLE)
    return engine


def _names(engine):
    """Mailbox labels and routing rule names, so the console and the command line show names, not IDs."""
    mailboxes = sa.table('mailboxes', sa.column('id'), sa.column('label'))
    with engine.connect() as connection:
        labels = {row[0]: row[1] for row in connection.execute(sa.select(mailboxes.c.id, mailboxes.c.label))}
    import json
    names = {}
    revisions = sa.table('routing_rule_revisions', sa.column('id'), sa.column('document'))
    state = sa.table('routing_rule_state', sa.column('active_revision_id'))
    # Every scope's active rules (the organization's, each mailbox's and each workflow's): a rule ID names one rule.
    with engine.connect() as connection:
        texts = connection.execute(sa.select(revisions.c.document).join(
            state, state.c.active_revision_id == revisions.c.id)).scalars().all()
    for text in texts:
        try:
            document = json.loads(text) if text else {}
        except ValueError:
            continue
        for section in ('routes', 'limits'):
            for rule in document.get(section) or ():
                if isinstance(rule, dict) and rule.get('id'):
                    names[rule['id']] = rule.get('name') or rule['id']
    return labels, names


def profile_view(engine):
    labels, names = _names(engine)
    attached = []
    for item in store.attachments(engine):
        name = (labels if item['kind'] == 'mailbox' else names).get(item['target'], item['target'])
        attached.append({**item, 'name': name,
                         'sentence': (f'Faxes from the {name} mailbox go under the California court profile.'
                                      if item['kind'] == 'mailbox' else
                                      f'Faxes whose route the rule "{name}" chooses go under the California court '
                                      'profile.')})
    return {
        'attached': attached,
        'summary': ('Faxes under this profile go exactly as given, by a fax call to the number given, with the MC-005 '
                    'cover sheet first when filing, and their transmission record is kept as your proof.'),
        'rules': list(rules.RULE_SOURCES),
        'holidays': {'source': rules.HOLIDAYS_SOURCE, 'years': sorted(rules.HOLIDAYS),
                     'days': [{'date': day.isoformat(), 'name': name}
                              for year in sorted(rules.HOLIDAYS) for day, name in rules.HOLIDAYS[year]]},
        'courts': [{'county': item.county, 'status': item.status, 'note': item.note, 'fax': item.fax,
                    'cutoff': item.cutoff.strftime('%H:%M') if item.cutoff else None, 'source': item.source,
                    'source_date': item.source_date, 'read_on': rules.READ_ON} for item in rules.COURTS],
        'not_established': list(rules.NOT_ESTABLISHED), 'trend': rules.TREND,
        'mailboxes': [{'id': key, 'label': value} for key, value in sorted(labels.items(), key=lambda kv: kv[1])],
        'routing_rules': [{'id': key, 'name': value} for key, value in sorted(names.items(), key=lambda kv: kv[1])],
    }


@router.get('/profile', dependencies=[Depends(require_permission('settings:read'))])
async def get_profile(request: Request):
    engine = _engine(request)
    return await run_lifecycle_step(lambda: profile_view(engine))


@router.put('/profile/attachments')
async def put_attachment(payload: AttachIn, request: Request, identity=Depends(require_permission('settings:write'))):
    engine = _engine(request)
    actor = {'id': getattr(getattr(identity, 'actor', None), 'principal_id', None),
             'name': getattr(getattr(identity, 'actor', None), 'display_name', None)}

    def change():
        labels, names = _names(engine)
        known = labels if payload.kind == 'mailbox' else names
        if payload.on and payload.target not in known:
            raise store.CourtStoreError('Choose one of your mailboxes.' if payload.kind == 'mailbox'
                                        else 'Choose one of your routing rules.')
        changed = store.set_attached(engine, payload.kind, payload.target, payload.on, actor=actor)
        return changed, profile_view(engine)
    try:
        changed, view = await run_lifecycle_step(change)
    except store.CourtStoreError as error:
        raise HTTPException(400, detail=str(error)) from None
    return {**view, 'changed': changed}


async def _readable(request, identity, job_id):
    runtime = request.app.state.access_runtime
    await run_lifecycle_step(private_operation(lambda: runtime.queries.job(identity.actor, job_id)))


@router.get('/faxes/{job_id}')
async def fax_view(job_id: str, request: Request, identity=Depends(require_identity)):
    """What Sent details say about a court fax, for anyone who may read that fax; ``court`` None otherwise."""
    await _readable(request, identity, job_id)
    engine = _engine(request)
    return {'court': await run_lifecycle_step(lambda: record.view(engine, job_id))}


@router.get('/faxes/{job_id}/record')
async def fax_record(job_id: str, request: Request, identity=Depends(require_identity)):
    """The court fax's transmission record with a draft proof, as a PDF, for anyone who may read that fax."""
    await _readable(request, identity, job_id)
    engine = _engine(request)
    try:
        data = await run_lifecycle_step(lambda: record.record_pdf(engine, job_id))
    except LookupError:
        raise HTTPException(404, detail='This fax was not sent under the court profile.') from None
    return Response(data, media_type='application/pdf',
                    headers={'Content-Disposition': f'attachment; filename="transmission-record-{job_id[:8]}.pdf"'})


@router.post('/deadline')
async def extend_deadline(payload: DeadlineIn, identity=Depends(require_identity)):
    """A deadline worked out from a fax service date, extended by two court days; for anyone signed in."""
    try:
        moved, sentence = rules.extended(payload.deadline)
    except rules.CourtCalendarUnknown as error:
        raise HTTPException(400, detail=str(error)) from None
    return {'deadline': payload.deadline.isoformat(), 'extended': moved.isoformat(), 'sentence': sentence}
