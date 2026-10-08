"""Stored state of the experimental payload codec: recipient opt-ins, their history, sends and decode results.

Runtime code reflects the 0031 tables; it never imports the frozen metadata.
Opt-in history, send rows and decode results are written once and never
updated. A shared key is sealed with the installation key; only its
fingerprint is ever read back for display.
"""
from datetime import datetime
import uuid

import sqlalchemy as sa

STYLES = ('dense', 'picture')
LEVELS = ('low', 'medium', 'high')
NAMES = ('codec_numbers', 'codec_number_changes', 'codec_sends', 'codec_receipts')


class CodecStoreError(RuntimeError):
    """The codec tables are missing or unreadable (the database needs its 0031 upgrade)."""


class CodecInputError(ValueError):
    pass


class CodecConflict(RuntimeError):
    pass


def utcnow():
    return datetime.utcnow()


_REFLECTED = {}


def tables(engine):
    key = id(engine)
    found = _REFLECTED.get(key)
    if found is None or found[0] is not engine:
        try:
            metadata = sa.MetaData()
            with engine.connect() as connection:
                reflected = {name: sa.Table(name, metadata, autoload_with=connection) for name in NAMES}
        except sa.exc.SQLAlchemyError:
            raise CodecStoreError('The payload codec storage is not ready; upgrade the database.') from None
        found = (engine, reflected)
        _REFLECTED[key] = found
    return found[1]


DEFAULT = {'enabled': False, 'style': 'dense', 'fec': 'medium', 'key_fingerprint': None, 'version': 0,
           'has_key': False}


class KeySeal:
    """Seal shared keys with the installation configuration key, bound to the fax number."""

    def __init__(self, configuration):
        self.configuration = configuration

    def _context(self):
        with self.configuration.engine.connect() as connection:
            head = self.configuration._head(connection)
        if head is None:
            raise CodecStoreError('Installation configuration is not ready.')
        return self.configuration._cipher(), head['installation_id']

    def seal(self, secret, number):
        cipher, installation = self._context()
        return cipher.seal({'key': secret}, installation_id=installation, kind='codec_partner_key', record_id=number)

    def open(self, envelope, number):
        cipher, installation = self._context()
        payload = cipher.open(envelope, installation_id=installation, kind='codec_partner_key', record_id=number)
        secret = payload.get('key')
        return secret if isinstance(secret, str) else None


