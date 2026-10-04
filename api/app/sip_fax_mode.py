"""T.38 or audio fax for new trunk calls, and why.

Faxbot chooses audio fax by itself in two cases, and records why so every
screen can say it in one sentence:

- ``no_data_back``: a call switched to T.38 and no fax data came back. The
  failed fax is never sent again; only new calls use audio fax.
- ``network``: a new Telnyx trunk on a network that changes port numbers,
  where Telnyx's T.38 data was seen not to come back.

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
from pathlib import Path

from . import sip_trunk

NO_DATA_BACK = 'no_data_back'
NETWORK = 'network'
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


def write(values, mode, reason, at=None):
    path = record_path(values)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    record = {'mode': mode, 'reason': reason,
              'at': (at or datetime.now(timezone.utc)).replace(tzinfo=None).isoformat(timespec='seconds') + 'Z'}
    sip_trunk._write_private(path, json.dumps(record) + '\n')
    return record


def off_sentence(reason, day=''):
    """One sentence for why new calls use audio fax; ``day`` is the date in the reader's own words."""
    if reason == NO_DATA_BACK:
        return (f'Off: {"on " + day + " " if day else ""}a T.38 fax got no fax data back on this network, '
                'so Faxbot uses audio fax.')
    if reason == NETWORK:
        return ('Off: your network changes port numbers, and Telnyx\'s T.38 fax data does not come back through '
                'such networks, so Faxbot uses audio fax.')
    return None


def reason_for(values):
    """Why new calls use audio fax ({reason, at}), or None when T.38 is on or a person turned it off."""
    if values.sip_t38_enabled:
        return None
    record = read(values)
    if record and record['mode'] == 'audio' and record.get('reason') in (NO_DATA_BACK, NETWORK):
        return {'reason': record['reason'], 'at': record.get('at')}
    return None


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


def _on_fax_event(event):
    """AMI listener for FaxResult and FaxInboundCall; runs in the event loop and never raises."""
    try:
        from .sip_calls import verdict
        if _runtime is None or verdict(event) != 'no_t38_data_back':
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
        return await _load_into_engine(values)
    except Exception:
        logging.getLogger(__name__).warning('Faxbot could not switch new calls to audio fax.')
        return None
