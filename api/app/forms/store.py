"""The form registry and the ledger of forms sent and received.

Form versions are immutable: this store only ever inserts them. Importing
content whose address is already registered returns that version unchanged.
"""
import base64
from datetime import timedelta
import json
import zlib
from uuid import uuid4

import sqlalchemy as sa

from ..routing.database import read_connection, reflect, utcnow, write_transaction
from . import model
from .raster import Bitmap


# A person's "send the pages as a fax" holds the form this long before Faxbot checks whether it was queued.
CLAIM_WINDOW = timedelta(minutes=5)
CLAIM = 'claim-'
# A sent fax's state, in the words Sent uses for it.
FAX_STATE_TEXT = {'ready': 'waiting to be sent', 'preparing': 'waiting to be sent', 'submitting': 'being sent',
                  'in_progress': 'being sent', 'success': 'sent', 'failed': 'not delivered', 'held': 'held',
                  'reconciliation_required': 'waiting for its result to be confirmed'}


def claim_job(claim):
    """The fax id a person's claim reserved, or None for anything else."""
    return claim[len(CLAIM):] if isinstance(claim, str) and claim.startswith(CLAIM) else None


class FormConflict(RuntimeError):
    """A plain-sentence refusal of a registry or ledger change."""


def pack_backgrounds(backgrounds):
    return base64.b64encode(zlib.compress(b''.join(page.packed() for page in backgrounds), 9)).decode('ascii')