class CodecSettings:
    def __init__(self, engine, seal=None):
        self.engine = engine
        self.seal = seal

    def get(self, number):
        t = tables(self.engine)['codec_numbers']
        with self.engine.connect() as connection:
            row = connection.execute(sa.select(t).where(t.c.phone_number == number)).mappings().first()
        if row is None:
            return dict(DEFAULT, phone_number=number)
        return {'phone_number': number, 'enabled': bool(row['enabled']), 'style': row['style'], 'fec': row['fec'],
                'key_fingerprint': row['key_fingerprint'], 'has_key': row['secret_envelope'] is not None,
                'version': row['version']}

    def numbers(self):
        t = tables(self.engine)['codec_numbers']
        with self.engine.connect() as connection:
            rows = connection.execute(sa.select(t.c.phone_number).where(t.c.enabled == 1)
                                      .order_by(t.c.phone_number)).scalars().all()
        return list(rows)

    def secrets(self, number=None):
        """Shared keys to try when decoding: the sender's first, then every other partner's."""
        if self.seal is None:
            return []
        t = tables(self.engine)['codec_numbers']
        with self.engine.connect() as connection:
            rows = connection.execute(sa.select(t.c.phone_number, t.c.secret_envelope).where(
                t.c.secret_envelope.is_not(None))).all()
        ordered = sorted(rows, key=lambda row: row[0] != number)
        found = []
        for phone, envelope in ordered[:50]:
            try:
                secret = self.seal.open(envelope, phone)
            except Exception:
                continue
            if secret:
                found.append(secret)
        return found

    def secret(self, number):
        if self.seal is None:
            return None
        t = tables(self.engine)['codec_numbers']
        with self.engine.connect() as connection:
            envelope = connection.execute(sa.select(t.c.secret_envelope).where(
                t.c.phone_number == number)).scalar_one_or_none()
        return None if envelope is None else self.seal.open(envelope, number)

    def history(self, number):
        t = tables(self.engine)['codec_number_changes']
        with self.engine.connect() as connection:
            rows = connection.execute(sa.select(t).where(t.c.phone_number == number)
                                      .order_by(t.c.created_at.desc(), t.c.id.desc())).mappings().all()
        return [dict(row) for row in rows]

    def save(self, number, *, enabled, recipient_agreed, actor, actor_name=None, style=None, fec=None,
             secret=None, clear_key=False, expected_version=None, now=None):
        """Turn the codec on or off for a number, or change it; returns (setting, action or None).

        Turning it on needs the person to record that the recipient agreed.
        """
        from .container import key_fingerprint
        if style is not None and style not in STYLES:
            raise CodecInputError('Choose dense pages or a picture.')
        if fec is not None and fec not in LEVELS:
            raise CodecInputError('Choose low, medium or high error correction.')
        if secret is not None and (not isinstance(secret, str) or len(secret.strip()) < 8 or len(secret) > 200):
            raise CodecInputError('A shared key has 8 to 200 characters.')
        now = now or utcnow()
        t = tables(self.engine)
        numbers, changes = t['codec_numbers'], t['codec_number_changes']
        with self.engine.begin() as connection:
            row = connection.execute(sa.select(numbers).where(numbers.c.phone_number == number)).mappings().first()
            current = dict(row) if row is not None else None
            version = current['version'] if current else 0
            if expected_version is not None and expected_version != version:
                raise CodecConflict('Someone changed this setting since you opened it; reload and try again.')
            was_on = bool(current and current['enabled'])
            if enabled and not was_on and not recipient_agreed:
                raise CodecInputError('Record that the recipient agreed before turning encoded pages on.')
            values = {
                'enabled': 1 if enabled else 0,
                'style': style or (current['style'] if current else 'dense'),
                'fec': fec or (current['fec'] if current else 'medium'),
            }
            envelope = current['secret_envelope'] if current else None
            fingerprint = current['key_fingerprint'] if current else None
            if clear_key:
                envelope = fingerprint = None
            if secret is not None:
                if self.seal is None:
                    raise CodecStoreError('Shared keys cannot be stored on this installation.')
                envelope, fingerprint = self.seal.seal(secret.strip(), number), key_fingerprint(secret)
            changed = (current is None and enabled) or (current is not None and (
                values['enabled'] != current['enabled'] or values['style'] != current['style']
                or values['fec'] != current['fec'] or fingerprint != current['key_fingerprint']))
            if not changed:
                return self.get(number), None
            if current is None:
                connection.execute(numbers.insert().values(
                    id=uuid.uuid4().hex, phone_number=number, secret_envelope=envelope, key_fingerprint=fingerprint,
                    version=1, created_at=now, updated_at=now, **values))
            else:
                connection.execute(numbers.update().where(numbers.c.id == current['id']).values(
                    secret_envelope=envelope, key_fingerprint=fingerprint, version=version + 1, updated_at=now,
                    **values))
            action = 'on' if enabled and not was_on else 'off' if not enabled else 'changed'
            connection.execute(changes.insert().values(
                id=uuid.uuid4().hex, phone_number=number, action=action, actor=actor[:100],
                actor_name=(actor_name or None) and actor_name[:200], recipient_agreed=1 if recipient_agreed else 0,
                style=values['style'], fec=values['fec'], key_fingerprint=fingerprint, created_at=now))
        return self.get(number), action


def record_send(connection, engine, job_id, row, now):
    """Insert the fax's one send row (``send.record_attempt``: from its first attempt with encoded pages)."""
    t = tables(engine)['codec_sends']
    connection.execute(t.insert().values(id=job_id, created_at=now, **row))


def send_for(engine, job_id):
    t = tables(engine)['codec_sends']
    with engine.connect() as connection:
        row = connection.execute(sa.select(t).where(t.c.id == job_id)).mappings().first()
    return dict(row) if row is not None else None


def sends_for(engine, job_ids):
    if not job_ids:
        return {}
    t = tables(engine)['codec_sends']
    with engine.connect() as connection:
        rows = connection.execute(sa.select(t).where(t.c.id.in_(list(job_ids)))).mappings().all()
    return {row['id']: dict(row) for row in rows}


def receipt_for(engine, inbound_fax_id):
    t = tables(engine)['codec_receipts']
    with engine.connect() as connection:
        row = connection.execute(sa.select(t).where(t.c.inbound_fax_id == inbound_fax_id)).mappings().first()
    return dict(row) if row is not None else None


def record_receipt(engine, inbound_fax_id, values, now=None):
    """Write the one decode result for a received fax; an existing result is kept as it is."""
    t = tables(engine)['codec_receipts']
    now = now or utcnow()
    with engine.begin() as connection:
        existing = connection.execute(sa.select(t).where(t.c.inbound_fax_id == inbound_fax_id)).mappings().first()
        if existing is not None:
            return dict(existing)
        row = {'id': uuid.uuid4().hex, 'inbound_fax_id': inbound_fax_id, 'created_at': now, **values}
        try:
            connection.execute(t.insert().values(**row))
        except sa.exc.IntegrityError:
            pass
    return receipt_for(engine, inbound_fax_id)
