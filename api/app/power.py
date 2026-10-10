"""Power-aware admission: start a call only when the office's power can see it through (brief 92, RF).

Optional, and on by itself as soon as you set a UPS: Faxbot reads it through NUT (``nut.py``) at the address
you give (``power_sources``, newest row; ``host`` None turns it off). Nothing changes while the UPS is on mains.

On battery, before a call starts, Faxbot compares what the call needs from this server with the UPS's remaining
runtime (``power_allows``): a call on this server's own trunk needs the fax's p90 duration from the predictor
(``Prediction.p90_seconds``), plus the reserve you set; a fax service only needs Faxbot to hand the fax over
(``HAND_OVER_SECONDS``), because the service places the call on its own power. When the call does not fit,
Faxbot prefers an approved route in another power domain, or holds the fax, unsubmitted, in Sent with one
sentence. An unreadable UPS or an unknown duration never holds a fax: Diagnostics says so instead.

Nothing here sends, resends or drops a fax: the start rules (jo's ``schedule.py``/``capacity.py``) ask
``admission_for`` and keep the fax ready while it says hold, urgent or not; an uncertain outcome keeps its
usual reconciliation. Not yet run against a real UPS.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import threading
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
import sqlalchemy as sa

from . import nut
from .access.route_policy import require_permission
from .config_runtime import run_lifecycle_step


DEFAULT_RESERVE_SECONDS = 120
HAND_OVER_SECONDS = 60
CACHE_SECONDS = 20.0
THIS_SERVER, SERVICE = 'this_server', 'service'
# Accounts whose calls run on this server's own fax engine, so the whole call needs this office's power.
LOCAL_PROVIDERS = ('sip', 'freeswitch')


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def power_allows(runtime_seconds, needed_seconds, reserve_seconds) -> bool:
    """Whether a call needing ``needed_seconds`` fits in the UPS's runtime with ``reserve_seconds`` to spare.

    Unknown runtime or unknown need never refuses: only evidence holds a fax. Pure."""
    if runtime_seconds is None or needed_seconds is None:
        return True
    return float(needed_seconds) + max(0.0, float(reserve_seconds or 0)) <= float(runtime_seconds)


def domain(provider) -> str:
    """The power domain a route's call depends on: this server, or the fax service's own."""
    return THIS_SERVER if str(provider or '').lower() in LOCAL_PROVIDERS else SERVICE


# -- settings ------------------------------------------------------------------------------------------------

def _table(db):
    return sa.Table('power_sources', sa.MetaData(), autoload_with=db)


def settings(db) -> dict | None:
    """The UPS in force: its newest row, or None when none is set or the newest turned it off."""
    table = _table(db)
    with db.connect() as connection:
        row = connection.execute(sa.select(table).order_by(table.c.created_at.desc(), table.c.id.desc())
                                 .limit(1)).mappings().one_or_none()
    return dict(row) if row is not None and row['host'] else None


def save(db, host, *, port=nut.PORT, ups_name=None, reserve_seconds=DEFAULT_RESERVE_SECONDS, actor_id=None,
         actor_name=None, now=None) -> dict:
    """Set the UPS (a new row; the older stay as history). ``host`` empty turns the UPS check off."""
    host = (host or '').strip()[:255] or None
    if host and any(char.isspace() for char in host):
        raise ValueError('Enter the UPS server\'s address without spaces, such as 192.168.1.5 or ups.local.')
    try:
        port = int(port or nut.PORT)
        reserve_seconds = int(reserve_seconds if reserve_seconds is not None else DEFAULT_RESERVE_SECONDS)
    except (TypeError, ValueError):
        raise ValueError('Enter whole numbers for the port and the reserve.') from None
    if not 1 <= port <= 65535:
        raise ValueError('Enter a port from 1 to 65535; NUT uses 3493.')
    if not 0 <= reserve_seconds <= 3600:
        raise ValueError('Enter a reserve from 0 to 60 minutes.')
    row = {'id': uuid.uuid4().hex, 'host': host, 'port': port, 'ups_name': (ups_name or '').strip()[:64] or None,
           'reserve_seconds': reserve_seconds, 'created_at': now or utcnow(),
           'created_by': (str(actor_id)[:40] if actor_id else None),
           'created_by_name': (str(actor_name)[:200] if actor_name else None)}
    with db.begin() as connection:
        connection.execute(_table(db).insert().values(**row))
    _cache.clear()
    return row


# -- the UPS's state -----------------------------------------------------------------------------------------

@dataclass(frozen=True)
class PowerState:
    configured: bool
    reading: nut.Reading | None = None
    error: str | None = None
    reserve_seconds: int = DEFAULT_RESERVE_SECONDS

    @property
    def on_battery(self) -> bool:
        return bool(self.reading and self.reading.on_battery)

    @property
    def runtime_seconds(self):
        return self.reading.runtime_seconds if self.reading else None


_cache: dict = {}
_cache_lock = threading.Lock()


def state(db, *, reader=nut.read) -> PowerState:
    """The UPS now, read at most every ``CACHE_SECONDS`` (a claim may ask for every fax it considers)."""
    current = settings(db)
    if current is None:
        return PowerState(False)
    key = (current['id'],)
    with _cache_lock:
        cached = _cache.get(key)
        if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
            return cached[1]
    try:
        found = PowerState(True, reader(current['host'], port=current['port'], ups=current['ups_name']),
                           reserve_seconds=current['reserve_seconds'])
    except nut.NutError as error:
        found = PowerState(True, error=str(error), reserve_seconds=current['reserve_seconds'])
    with _cache_lock:
        _cache[key] = (time.monotonic(), found)
    return found


def _minutes(seconds):
    minutes = max(1, round(float(seconds) / 60))
    return f'{minutes} minute' + ('' if minutes == 1 else 's')


def state_view(found: PowerState) -> dict:
    """{'status', 'sentence'} for Diagnostics: ok, attention or off."""
    if not found.configured:
        return {'status': 'off', 'sentence': 'No UPS is set. Set its address so Faxbot holds long calls while the '
                                             'office runs on battery.'}
    if found.reading is None:
        return {'status': 'attention', 'sentence': f'Faxbot could not read the UPS: {found.error} Faxes are sent '
                                                   'as usual until it can.'}
    runtime = found.reading.runtime_seconds
    if found.on_battery:
        left = f'about {_minutes(runtime)} left' if runtime is not None else 'an unknown time left'
        return {'status': 'attention', 'sentence': f'On battery, with {left}. Faxbot starts a call on this server '
                f'only when it can finish with {_minutes(found.reserve_seconds)} to spare.'}
    if runtime is not None:
        return {'status': 'ok', 'sentence': f'On mains power. The battery would last about {_minutes(runtime)}.'}
    return {'status': 'ok', 'sentence': 'On mains power. The UPS does not say how long its battery would last.'}


# -- admission -----------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Admission:
    action: str               # 'go', 'other' (use ``account``) or 'hold'
    sentence: str | None = None
    account: str | None = None


def admission(found: PowerState, *, provider, p90_seconds, urgent=False, others=()) -> Admission:
    """Whether a call may start on a route of ``provider`` now. ``others``: approved alternative routes, each
    ``(account key, label, provider, p90 seconds)``, best first. Pure."""
    if not found.on_battery:
        return Admission('go')
    runtime, reserve = found.runtime_seconds, found.reserve_seconds

    def need(route_provider, p90):
        return p90 if domain(route_provider) == THIS_SERVER else HAND_OVER_SECONDS

    if power_allows(runtime, need(provider, p90_seconds), reserve):
        return Admission('go')
    left = f'about {_minutes(runtime)}'
    call = f'up to {_minutes(p90_seconds)}' if p90_seconds is not None else 'longer'
    for key, label, other_provider, other_p90 in others:
        if domain(other_provider) != domain(provider) and power_allows(runtime, need(other_provider, other_p90),
                                                                        reserve):
            return Admission('other', f'This office is on battery with {left} left and this fax\'s call could take '
                                      f'{call}, so Faxbot sends it by {label}, whose call does not depend on this '
                                      'office\'s power.', key)
    sentence = (f'This office is on battery with {left} left and this fax\'s call could take {call}, so Faxbot '
                'holds it, unsent, until the power is back or the battery can cover the call.')
    if urgent:
        sentence += ' It is urgent: if you have another way to send it, use it now.'
    return Admission('hold', sentence)


def admission_for(db, values, account_key, p90_seconds, *, urgent=False, others=(), reader=nut.read) -> Admission:
    """The start rules' hook (jo): ``admission`` for an account by its key, with this installation's UPS."""
    from .accounts import account_named
    found = state(db, reader=reader)
    if not found.on_battery:
        return Admission('go')
    account = account_named(values, account_key)
    provider = account.provider if account is not None else account_key
    return admission(found, provider=provider, p90_seconds=p90_seconds, urgent=urgent, others=others)


# -- HTTP: Administration → System health → Power ----------------------------------------------------------------------

router = APIRouter(prefix='/power', tags=['Diagnostics'])
UNAVAILABLE = 'Power settings are unavailable right now. Try again in a moment.'


class PowerIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    host: str | None = Field(default=None, max_length=255)
    port: int = Field(default=nut.PORT, ge=1, le=65535)
    ups_name: str | None = Field(default=None, max_length=64)
    reserve_minutes: int = Field(default=DEFAULT_RESERVE_SECONDS // 60, ge=0, le=60)


def _engine(request):
    from .routing.background import installation_engine
    engine, _ = installation_engine(request.app)
    if engine is None:
        raise HTTPException(503, detail=UNAVAILABLE)
    return engine


def _view(db):
    current = settings(db)
    found = state(db)
    view = state_view(found)
    reading = found.reading
    return {'configured': current is not None, 'host': current['host'] if current else None,
            'port': current['port'] if current else nut.PORT, 'ups_name': current['ups_name'] if current else None,
            'reserve_minutes': (current['reserve_seconds'] // 60) if current else DEFAULT_RESERVE_SECONDS // 60,
            'status': view['status'], 'sentence': view['sentence'], 'on_battery': found.on_battery,
            'runtime_minutes': (round(reading.runtime_seconds / 60) if reading and reading.runtime_seconds is not None
                                else None),
            'charge_percent': reading.charge_percent if reading else None}


@router.get('', dependencies=[Depends(require_permission('settings:read'))])
async def get_power(request: Request):
    """The UPS Faxbot reads and what it says now."""
    engine = _engine(request)
    return await run_lifecycle_step(lambda: _view(engine))


@router.put('')
async def put_power(payload: PowerIn, request: Request, identity=Depends(require_permission('settings:write'))):
    """Set the UPS Faxbot reads through NUT, or turn the check off with an empty address."""
    engine = _engine(request)

    def change():
        principal = getattr(getattr(identity, 'actor', None), 'principal_id', None)
        try:
            save(engine, payload.host, port=payload.port, ups_name=payload.ups_name,
                 reserve_seconds=payload.reserve_minutes * 60, actor_id=principal)
        except ValueError as error:
            raise HTTPException(400, detail=str(error)) from None
        return _view(engine)
    return await run_lifecycle_step(change)
