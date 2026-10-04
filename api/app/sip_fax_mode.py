"""T.38 or audio fax for new trunk calls, and why.

Faxbot chooses audio fax by itself in two cases, and records why so every
screen can say it in one sentence:

- ``no_data_back``: a call switched to T.38 and no fax data came back. The
  failed fax is never sent again; only new calls use audio fax.
- ``network``: a new Telnyx trunk on a network that changes port numbers,
  where Telnyx's T.38 data was seen not to come back.
- ``carrier``: a new trunk with a carrier that turns T.38 into audio fax inside
  its own network (BT One Voice), so audio fax is what reaches the far end.

A person's own choice (the switch, "Try T.38 again", ``faxbot trunk mode``)
is recorded as ``chosen`` and is never overridden by the network rule. The
record lives next to the trunk files in ``<FAX_DATA_DIR>/asterisk/fax-mode``;
the setting itself (``sip_t38_enabled``) is saved like any other, by "system"
when Faxbot changed it.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
import re
from pathlib import Path

from . import sip_trunk

NO_DATA_BACK = 'no_data_back'
# Only the fax engine's T0/T1 timeouts show that T.38 data never came back; a
# plain hang-up, a busy line or the other side hanging up never switches the
# installation. Stored call reasons are cut short, so the start is enough.
_T38_TIMEOUT = re.compile(r'timed out waiting for (?:initial commu|the first mess)', re.IGNORECASE)


def t38_timeout(text) -> bool:
    """Whether the fax engine's words for a call are a T0/T1 timeout."""
    return bool(_T38_TIMEOUT.search(str(text or '')))

NETWORK = 'network'
CARRIER = 'carrier'
CHOSEN = 'chosen'


def record_path(values) -> Path:
    return Path(values.fax_data_dir) / 'asterisk' / 'fax-mode'


def read(values):
    """The recorded decision ({mode, reason, at}), or None when Faxbot never decided."""
    try:
        record = json.loads(record_path(values).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) and record.get('mode') in ('t38', 'audio') else None


def write(values, mode, reason, at=None, *, derived=False):
    """Record a decision; ``at`` is a datetime or an ISO time from a call record."""
    path = record_path(values)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if isinstance(at, str):
        when = at if at.endswith('Z') else at + 'Z'
    else:
        when = (at or datetime.now(timezone.utc)).replace(tzinfo=None).isoformat(timespec='seconds') + 'Z'
    record = {'mode': mode, 'reason': reason, 'at': when}
    if derived:
        record['derived'] = True
    sip_trunk._write_private(path, json.dumps(record) + '\n')
    return record


def _call_records():
    """The call records of the running installation, when its fax engine connection is attached."""
    from . import sip_calls
    return sip_calls._current


def derive(values, records=None):
    """T.38 is off with no record of why: read the reason from the call history, once.

    When the most recent T.38 call (either direction) got no fax data back, no
    T.38 call has succeeded since, so that call is why audio fax is in use; it
    is recorded as Faxbot's reason, marked as derived, with that call's end
    time. Anything else leaves no record (a person's own choice).
    """
    if values.sip_t38_enabled or read(values) is not None or not sip_trunk.configured(values):
        return None
    records = records or _call_records()
    if records is None:
        return None
    cursor = None
    for _ in range(4):
        try:
            page = records.page(cursor=cursor, limit=50)
        except Exception:
            return None
        for call in page['items']:
            if call.get('t38') != 'yes':
                continue
            if call.get('verdict') != 'no_t38_data_back' or not t38_timeout(call.get('error_cause')):
                return None
            return write(values, 'audio', NO_DATA_BACK, call.get('ended_at') or call.get('started_at'), derived=True)
        cursor = page.get('next_cursor')
        if not cursor:
            return None
    return None


def off_sentence(reason, day='', carrier=''):
    """One sentence for why new calls use audio fax; ``day`` is the date in the reader's own words."""
    if reason == NO_DATA_BACK:
        return (f'Off: {"on " + day + " " if day else ""}a T.38 fax got no fax data back on this network, '
                'so Faxbot uses audio fax.')
    if reason == NETWORK:
        return ('Off: your network changes port numbers, and Telnyx\'s T.38 fax data does not come back through '
                'such networks, so Faxbot uses audio fax.')
    if reason == CARRIER:
        return f'Off: {carrier or "your carrier"} turns T.38 into audio fax inside its network, so Faxbot uses audio fax.'
    return None


