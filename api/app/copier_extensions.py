"""Copiers as extensions of Faxbot (research N22, store and forward): a copier with an IP fax option registers to
Faxbot's Asterisk as its own SIP extension and faxes through Faxbot.

- **Its credentials.** Faxbot makes the extension's user name and a random password, shows the password once, and
  keeps only pjsip's MD5 digest of it (``md5_cred``, realm ``faxbot``). The endpoint takes registrations and calls
  only from private addresses (RFC 1918), so a copier extension is never reachable from the internet.
- **Sending.** A copier's call ends at Faxbot: the built-in engine receives the fax (``faxbot-copier`` in
  ``extensions-copier.conf``), the number the copier dialled is taken from the Request-URI, and the fax is queued as
  an outbound Faxbot fax from the copier's mailbox through the same acceptance as ``POST /fax``: sending rules,
  the dialing guard, holds and Sent. A copier call is never bridged to the trunk (no toll fraud from a device on
  the network, and no page lost to a call that fails half way). The copier's call completes when Faxbot has the
  pages; if the onward send fails, the fax stays in Sent with its reason.
- **Receiving.** A received fax for a number you assigned to the copier lands in Faxbot's intake first, as every
  received fax does, and is then faxed on to the copier's extension; when the copier doesn't answer, it stays in
  intake (or, with ``receive`` ``intake``, it is never sent on).

Ricoh documents IP-Fax over SIP with T.38 and SIP-server registration (IM 550/600 manual, read 2026-10-08 and
2026-10-10, ``copiers.py``). Not yet run against a real copier.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
import secrets
import uuid

import sqlalchemy as sa

REALM = 'faxbot'
PRIVATE = ('10.0.0.0/255.0.0.0', '172.16.0.0/255.240.0.0', '192.168.0.0/255.255.0.0')
RECEIVE = ('copier', 'intake')
EXTENSION = re.compile(r'[1-9][0-9]{2,5}')
CONTEXT = 'faxbot-copier'


class CopierError(ValueError):
    """A copier change Faxbot refuses, in one sentence."""


@dataclass(frozen=True)
class Copier:
    id: str
    name: str
    extension: str
    username: str
    md5_cred: str
    mailbox_id: str | None
    numbers: tuple[str, ...]
    receive: str
    state: str
    secret_envelope: str | None = None
    key_binding_id: str | None = None
    key_principal_id: str | None = None

    @property
    def endpoint(self):
        return f'copier-{self.extension}'


def digest(username, password):
    """pjsip's ``md5_cred``: MD5 of user:realm:password, so the password itself is never stored."""
    return hashlib.md5(f'{username}:{REALM}:{password}'.encode('utf-8')).hexdigest()


def _table(bind):
    # Reflected once per engine (routing.database.reflect): the Capabilities page reads this on every visit.
    from .routing.database import reflect
    return reflect(bind, ('copier_extensions',))['copier_extensions']


def copiers(engine, *, active_only=True):
    """Every copier extension, newest row per copier."""
    table = _table(engine)
    with engine.connect() as connection:
        rows = connection.execute(sa.select(table).order_by(table.c.created_at, table.c.id)).mappings().all()
    newest = {}
    for row in rows:
        newest[row['copier_id']] = row
    found = [Copier(row['copier_id'], row['name'], row['extension'], row['username'], row['md5_cred'],
                    row['mailbox_id'], tuple(json.loads(row['numbers'] or '[]')), row['receive'], row['state'],
                    row['secret_envelope'], row['key_binding_id'], row['key_principal_id'])
             for row in newest.values()]
    return [item for item in found if item.state == 'active'] if active_only else found


def find(engine, copier_id):
    """The active copier with this ID, or None."""
    return next((item for item in copiers(engine) if item.id == copier_id), None)


