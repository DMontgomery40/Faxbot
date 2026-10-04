"""How a fax received by Asterisk reaches Faxbot, and how one that did not is brought in.

Asterisk stores each received image as ``<fax data>/inbound/<uniqueid>.tiff``
and hands it over to ``POST /_internal/asterisk/inbound`` with a shared secret.
That secret is plumbing between two containers of one installation, so Faxbot
creates it when none is set (an operator or ``.env`` value always wins) and
Apply to Asterisk writes it where the Asterisk container reads it.

A hand-over can still fail: the API was down, the secret was missing or
refused, or Faxbot stopped between the call and the hand-over. The image then
stays in the folder with no import record. ``recover`` finds such images and
imports them through the same SIP acquisition path, keyed on the call's
uniqueid, so a late hand-over and a recovery are one fax. A recovered fax is
marked as recovered and its source time is the image file's modification time,
recorded as such.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
import os
from pathlib import Path
import re
import secrets

import sqlalchemy as sa

from ..sip_calls import HANDOVER_REASONS, handover_sentence  # noqa: F401 (the hand-over wording lives with call records)
from .acquisition import AcquisitionError, account_identity, convert_tiff, discard

# An image is left alone this long after its last write unless its call is known
# to have ended: a page of a slow fax can take over a minute, and importing a
# fax that is still arriving would keep a partial document.
QUIET_PERIOD = timedelta(minutes=10)
# How often the background scan looks for images that were never handed over.
SCAN_SECONDS = 60
_IMAGE = re.compile(r'([0-9]{1,20}\.[0-9]{1,10})\.tiff')


def ensure_inbound_secret(manager) -> str:
    """The inbound secret in the saved settings, created once (actor "system") when none is set."""
    from ..config_store import ConfigurationConflict
    for _ in range(3):
        snapshot = manager.store.read()
        current = snapshot.desired.values.asterisk_inbound_secret
        if current:
            return current
        try:
            manager.patch(snapshot, {'asterisk_inbound_secret': secrets.token_urlsafe(32)}, actor='system')
        except ConfigurationConflict:
            continue
        try:
            from ..audit import audit_event
            audit_event('inbound_secret_created', backend='sip')
        except Exception:
            pass
    secret = manager.store.read().desired.values.asterisk_inbound_secret
    if not secret:
        raise AcquisitionError('Faxbot could not save an inbound secret for the fax engine.')
    return secret


def receives_over_trunk(values) -> bool:
    """Whether Asterisk may hand received faxes to Faxbot under these settings."""
    from .. import sip_trunk
    return bool(values.effective_inbound == 'sip' or sip_trunk.configured(values))


def prepare_handover(manager, values) -> bool:
    """At startup: create the secret if needed and write it for Asterisk; True when written."""
    from .. import sip_trunk
    if not receives_over_trunk(values):
        return False
    secret = ensure_inbound_secret(manager)
    sip_trunk.write_inbound_secret(values, secret)
    return True


@dataclass(frozen=True)
class Recovered:
    found: int
    imported: tuple
    waiting: int


def _orphans(directory: Path, now: datetime, ended_calls: set[str]):
    """Received images whose last write is old enough, or whose call is known to have ended."""
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return
    for entry in entries:
        match = _IMAGE.fullmatch(entry.name)
        if match is None:
            continue
        try:
            if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                continue
            modified = datetime.fromtimestamp(entry.stat(follow_symlinks=False).st_mtime, timezone.utc).replace(tzinfo=None)
        except OSError:
            continue
        uniqueid = match.group(1)
        if uniqueid in ended_calls or now - modified >= QUIET_PERIOD:
            yield uniqueid, Path(entry.path), modified


def recover(store, engine, values, *, now=None) -> Recovered:
    """Import received images that were never handed over; safe to run repeatedly.

    Only runs while receiving over the SIP trunk is on. Each image is keyed on
    its call's uniqueid; an image that already has an import is left alone.
    """
    from .. import sip_calls
    from ..routing.numbers import DEFAULT_COUNTRY
    now = now or datetime.utcnow()
    if not values.inbound_enabled or not receives_over_trunk(values):
        return Recovered(0, (), 0)
    directory = Path(values.fax_data_dir) / 'inbound'
    records = sip_calls.SipCallRecords(engine)
    ended = records.unclaimed_inbound_calls()
    imports = store.imports
    imported, waiting, found = [], 0, 0
    for uniqueid, path, modified in _orphans(directory, now, ended):
        with store.engine.connect() as connection:
            known = connection.execute(sa.select(imports.c.inbound_fax_id).where(
                imports.c.source == 'sip', imports.c.operation_id == uniqueid).limit(1)).first()
        if known is not None:
            # Handed over after all (a reply that never arrived): the call now points at its fax.
            if uniqueid in ended:
                records.link_inbound(uniqueid, known[0])
            continue
        found += 1
        call = records.inbound_call(uniqueid) or {}
        report = {'recovered': True, 'source_time': 'image file modified time', 'uniqueid': uniqueid}
        to_number = call.get('did')
        dids = list(values.sip_trunk_did_list)
        if not to_number and len(dids) == 1:
            # The trunk has one fax number, so the fax arrived on it.
            to_number = dids[0]
            report['to_number'] = 'inferred from the only fax number on the trunk'
        begun = store.begin(
            source='sip', account=account_identity('sip', values.sip_trunk_username), operation_id=uniqueid,
            backend='sip', inbound_backend=values.effective_inbound or 'sip', to_number=to_number,
            from_number=call.get('caller'), reported_pages=call.get('pages'), report=report,
            source_received_at=modified, tiff_path=str(path), schedule=False,
            country=values.fax_default_country or DEFAULT_COUNTRY)
        if begun.state == 'pending':
            try:
                artifact = convert_tiff(str(path), begun.inbound_fax_id)
                completion = store.complete(begun.import_id, artifact_path=artifact.path, digest=artifact.digest,
                                            size=artifact.size, pages=artifact.pages, media_type=artifact.media_type,
                                            source_received_at=modified)
                discard(artifact, completion)
            except AcquisitionError as error:
                store.fail(begun.import_id, str(error))
                waiting += 1
            except Exception:
                logging.getLogger(__name__).warning('A recovered fax image could not be converted; Faxbot will try again.')
                store.fail(begun.import_id, 'Faxbot could not convert the received fax image.')
                waiting += 1
        records.link_inbound(uniqueid, begun.inbound_fax_id)
        imported.append(begun.inbound_fax_id)
        try:
            from ..audit import audit_event
            audit_event('inbound_recovered', job_id=begun.inbound_fax_id, backend='sip')
        except Exception:
            pass
    return Recovered(found, tuple(imported), waiting)