def reason_for(values, records=None):
    """Why new calls use audio fax ({reason, at}), or None when T.38 is on or a person turned it off."""
    if values.sip_t38_enabled:
        return None
    record = read(values) or derive(values, records)
    if record and record['mode'] == 'audio' and record.get('reason') in (NO_DATA_BACK, NETWORK):
        return {'reason': record['reason'], 'at': record.get('at')}
    if record and record['mode'] == 'audio' and record.get('reason') == CARRIER and _carrier_prefers_audio(values):
        return {'reason': CARRIER, 'at': record.get('at')}
    return None


def _carrier_prefers_audio(values):
    preset = sip_trunk.PRESETS.get(values.sip_trunk_preset)
    return bool(preset and preset.audio_by_default)


def carrier_prefers_audio(values, *, has_calls):
    """A new trunk with a carrier that turns T.38 into audio itself starts with audio fax."""
    return bool(_carrier_prefers_audio(values) and values.sip_t38_enabled and read(values) is None
                and not has_calls)


def reconcile(values):
    """Record a person's own choice: whatever the switch says now, unless Faxbot's reason still stands."""
    record = read(values)
    mode = 't38' if values.sip_t38_enabled else 'audio'
    if record and record['mode'] == mode:
        return record
    return write(values, mode, CHOSEN)


def network_prefers_audio(values, network, *, has_calls):
    """A new Telnyx trunk on a network that changes port numbers starts with audio fax."""
    return bool(values.sip_trunk_preset == 'telnyx' and values.sip_t38_enabled and read(values) is None
                and not has_calls and network is not None and network.public_ip and network.ports == 'changes')


# After a call: switch new calls to audio fax when T.38 data never came back ----------------------------

_runtime = None
_pending: set = set()
# How often and how long Faxbot waits for calls to end before Asterisk loads audio fax.
BUSY_RETRY_SECONDS = 5
BUSY_WAIT_SECONDS = 120


def _on_fax_event(event):
    """AMI listener for FaxResult and FaxInboundCall; runs in the event loop and never raises."""
    try:
        from .sip_calls import _reason, verdict
        if _runtime is None or verdict(event) != 'no_t38_data_back' or not t38_timeout(_reason(event)):
            return
        task = asyncio.get_running_loop().create_task(switch_to_audio(_runtime, NO_DATA_BACK))
        _pending.add(task)
        task.add_done_callback(_pending.discard)
    except Exception:
        logging.getLogger(__name__).warning('Faxbot could not check the last call for audio fax.')


def attach(ami_client, runtime):
    """Listen for calls whose T.38 data never came back; safe to call on every start."""
    global _runtime
    _runtime = runtime
    ami_client.on_fax_result(_on_fax_event)
    ami_client.on_inbound_call(_on_fax_event)


def detach():
    global _runtime
    _runtime = None


async def switch_to_audio(runtime, reason):
    """Save audio fax for new calls (by "system"), record why, write the trunk and let Asterisk load it.

    Never resends anything: the failed fax keeps its result. Returns what
    happened with the fax engine, or None when nothing changed.
    """
    from .config_runtime import run_lifecycle_step
    from .config_store import ConfigurationConflict

    def save():
        for _ in range(3):
            snapshot = runtime.manager.store.read()
            values = snapshot.desired.values
            if not values.sip_t38_enabled or not sip_trunk.configured(values):
                return None
            try:
                runtime.manager.patch(snapshot, {'sip_t38_enabled': False}, actor='system')
            except ConfigurationConflict:
                continue
            values = runtime.manager.store.read().active.values
            write(values, 'audio', reason)
            sip_trunk.write_asterisk_configuration(values)
            return values
        return None

    try:
        values = await run_lifecycle_step(save)
        if values is None:
            return None
        try:
            from .audit import audit_event
            audit_event('sip_audio_fax_chosen', backend='sip')
        except Exception:
            pass
        from .sip_http import _load_into_engine
        # The event arrives from the call's hangup handler, while its channel still
        # exists: wait for it (and any other call) to end before restarting Asterisk.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + BUSY_WAIT_SECONDS
        result = await _load_into_engine(values)
        while result.get('engine') == 'busy' and loop.time() < deadline:
            await asyncio.sleep(BUSY_RETRY_SECONDS)
            result = await _load_into_engine(values)
        return result
    except Exception:
        logging.getLogger(__name__).warning('Faxbot could not switch new calls to audio fax.')
        return None