class CopierSecrets:
    """Seal a copier's own sending key with the installation configuration key, bound to the copier."""

    KIND = 'copier_extension'

    def __init__(self, configuration):
        self.configuration = configuration

    def _context(self):
        with self.configuration.engine.connect() as connection:
            head = self.configuration._head(connection)
        if head is None:
            raise CopierError('Faxbot is not set up yet.')
        return self.configuration._cipher(), head['installation_id']

    def seal(self, token, copier_id):
        cipher, installation = self._context()
        return cipher.seal({'key_token': token}, installation_id=installation, kind=self.KIND, record_id=copier_id)

    def token(self, copier):
        cipher, installation = self._context()
        return dict(cipher.open(copier.secret_envelope, installation_id=installation, kind=self.KIND,
                                record_id=copier.id))['key_token']


def add(engine, *, name, extension, mailbox_id=None, numbers=(), receive='copier', actor=None, now=None, key=None,
        seal=None):
    """A new copier extension and its password, shown once: (Copier, password). ``key`` is (token, binding id,
    principal id) of the copier's own sending key and ``seal`` a ``CopierSecrets``; the token is kept sealed."""
    from .routing.database import utcnow, write_transaction
    name = ' '.join(str(name or '').split())
    if not name or len(name) > 100:
        raise CopierError('Name the copier, such as Front desk Ricoh.')
    if not EXTENSION.fullmatch(str(extension or '')):
        raise CopierError('An extension is 3 to 6 digits and does not start with 0, such as 601.')
    if receive not in RECEIVE:
        raise CopierError('Choose copier (faxes for its numbers go on to it) or intake (they stay in Faxbot).')
    if any(item.extension == extension for item in copiers(engine)):
        raise CopierError(f'Extension {extension} is already a copier.')
    username = f'copier-{extension}'
    password = secrets.token_urlsafe(18)
    copier_id = uuid.uuid4().hex
    envelope = seal.seal(key[0], copier_id) if key is not None and seal is not None else None
    with write_transaction(engine) as connection:
        connection.execute(_table(connection).insert().values(
            id=uuid.uuid4().hex, copier_id=copier_id, name=name, extension=extension, username=username,
            md5_cred=digest(username, password), mailbox_id=mailbox_id, numbers=json.dumps(sorted(set(numbers))),
            receive=receive, state='active', secret_envelope=envelope,
            key_binding_id=key[1] if key else None, key_principal_id=key[2] if key else None,
            recorded_by_name=(actor or {}).get('name'), created_at=now or utcnow()))
    return next(item for item in copiers(engine) if item.id == copier_id), password


def remove(engine, copier_id, *, actor=None, now=None):
    from .routing.database import utcnow, write_transaction
    found = next((item for item in copiers(engine) if item.id == copier_id), None)
    if found is None:
        raise CopierError('No copier extension has that ID.')
    with write_transaction(engine) as connection:
        connection.execute(_table(connection).insert().values(
            id=uuid.uuid4().hex, copier_id=found.id, name=found.name, extension=found.extension,
            username=found.username, md5_cred=found.md5_cred, mailbox_id=found.mailbox_id,
            numbers=json.dumps(list(found.numbers)), receive=found.receive, state='removed',
            key_binding_id=found.key_binding_id, key_principal_id=found.key_principal_id,
            recorded_by_name=(actor or {}).get('name'), created_at=now or utcnow()))
    return found


# A copier's fax, handed over by Asterisk (extensions-copier.conf) ------------------------------------------------

class HandoverRefused(ValueError):
    """The copier's fax could not be queued; it is filed as a received fax instead, so no page is lost."""


def job_id_for(copier_id, uniqueid):
    """The outbound fax's ID for one copier call, so a repeated hand-over never makes a second fax."""
    import hashlib
    return hashlib.sha256(f'copier:{copier_id}:{uniqueid}'.encode('utf-8')).hexdigest()[:32]


