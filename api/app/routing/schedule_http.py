"""Recipients, Details → "When to send": the hours a recipient takes faxes and the busy hours Faxbot learned.

``GET /routing/destinations/{number}/schedule`` shows both, with what a failed
try costs on the route Faxbot would use; ``PUT`` saves the recipient's hours
(its own request, such as "business hours only", in its time zone) and whether
Faxbot may learn its busy hours. Every save is a new row; the newest counts
(``routing/schedule.py``).
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from ..access.route_policy import require_permission
from ..config_runtime import run_lifecycle_step
from .background import installation_engine
from .numbers import InvalidNumber, normalize_number
from . import schedule


router = APIRouter(prefix='/routing', tags=['Delivery routes'])


class ScheduleIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    # An IANA time zone name, such as America/New_York; empty or None: the installation's own.
    time_zone: str | None = Field(default=None, max_length=64)
    # Days the recipient takes faxes ('mon' … 'sun'); None or empty: every day.
    days: list[str] | None = Field(default=None, max_length=7)
    # 'HH:MM' the recipient starts and stops taking faxes; both None: any time of day.
    start: str | None = Field(default=None, max_length=5)
    end: str | None = Field(default=None, max_length=5)
    learn_busy: StrictBool = True


def _values(request):
    return request.scope['faxbot.configuration'].active.values


def _number(value, request):
    try:
        return normalize_number(value, country=getattr(_values(request), 'fax_default_country', 'US'))
    except InvalidNumber as error:
        raise HTTPException(400, detail=str(error)) from None


def _engine(request):
    engine, _ = installation_engine(request.app)
    if engine is None:
        raise HTTPException(503, detail='Recipient schedules are unavailable. Try again.')
    return engine


def _route(request):
    """The outbound provider's identity, or '' when none is set."""
    _, runtime = installation_engine(request.app)
    snapshot = request.scope['faxbot.configuration']
    identity = snapshot.active.profile_id('outbound')
    if runtime is None or identity is None:
        return ''
    return runtime.manager.store.read_profile(identity).configuration.provider_id


def _clock_text(minute):
    return None if minute is None else f'{minute // 60:02d}:{minute % 60:02d}'


def call_hours(timing, settings):
    """The learned call hours for Recipients details and the CLI (M26): each hour with enough calls, the typical
    hour, and one sentence on what Faxbot does with them."""
    own = timing is not None and timing.scope == 'number'
    facts = timing.facts() if own else []
    if not settings.learn_busy:
        sentence = "Faxbot does not learn this number's hours."
    elif not facts:
        sentence = 'Faxbot has too few calls to this number to compare its hours yet.'
    else:
        hours = round(schedule.HOUR_WAIT.total_seconds() / 3600)
        sentence = (f'An ordinary fax may wait up to {hours} hours for an hour in which calls to this number take '
                    'much less time a page or fail less often after the fax machine answers. Urgent faxes always go '
                    'at once.')
    typical = timing.typical if own and timing.typical is not None else None
    return {'call_hours': [{'label': fact.label(), 'sentence': fact.summary()} for fact in facts],
            'typical_hour': typical.summary() if typical is not None and (typical.timed or typical.judged) else None,
            'call_hours_sentence': sentence}


def view(engine, number, values, route, now):
    """What Recipients, Details and ``faxbot recipients schedule show`` display."""
    from ..provider_labels import provider_label
    scheduler = schedule.for_engine(engine)
    with engine.connect() as connection:
        settings = scheduler.settings(connection, number, values)
        busy = scheduler.busy_hours(connection, number, settings, now)
        timing = scheduler.timing(connection, number, settings, now) if settings.learn_busy else None
    hours = settings.hours
    zone = f' in their time zone ({settings.zone_name})' if settings.zone_set else ''
    hours_sentence = ('Faxbot sends to this recipient at any time.' if hours.always
                      else f'This recipient takes faxes only {schedule.hours_text(hours)}{zone}.')
    slots = busy.busy_slots()
    tries = schedule.failed_tries(route, getattr(values, 'sip_trunk_preset', '') or '')
    label = provider_label(route) if route else 'Your provider'
    free = slots and all(tries.charge(slot.kind if slot.kind in schedule.UNREACHABLE else 'busy') == 'free'
                         for slot in slots)
    if not settings.learn_busy:
        busy_sentence = "Faxbot does not learn this number's busy hours."
    elif not slots:
        busy_sentence = 'Faxbot has not seen a busy hour for this number in the last 30 days.'
    elif free:
        busy_sentence = (f'A failed try costs nothing with {label}, so faxes are not held for these hours; they go '
                         'after other faxes waiting for the same line.')
    else:
        busy_sentence = ('Ordinary faxes wait until these hours end, unless their send-by time does not allow it. '
                         'Urgent faxes always go at once.')
    return {
        'number': number,
        'time_zone': settings.zone_name if settings.zone_set else '',
        'installation_time_zone': getattr(values, 'time_zone', '') or '',
        'days': None if hours.days is None else [schedule.DAYS[day] for day in sorted(hours.days)],
        'start': _clock_text(hours.start), 'end': _clock_text(hours.end),
        'learn_busy': settings.learn_busy,
        'hours_sentence': hours_sentence,
        'busy_hours': [{'label': slot.label(), 'sentence': slot.summary()} for slot in slots]
        if settings.learn_busy else [],
        'busy_sentence': busy_sentence,
        **call_hours(timing, settings),
        'failed_try': {'route': route or None, 'label': label, 'sentence': tries.note,
                       'sources': list(tries.sources), 'read_on': tries.read_on},
    }


@router.get('/destinations/{number}/schedule', dependencies=[Depends(require_permission('settings:read'))])
async def get_schedule(number: str, request: Request):
    target = _number(number, request)
    values, route = _values(request), _route(request)
    return await run_lifecycle_step(lambda: view(_engine(request), target, values, route, datetime.utcnow()))


@router.put('/destinations/{number}/schedule')
async def put_schedule(number: str, payload: ScheduleIn, request: Request,
                       identity=Depends(require_permission('settings:write'))):
    target = _number(number, request)
    values, route = _values(request), _route(request)
    actor = getattr(getattr(identity, 'actor', None), 'principal_id', None)
    try:
        days = schedule.parse_days(payload.days)
        start, end = schedule.parse_clock(payload.start), schedule.parse_clock(payload.end, end=True)
        zone = schedule.usable_zone(payload.time_zone)
    except ValueError as error:
        raise HTTPException(400, detail=str(error)) from None

    def save():
        engine = _engine(request)
        schedule.for_engine(engine).save(target, time_zone=zone, days=days, start=start, end=end,
                                         learn_busy=payload.learn_busy, actor=actor)
        # Held faxes are looked at again at the next claim, under the new hours.
        from ..capacity import for_engine as capacity_for
        capacity_for(engine).forget_holds()
        return view(engine, target, values, route, datetime.utcnow())
    try:
        result = await run_lifecycle_step(save)
    except ValueError as error:
        raise HTTPException(400, detail=str(error)) from None
    except RuntimeError:
        raise HTTPException(503, detail='Recipient schedules are unavailable. Try again.') from None
    from ..audit import audit_event
    audit_event('recipient_schedule', number=target, days=result['days'], start=result['start'], end=result['end'],
                time_zone=result['time_zone'] or None, learn_busy=payload.learn_busy)
    return result
