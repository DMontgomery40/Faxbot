"""T.38 or audio fax for new trunk calls, and why.

Faxbot chooses audio fax by itself in two cases, and records why so every
screen can say it in one sentence:

- ``no_data_back``: a call switched to T.38 and no fax data came back. The
  failed fax is never sent again; only new calls use audio fax.
- ``network``: the network check (``sip_network``) says the carrier's T.38
  data cannot come back through this network (it changes port numbers). The
  check runs at start, on Apply, every few minutes and on "Check again", and
  each time Faxbot re-decides: once the network lets T.38 data come back,
  Faxbot turns T.38 on again by itself, recorded with the same reason.
- ``carrier``: a new trunk with a carrier that turns T.38 into audio fax inside
  its own network (BT One Voice), so audio fax is what reaches the far end.

A person's own choice (the switch, "Try T.38 again", ``faxbot trunk mode``)
is recorded as ``chosen`` with what the network allowed at that moment, and
the network rule leaves it alone until the network changes. Every record
carries that network verdict (``network``: open, blocked or unknown) so a
later check can tell a fixed network from the same one. The record lives
next to the trunk files in ``<FAX_DATA_DIR>/asterisk/fax-mode``;
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
# The SSL Fax engine (HylaFAX+) says it as E002 "No carrier detected" or E126
# "No receiver protocol (T.30 T1 timeout)" (sip_calls.engine_verdict).
_T38_TIMEOUT = re.compile(r'timed out waiting for (?:initial commu|the first mess)'
                          r'|No carrier detected|T\.30 T1 timeout|\bE(?:002|126)\b', re.IGNORECASE)


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


def write(values, mode, reason, at=None, *, derived=False, network=None):
    """Record a decision; ``at`` is a datetime or an ISO time from a call record, ``network`` what the
    network check allowed when it was made."""
    path = record_path(values)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if isinstance(at, str):
        when = at if at.endswith('Z') else at + 'Z'
    else:
        when = (at or datetime.now(timezone.utc)).replace(tzinfo=None).isoformat(timespec='seconds') + 'Z'
    record = {'mode': mode, 'reason': reason, 'at': when}
    if derived:
        record['derived'] = True
    if network:
        record['network'] = network
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
        if carrier == 'Telnyx':
            return ('Off: your network changes port numbers, so fax over IP (T.38) cannot work; Faxbot sends audio fax '
                    'until the network is fixed.')
        return ('Off: your network changes port numbers, so fax over IP (T.38) most likely cannot work; Faxbot sends '
                'audio fax until the network is fixed.')
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


def reconcile(values, network=None):
    """Record a person's own choice: whatever the switch says now, unless Faxbot's reason still stands.

    ``network`` is what the network check allowed before this choice. A switch
    nobody has moved yet (no record at all) is recorded without it, so the
    network rule may still decide for a new trunk.
    """
    record = read(values)
    mode = 't38' if values.sip_t38_enabled else 'audio'
    if record and record['mode'] == mode:
        return record
    return write(values, mode, CHOSEN, network=network if record else None)


def network_decision(values, verdict, *, previous=None, records=None):
    """'t38' or 'audio' when a network check should switch new calls, else None.

    ``verdict`` is what the check found (open, blocked or unknown), ``previous``
    what the network allowed before it. Faxbot turns T.38 off when fax data
    cannot come back, unless a person chose T.38 on this same network, and
    turns it on again once the network lets it come back: after its own
    network decision, or after a T.38 call got no fax data back on a network
    that has been fixed since. A carrier that turns T.38 into audio itself, a
    phone system, and a person's choice of audio fax are left alone.
    """
    preset = sip_trunk.PRESETS.get(values.sip_trunk_preset)
    if preset is None or preset.phone_system or preset.audio_by_default or verdict not in ('open', 'blocked'):
        return None
    if verdict == 'blocked' and values.sip_t38_enabled:
        record = read(values)
        if record is None:
            return 'audio'
        if record['mode'] != 't38':
            return None  # the switch moved since Faxbot's record: a person's choice, until Apply records it
        if record.get('reason') == CHOSEN and record.get('network') == 'blocked':
            return None  # "Try T.38 again" on this network
        return 'audio'
    if verdict == 'open' and not values.sip_t38_enabled:
        record = read(values) or derive(values, records)
        if not record or record['mode'] != 'audio':
            return None
        if record.get('reason') == NETWORK:
            return 't38'
        if record.get('reason') == NO_DATA_BACK and (record.get('network') or previous) == 'blocked':
            return 't38'
    return None


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


def _network_now(values):
    """What the last network check allowed (open, blocked or unknown), or None before any check."""
    from .sip_network import read_check
    record = read_check(values)
    return record['t38'] if record else None


async def switch_to_audio(runtime, reason):
    """Save audio fax for new calls (by "system"), record why, write the trunk and let Asterisk load it.

    Never resends anything: the failed fax keeps its result. Returns what
    happened with the fax engine, or None when nothing changed.
    """
    return await switch(runtime, False, reason)


async def switch(runtime, enabled, reason, *, network=None):
    """Save T.38 on or off for new calls (by "system"), record why with what the network allowed, write
    the trunk and let Asterisk load it once no call is up. Returns what happened with the fax engine,
    or None when nothing changed."""
    from .config_runtime import run_lifecycle_step
    from .config_store import ConfigurationConflict

    def save():
        for _ in range(3):
            snapshot = runtime.manager.store.read()
            values = snapshot.desired.values
            if values.sip_t38_enabled == enabled or not sip_trunk.configured(values):
                return None
            try:
                runtime.manager.patch(snapshot, {'sip_t38_enabled': enabled}, actor='system')
            except ConfigurationConflict:
                continue
            values = runtime.manager.store.read().active.values
            write(values, 't38' if enabled else 'audio', reason, network=network or _network_now(values))
            sip_trunk.write_asterisk_configuration(values)
            return values
        return None

    try:
        values = await run_lifecycle_step(save)
        if values is None:
            return None
        try:
            from .audit import audit_event
            audit_event('sip_t38_chosen' if enabled else 'sip_audio_fax_chosen', backend='sip', reason=reason)
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
        logging.getLogger(__name__).warning('Faxbot could not switch T.38 for new calls.')
        return None