def queue(app, payload, tiff_path):
    """Queue a copier's received fax as an outbound fax from the copier's mailbox to the number it dialled, through
    the same acceptance as POST /fax (sending rules, dialing guard, holds and Sent). Returns the fax ID; raises
    ``HandoverRefused`` with one sentence when it can't (the caller then files the pages as a received fax)."""
    import os
    import tempfile
    from .access.types import AccessError
    from .conversion import DocumentConversionError, tiff_to_pdf
    from .routing.background import installation_engine
    from .routing.numbers import InvalidNumber, normalize_number
    from .routing.submit import accept_generated_fax
    if str(payload.get('faxstatus') or '').upper() != 'SUCCESS':
        raise HandoverRefused("The copier's call ended before every page arrived, so nothing was sent on; the pages "
                              'that arrived are here.')
    engine, runtime = installation_engine(app)
    if engine is None or runtime is None:
        raise HandoverRefused('Faxbot was not ready to take the copier\'s fax.')
    copier = find(engine, str(payload.get('copier') or ''))
    if copier is None:
        raise HandoverRefused('No copier extension has that ID any more.')
    revision = runtime.manager.store.read()
    try:
        destination = normalize_number(str(payload.get('dialed') or ''),
                                       country=revision.active.values.fax_default_country)
    except (InvalidNumber, ValueError):
        raise HandoverRefused(f"{copier.name} dialled a number Faxbot can't send to.") from None
    access = getattr(app.state, 'access_runtime', None)
    if access is None or copier.secret_envelope is None:
        raise HandoverRefused(f'{copier.name} has no sending key; add the copier again.')
    try:
        actor = access.authentication.header_key(CopierSecrets(runtime.manager.store).token(copier))
    except AccessError:
        raise HandoverRefused(f"{copier.name}'s sending key was revoked; add the copier again.") from None
    folder = tempfile.mkdtemp(prefix='copier-')
    pdf = os.path.join(folder, 'copier.pdf')
    try:
        pages, _ = tiff_to_pdf(tiff_path, pdf)
        with open(pdf, 'rb') as handle:
            document = handle.read()
    except (DocumentConversionError, OSError):
        raise HandoverRefused(f"The pages from {copier.name} couldn't be read.") from None
    finally:
        for name in os.listdir(folder):
            os.unlink(os.path.join(folder, name))
        os.rmdir(folder)
    uniqueid = str(payload.get('uniqueid') or tiff_path)
    return accept_generated_fax(runtime, access, actor, revision.active, to_number=destination, document=document,
                                file_name=f'{copier.username}.pdf', pages=pages,
                                job_id=job_id_for(copier.id, uniqueid), mailbox=copier.mailbox_id)


def render(items) -> str:
    """The pjsip.conf sections for copier extensions ('' for none): per copier an MD5 auth, an AOR it registers
    to (one contact, the newest replaces the old), and an endpoint that only private addresses may use."""
    items = [item for item in items or () if item.state == 'active']
    if not items:
        return ''
    deny = 'deny=0.0.0.0/0.0.0.0'
    permit = [f'permit={network}' for network in PRIVATE]
    lines = ['; Copiers that fax through Faxbot as its own extensions (copier_extensions.py).',
             '; Faxbot writes these from Delivery setup; edits here are replaced.']
    for item in sorted(items, key=lambda copier: copier.extension):
        name = item.endpoint
        lines += [f'[{name}-auth]', 'type=auth', 'auth_type=md5', f'username={item.username}', f'realm={REALM}',
                  f'md5_cred={item.md5_cred}', '',
                  f'[{name}]', 'type=aor', 'max_contacts=1', 'remove_existing=yes', 'qualify_frequency=60', '',
                  f'[{name}]', 'type=endpoint', 'transport=transport-udp', f'aors={name}', f'auth={name}-auth',
                  f'context={CONTEXT}', 'disallow=all', 'allow=ulaw,alaw', 't38_udptl=yes',
                  't38_udptl_ec=redundancy', 't38_udptl_maxdatagram=400', 'direct_media=no', 'rtp_symmetric=yes',
                  'force_rport=yes', 'rewrite_contact=yes', deny, *permit,
                  'contact_deny=0.0.0.0/0.0.0.0', *[f'contact_permit={network}' for network in PRIVATE],
                  f'set_var=FAXBOT_COPIER={item.id}', '']
    return '\n'.join(lines)