def unpack_backgrounds(document, encoded):
    """Page bitmaps from stored or received data, each checked against the content's page hash."""
    expected = sum((page['width'] + 7) // 8 * page['height'] for page in document['pages'])
    try:
        inflater = zlib.decompressobj()
        data = inflater.decompress(base64.b64decode(encoded, validate=True), expected + 1)
        if inflater.unconsumed_tail or not inflater.eof:
            raise ValueError
    except (ValueError, zlib.error):
        raise model.FormError('The form pages are damaged.') from None
    pages, at = [], 0
    for page in document['pages']:
        size = (page['width'] + 7) // 8 * page['height']
        chunk = data[at:at + size]
        at += size
        if len(chunk) != size or model.sha256(chunk) != page['background']:
            raise model.FormError('The form pages do not match the form.')
        pages.append(Bitmap.from_packed(page['width'], page['height'], chunk))
    if at != len(data):
        raise model.FormError('The form pages do not match the form.')
    return pages


class FormVersion:
    """One immutable form version with its parsed content."""

    def __init__(self, row, form):
        self.row, self.form = row, form
        self.id, self.address, self.number = row['id'], row['address'], row['number']
        self.title = row['title']
        self.content = json.loads(row['content'])
        self._backgrounds = None

    @property
    def backgrounds(self):
        if self._backgrounds is None:
            self._backgrounds = unpack_backgrounds(self.content, self.row['backgrounds'])
        return self._backgrounds

    def bundle(self):
        """What a partner fetches: the addressed content, its pages and its labels."""
        return {'faxbot_form_bundle': 1, 'address': self.address, 'title': self.title, 'version': self.number,
                'content': self.content, 'backgrounds': self.row['backgrounds']}


class FormStore:
    TABLES = ('forms', 'form_versions', 'form_deliveries', 'intake_items', 'direct_deliveries', 'inbound_imports',
              'fax_jobs', 'outbound_deliveries')

    def __init__(self, engine):
        self.engine = engine
        tables = reflect(engine, self.TABLES)
        self.forms = tables['forms']
        self.versions = tables['form_versions']
        self.deliveries = tables['form_deliveries']
        self.intake = tables['intake_items']
        self.direct = tables['direct_deliveries']
        self.imports = tables['inbound_imports']  # read only
        self.jobs = tables['fax_jobs']  # read only
        self.outbound = tables['outbound_deliveries']  # read only

    # Registry ------------------------------------------------------------------------------------
    def _version(self, connection, row):
        form = connection.execute(sa.select(self.forms).where(self.forms.c.id == row['form_id'])).mappings().one()
        return FormVersion(dict(row), dict(form))

    def add_version(self, imported, *, name=None, form_id=None, created_by=None, now=None):
        """Register an import as a new version (of ``form_id``, or of a new form called ``name``).

        Returns (version, created). Content already registered returns its version, created False.
        """
        now = now or utcnow()
        with write_transaction(self.engine) as connection:
            existing = connection.execute(sa.select(self.versions).where(
                self.versions.c.address == imported.address)).mappings().one_or_none()
            if existing is not None:
                return self._version(connection, existing), False
            if form_id is not None:
                form = connection.execute(sa.select(self.forms).where(self.forms.c.id == form_id)).mappings().one_or_none()
                if form is None:
                    raise FormConflict('This form does not exist.')
                if form['origin'] != 'local':
                    raise FormConflict("A partner's form gets new versions only from that partner.")
            else:
                name = model.text(name or '').strip()
                if not 0 < len(name) <= 200:
                    raise FormConflict('Give the form a name of up to 200 characters.')
                form_id = uuid4().hex
                connection.execute(self.forms.insert().values(id=form_id, name=name, origin='local', peer_id=None,
                                                              created_at=now))
                form = {'name': name}
            number = (connection.scalar(sa.select(sa.func.max(self.versions.c.number)).where(
                self.versions.c.form_id == form_id)) or 0) + 1
            identity = uuid4().hex
            connection.execute(self.versions.insert().values(
                id=identity, form_id=form_id, number=number, address=imported.address, title=form['name'],
                source=imported.source, content=model.canonical(imported.content).decode('ascii'),
                backgrounds=pack_backgrounds(imported.backgrounds),
                template=base64.b64encode(imported.template).decode('ascii') if imported.template else None,
                template_media_type=imported.media_type,
                template_sha256=model.sha256(imported.template) if imported.template else None,
                page_count=len(imported.content['pages']), field_count=len(imported.content['fields']),
                peer_id=None, created_by=created_by, created_at=now))
            row = connection.execute(sa.select(self.versions).where(self.versions.c.id == identity)).mappings().one()
            return self._version(connection, row), True

    def add_partner_version(self, bundle, *, peer, now=None):
        """Keep a form fetched from a partner, after its address and pages were checked."""
        now = now or utcnow()
        with write_transaction(self.engine) as connection:
            existing = connection.execute(sa.select(self.versions).where(
                self.versions.c.address == bundle['address'])).mappings().one_or_none()
            if existing is not None:
                return self._version(connection, existing)
            title = model.text(bundle['title']).strip()[:200] or 'Form'
            form = connection.execute(sa.select(self.forms).where(
                self.forms.c.origin == 'partner', self.forms.c.peer_id == peer['id'],
                self.forms.c.name == title)).mappings().first()
            if form is None:
                form_id = uuid4().hex
                connection.execute(self.forms.insert().values(id=form_id, name=title, origin='partner',
                                                              peer_id=peer['id'], created_at=now))
            else:
                form_id = form['id']
            taken = set(connection.execute(sa.select(self.versions.c.number).where(
                self.versions.c.form_id == form_id)).scalars())
            number = bundle['version'] if bundle['version'] not in taken else max(taken) + 1
            identity = uuid4().hex
            connection.execute(self.versions.insert().values(
                id=identity, form_id=form_id, number=number, address=bundle['address'], title=title,
                source='partner', content=model.canonical(bundle['content']).decode('ascii'),
                backgrounds=bundle['backgrounds'], template=None, template_media_type=None, template_sha256=None,
                page_count=len(bundle['content']['pages']), field_count=len(bundle['content']['fields']),
                peer_id=peer['id'], created_by=None, created_at=now))
            row = connection.execute(sa.select(self.versions).where(self.versions.c.id == identity)).mappings().one()
            return self._version(connection, row)

    def list_forms(self):
        with read_connection(self.engine) as connection:
            forms = [dict(row) for row in connection.execute(sa.select(self.forms).order_by(
                self.forms.c.name, self.forms.c.created_at)).mappings()]
            columns = [column for name, column in self.versions.c.items() if name not in ('content', 'backgrounds',
                                                                                         'template')]
            versions = [dict(row) for row in connection.execute(sa.select(*columns).order_by(
                self.versions.c.form_id, self.versions.c.number)).mappings()]
        by_form = {}
        for version in versions:
            by_form.setdefault(version['form_id'], []).append(version)
        return [{**form, 'versions': by_form.get(form['id'], [])} for form in forms]

    def get_form(self, form_id):
        return next((form for form in self.list_forms() if form['id'] == form_id), None)

    def version(self, *, version_id=None, address=None):
        with read_connection(self.engine) as connection:
            column = self.versions.c.id if version_id is not None else self.versions.c.address
            row = connection.execute(sa.select(self.versions).where(
                column == (version_id if version_id is not None else address))).mappings().one_or_none()
            return self._version(connection, row) if row is not None else None

    def latest(self, form_id):
        with read_connection(self.engine) as connection:
            row = connection.execute(sa.select(self.versions).where(self.versions.c.form_id == form_id)
                                     .order_by(self.versions.c.number.desc()).limit(1)).mappings().one_or_none()
            return self._version(connection, row) if row is not None else None

    def contents(self, version_ids):
        """{version id: parsed content} for some versions, reading only their content column."""
        if not version_ids:
            return {}
        with read_connection(self.engine) as connection:
            rows = connection.execute(sa.select(self.versions.c.id, self.versions.c.content).where(
                self.versions.c.id.in_(sorted(version_ids)))).all()
        return {identity: json.loads(content) for identity, content in rows}

    def template(self, version_id):
        with read_connection(self.engine) as connection:
            row = connection.execute(sa.select(self.versions.c.template, self.versions.c.template_media_type)
                                     .where(self.versions.c.id == version_id)).one_or_none()
        if row is None or row[0] is None:
            return None
        return base64.b64decode(row[0]), row[1]

    def addresses(self):
        """Every form version held here: what the signed holdings answer lists."""
        with read_connection(self.engine) as connection:
            rows = connection.execute(sa.select(self.versions.c.address, self.versions.c.title,
                                                self.versions.c.number).order_by(
                self.versions.c.title, self.versions.c.number)).all()
        return [{'address': address, 'title': title, 'version': number} for address, title, number in rows]

    # Ledger --------------------------------------------------------------------------------------
    def record(self, **values):
        now = values.pop('now', None) or utcnow()
        identity = uuid4().hex
        with write_transaction(self.engine) as connection:
            if values.get('message_id') is not None:
                existing = connection.execute(sa.select(self.deliveries).where(
                    self.deliveries.c.direction == values['direction'],
                    self.deliveries.c.message_id == values['message_id'])).mappings().one_or_none()
                if existing is not None:
                    return dict(existing)
            connection.execute(self.deliveries.insert().values(id=identity, version=1, created_at=now,
                                                               updated_at=now, **values))
            return dict(connection.execute(sa.select(self.deliveries).where(
                self.deliveries.c.id == identity)).mappings().one())

    def delivery(self, identity=None, *, direction=None, message_id=None):
        with read_connection(self.engine) as connection:
            if identity is not None:
                query = sa.select(self.deliveries).where(self.deliveries.c.id == identity)
            else:
                query = sa.select(self.deliveries).where(self.deliveries.c.direction == direction,
                                                          self.deliveries.c.message_id == message_id)
            row = connection.execute(query).mappings().one_or_none()
            return dict(row) if row is not None else None

    def move(self, identity, state, *, expected=None, now=None, **values):
        """Change a delivery's state; with ``expected``, only from one of those states. Returns the row or None."""
        now = now or utcnow()
        with write_transaction(self.engine) as connection:
            row = connection.execute(sa.select(self.deliveries).where(
                self.deliveries.c.id == identity)).mappings().one_or_none()
            if row is None or (expected is not None and row['state'] not in expected):
                return None
            connection.execute(self.deliveries.update().where(self.deliveries.c.id == identity).values(
                state=state, version=row['version'] + 1, updated_at=now, **values))
            return dict(connection.execute(sa.select(self.deliveries).where(
                self.deliveries.c.id == identity)).mappings().one())

    def claim_fax(self, identity, *, decided_by, now=None):
        """Mark a delivery as being sent by fax by a person; None when it already has a fax or cannot."""
        now = now or utcnow()
        with write_transaction(self.engine) as connection:
            row = connection.execute(sa.select(self.deliveries).where(
                self.deliveries.c.id == identity)).mappings().one_or_none()
            if row is None or row['direction'] != 'outbound' or row['fax_job_id'] is not None \
                    or row['state'] not in ('mismatch', 'refused', 'not_sent', 'not_received'):
                return None
            # The claim names the fax in advance, so Faxbot can always tell whether it was queued.
            claim = CLAIM + uuid4().hex
            changed = connection.execute(self.deliveries.update().where(
                self.deliveries.c.id == identity, self.deliveries.c.fax_job_id.is_(None)).values(
                fax_job_id=claim, decided_by=decided_by, decided_at=now, version=row['version'] + 1, updated_at=now))
            return claim if changed.rowcount == 1 else None

    def finish_fax(self, identity, claim, job_id):
        with write_transaction(self.engine) as connection:
            connection.execute(self.deliveries.update().where(
                self.deliveries.c.id == identity, self.deliveries.c.fax_job_id == claim).values(
                fax_job_id=job_id, updated_at=utcnow()))

    def fax_state(self, job_id):
        """A sent fax's state in plain words, or None when there is no such fax."""
        with read_connection(self.engine) as connection:
            if connection.execute(sa.select(self.jobs.c.id).where(self.jobs.c.id == job_id)).first() is None:
                return None
            state = connection.scalar(sa.select(self.outbound.c.state).where(self.outbound.c.id == job_id))
        return FAX_STATE_TEXT.get(state, 'in Faxes, Sent')

    def settle_claim(self, identity, *, window=CLAIM_WINDOW, now=None):
        """Settle a person's earlier claim to fax a form's pages, for certain.

        Returns ``linked`` (its fax was queued; it is now linked), ``cleared``
        (it is older than ``window`` and no fax was queued, so nothing was sent
        and the form may be faxed), ``pending`` (still within ``window``) or
        ``none`` (no claim). Never queues a fax.
        """
        now = now or utcnow()
        with write_transaction(self.engine) as connection:
            row = connection.execute(sa.select(self.deliveries).where(
                self.deliveries.c.id == identity)).mappings().one_or_none()
            claim = row['fax_job_id'] if row is not None else None
            job_id = claim_job(claim)
            if job_id is None:
                return 'none'
            where = (self.deliveries.c.id == identity, self.deliveries.c.fax_job_id == claim)
            if connection.execute(sa.select(self.jobs.c.id).where(self.jobs.c.id == job_id)).first() is not None:
                connection.execute(self.deliveries.update().where(*where).values(fax_job_id=job_id, updated_at=now))
                return 'linked'
            if row['decided_at'] is not None and now - row['decided_at'] < window:
                return 'pending'
            connection.execute(self.deliveries.update().where(*where).values(
                fax_job_id=None, decided_by=None, decided_at=None, version=row['version'] + 1, updated_at=now))
            return 'cleared'

    def release_fax(self, identity, claim):
        with write_transaction(self.engine) as connection:
            connection.execute(self.deliveries.update().where(
                self.deliveries.c.id == identity, self.deliveries.c.fax_job_id == claim).values(
                fax_job_id=None, decided_by=None, decided_at=None, updated_at=utcnow()))

    def uncertain(self, *, limit=20):
        with read_connection(self.engine) as connection:
            return [dict(row) for row in connection.execute(sa.select(self.deliveries).where(
                self.deliveries.c.direction == 'outbound', self.deliveries.c.route == 'direct',
                self.deliveries.c.state.in_(('sending', 'uncertain'))).order_by(
                self.deliveries.c.updated_at).limit(limit)).mappings()]

    def recent(self, *, direction=None, limit=100):
        query = sa.select(self.deliveries).order_by(self.deliveries.c.created_at.desc()).limit(limit)
        if direction is not None:
            query = query.where(self.deliveries.c.direction == direction)
        with read_connection(self.engine) as connection:
            return [dict(row) for row in connection.execute(query).mappings()]

    def received(self, *, limit=200):
        """Matched arrivals that a partner's direct delivery filed, with what they were filed as."""
        d, direct, items = self.deliveries, self.direct, self.intake
        query = (sa.select(d, direct.c.id.label('direct_delivery_id'), direct.c.recipient_number.label('to_number'))
                 .select_from(d.join(direct, sa.and_(direct.c.direction == 'inbound',
                                                     direct.c.message_id == d.c.message_id,
                                                     direct.c.state == 'accepted')))
                 .where(d.c.direction == 'inbound', d.c.state == 'matched')
                 .order_by(d.c.created_at.desc()).limit(limit))
        with read_connection(self.engine) as connection:
            rows = [dict(row) for row in connection.execute(query).mappings()]
            for row in rows:
                row['intake_item_id'] = connection.scalar(sa.select(items.c.id).where(
                    items.c.direct_delivery_id == row['direct_delivery_id']).limit(1))
                row['inbound_fax_id'] = self._filed_fax(connection, row['message_id'])
        return rows

    def _filed_fax(self, connection, message_id):
        """The received fax a direct arrival was filed as, where this installation files them that way."""
        imports = self.imports
        return connection.scalar(sa.select(imports.c.inbound_fax_id).where(
            imports.c.operation_id == message_id, imports.c.account.like('direct:%')).limit(1))
