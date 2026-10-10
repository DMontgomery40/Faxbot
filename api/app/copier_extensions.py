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

    @property
    def endpoint(self):
        return f'copier-{self.extension}'


def digest(username, password):
    """pjsip's ``md5_cred``: MD5 of user:realm:password, so the password itself is never stored."""
    return hashlib.md5(f'{username}:{REALM}:{password}'.encode('utf-8')).hexdigest()


def _table(bind):
    return sa.Table('copier_extensions', sa.MetaData(), autoload_with=bind)


def copiers(engine, *, active_only=True):
    """Every copier extension, newest row per copier."""
    table = _table(engine)
    with engine.connect() as connection:
        rows = connection.execute(sa.select(table).order_by(table.c.created_at, table.c.id)).mappings().all()
    newest = {}
    for row in rows:
        newest[row['copier_id']] = row
    found = [Copier(row['copier_id'], row['name'], row['extension'], row['username'], row['md5_cred'],
                    row['mailbox_id'], tuple(json.loads(row['numbers'] or '[]')), row['receive'], row['state'])
             for row in newest.values()]
    return [item for item in found if item.state == 'active'] if active_only else found


def add(engine, *, name, extension, mailbox_id=None, numbers=(), receive='copier', actor=None, now=None):
    """A new copier extension and its password, shown once: (Copier, password)."""
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
    with write_transaction(engine) as connection:
        connection.execute(_table(connection).insert().values(
            id=uuid.uuid4().hex, copier_id=copier_id, name=name, extension=extension, username=username,
            md5_cred=digest(username, password), mailbox_id=mailbox_id, numbers=json.dumps(sorted(set(numbers))),
            receive=receive, state='active', recorded_by_name=(actor or {}).get('name'), created_at=now or utcnow()))
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
            recorded_by_name=(actor or {}).get('name'), created_at=now or utcnow()))
    return found


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
                  f'[{name}]', 'type=aor', 'max_contacts=1', 'remove_existing=yes', 'qualify_frequency=60',
                  'contact_deny=0.0.0.0/0.0.0.0', *[f'contact_permit={network}' for network in PRIVATE], '',
                  f'[{name}]', 'type=endpoint', 'transport=transport-udp', f'aors={name}', f'auth={name}-auth',
                  f'context={CONTEXT}', 'disallow=all', 'allow=ulaw,alaw', 't38_udptl=yes',
                  't38_udptl_ec=redundancy', 't38_udptl_maxdatagram=400', 'direct_media=no', 'rtp_symmetric=yes',
                  'force_rport=yes', 'rewrite_contact=yes', deny, *permit, f'set_var=FAXBOT_COPIER={item.id}', '']
    return '\n'.join(lines)