# Received faxes sent on to a copier's extension -----------------------------------------------------------------

MAX_TRIES = 2
LOOKBACK = 24 * 3600  # seconds: received faxes older than a day are not sent on to a copier any more
FORWARD_STATES = ('calling', 'sent', 'no_answer', 'failed')


def _forwards(bind):
    return sa.Table('copier_forwards', sa.MetaData(), autoload_with=bind)


def record_forward(engine, inbound_fax_id, copier_id, state, *, pages=None, reason=None, forward_id=None, now=None):
    """One try at sending a received fax on to a copier (append-only); returns its ID."""
    from .routing.database import utcnow, write_transaction
    forward_id = forward_id or uuid.uuid4().hex
    with write_transaction(engine) as connection:
        connection.execute(_forwards(connection).insert().values(
            id=uuid.uuid4().hex if state != 'calling' else forward_id, inbound_fax_id=inbound_fax_id,
            copier_id=copier_id, state=state, pages=pages, reason=(reason or None) and reason[:300],
            created_at=now or utcnow()))
    return forward_id


def forwards_for(engine, inbound_fax_id):
    table = _forwards(engine)
    with engine.connect() as connection:
        return [dict(row) for row in connection.execute(sa.select(table).where(
            table.c.inbound_fax_id == inbound_fax_id).order_by(table.c.created_at, table.c.id)).mappings()]


def due(engine, now=None):
    """[(inbound fax row, Copier)] received for a number you gave a copier, not sent on yet, tried fewer than
    ``MAX_TRIES`` times and not being called now."""
    from datetime import timedelta
    from .routing.database import utcnow
    now = now or utcnow()
    receiving = [item for item in copiers(engine) if item.receive == 'copier' and item.numbers]
    if not receiving:
        return []
    inbound = sa.table('inbound_faxes', sa.column('id'), sa.column('to_number'), sa.column('status'),
                       sa.column('tiff_path'), sa.column('pdf_path'), sa.column('pages'),
                       sa.column('received_at', sa.DateTime()))
    forwards = _forwards(engine)
    found = []
    with engine.connect() as connection:
        for copier in receiving:
            rows = connection.execute(sa.select(inbound).where(
                inbound.c.to_number.in_(list(copier.numbers)), inbound.c.received_at >= now - timedelta(
                    seconds=LOOKBACK)).order_by(inbound.c.received_at)).mappings().all()
            for row in rows:
                if not (row['tiff_path'] or row['pdf_path']):
                    continue
                states = connection.execute(sa.select(forwards.c.state).where(
                    forwards.c.inbound_fax_id == row['id'])).scalars().all()
                tries = sum(1 for state in states if state == 'calling')
                finished = sum(1 for state in states if state != 'calling')
                if 'sent' in states or tries >= MAX_TRIES or tries > finished:
                    continue
                found.append((dict(row), copier))
    return found


def _tiff_for(values, row, forward_id):
    """The received fax as a TIFF the fax engine can send: its own image, or one made from its PDF."""
    import os
    if row['tiff_path'] and os.path.isfile(row['tiff_path']):
        return row['tiff_path']
    from .conversion import FAX_IMAGE_MODE, pdf_to_tiff
    target = os.path.join(values.fax_data_dir, f'copier-{forward_id}.tiff')
    pdf_to_tiff(row['pdf_path'], target)
    os.chmod(target, FAX_IMAGE_MODE)  # the fax engine reads it, as it reads every fax image Faxbot writes
    return target


class Forwarder:
    """Sends received faxes on to their copier's extension through the fax engine (AMI Originate into
    ``faxbot-copier-send``); a copier that doesn't answer leaves the fax in Received, with why."""

    def __init__(self, engine, ami, values):
        self.engine, self.ami, self.values = engine, ami, values
        self.calling = {}  # forward ID: (inbound fax ID, copier)

    async def step(self):
        from .config_runtime import run_lifecycle_step
        for row, copier in await run_lifecycle_step(lambda: due(self.engine)):
            forward_id = uuid.uuid4().hex
            tiff = await run_lifecycle_step(lambda: _tiff_for(self.values(), row, forward_id))
            await run_lifecycle_step(lambda: record_forward(self.engine, row['id'], copier.id, 'calling',
                                                            forward_id=forward_id))
            self.calling[forward_id] = (row['id'], copier)
            try:
                await self.ami.originate_copier(forward_id, copier.endpoint, tiff)
            except (ConnectionError, OSError, ValueError):
                self.calling.pop(forward_id, None)
                await run_lifecycle_step(lambda: record_forward(
                    self.engine, row['id'], copier.id, 'failed',
                    reason="The fax engine couldn't be reached, so this fax stays in Received; Faxbot tries once more."))
        return False

    def on_originate(self, msg):
        """A call to the copier that never answered (AMI OriginateResponse Failure for our ActionID)."""
        fields = {key.lower(): value for key, value in msg.items()}
        action = fields.get('actionid', '')
        if not action.startswith('copier-') or fields.get('response', '').lower() != 'failure':
            return
        found = self.calling.pop(action[len('copier-'):], None)
        if found is not None:
            inbound_id, copier = found
            record_forward(self.engine, inbound_id, copier.id, 'no_answer',
                           reason=f"{copier.name} didn't answer, so this fax stays in Received.")

    def on_sent(self, msg):
        """The fax engine's word on a fax it sent to a copier (the FaxCopierSent user event)."""
        fields = {key.lower(): value for key, value in msg.items()}
        found = self.calling.pop(fields.get('job', ''), None)
        if found is None:
            return
        inbound_id, copier = found
        pages = int(fields['pages']) if fields.get('pages', '').isdigit() else None
        if fields.get('status', '').upper() == 'SUCCESS':
            record_forward(self.engine, inbound_id, copier.id, 'sent', pages=pages)
        else:
            record_forward(self.engine, inbound_id, copier.id, 'failed', pages=pages,
                           reason=f"{copier.name} answered but the fax didn't go through, so it stays in Received.")


def attach(ami, runtime, tasks):
    """Start sending received faxes on to copiers with the fax engine's connection (main.py, at startup)."""
    import asyncio
    from .routing.background import repeat_async
    forwarder = Forwarder(runtime.manager.store.engine, ami, lambda: runtime.manager.store.read().active.values)
    ami.on_originate_response(forwarder.on_originate)
    ami.on_copier_sent(forwarder.on_sent)
    tasks.append(asyncio.create_task(repeat_async(
        forwarder.step, interval=30.0, initial_delay=20.0,
        warning='Received faxes could not be sent on to copiers yet; Faxbot tries again.'),
        name='faxbot-copier-forwarder'))
    return forwarder


def fax_view(engine, inbound_fax_id):
    """What Received details say about a fax sent on to a copier: one sentence, or None."""
    rows = forwards_for(engine, inbound_fax_id)
    if not rows:
        return None
    last = rows[-1]
    copier = next((item for item in copiers(engine, active_only=False) if item.id == last['copier_id']), None)
    name = copier.name if copier is not None else 'The copier'
    if last['state'] == 'sent':
        count = last['pages']
        return (f"Sent on to {name} at extension {copier.extension if copier else '?'}"
                f"{f', {count} page' + ('s' if count != 1 else '') if count else ''}.")
    if last['state'] == 'calling':
        from datetime import timedelta
        from .routing.database import utcnow
        if last['created_at'] < utcnow() - timedelta(minutes=10):
            return (f"Faxbot doesn't know whether this fax reached {name}: the call's result never came back, so it "
                    'stays in Received and is not sent again.')
        return f'Sending this fax on to {name} now.'
    return last['reason'] or f"This fax stays in Received; it didn't reach {name}."
