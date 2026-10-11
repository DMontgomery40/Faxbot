"""Combinations, group 8: ordinary inbound x email delivery x ownership x provenance (brief 104).

A received fax goes through the real stores end to end on a migrated installation: the provider's (or Asterisk's)
notification starts its import (``inbound.acquisition.ImportStore``) and places it in a mailbox by its number, the
fetched document completes it, the email worker (``intake.worker.IntakeWorker`` over a real local SMTP server)
delivers it, and the work queue (``work.store.WorkStore``) gives it one item and its owner. Notifications are
repeated at every stage, from two accounts, with other content, and the email server fails in each way it can.

What applied is read back from the fax's own records: ``inbound_faxes``, ``inbound_imports``, ``intake_items``,
``work_items`` and ``work_events``, and the messages the SMTP server accepted. The invariant:

- one received fax is one record and one email (and one work item), however often it is reported;
- the owner is assigned once: by a person, or by one escalation to the mailbox's backup, never again by a
  replay, a restart or a duplicate report;
- provenance is kept: the account it arrived on (as its non-secret identity), the number it was sent to, the
  provider's own fax ID, and the document's digest, which is the digest of the bytes emailed.

SQLite and PostgreSQL. Synthetic numbers, accounts and documents; nothing leaves the test but local SMTP.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from email import message_from_bytes, policy
import hashlib
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from api.app.config_values import ConfigurationValues
from api.app.inbound.acquisition import ImportStore, account_identity
from api.app.intake.email import AmbiguousFailure, send
from api.app.intake.store import IntakeStore
from api.app.intake.worker import IntakeWorker
from api.app.work.store import WorkStore
from api.app.work.worker import WorkWorker
from api.tests.combinations_support import Case, database, record  # noqa: F401 - fixture
from api.tests.test_inbound_acquisition import pdf_bytes
from api.tests.test_intake import email_settings, smtp  # noqa: F401 - fixture
from api.tests.test_work_store import WorkWorld, operator

GROUP = '8 inbound x email x ownership x provenance'
INVARIANT = 'one record, one email, one work item; owner assigned once; account, number, fax ID and digest kept'
FRONT = '+15550100001'  # the Front Desk mailbox's number (InboundWorld)
SENDER = '+15559990000'
ACCOUNTS = {'phaxio': 'synthetic-phaxio-key-one', 'sinch': 'synthetic-project', 'sip': 'asterisk'}
OPERATIONS = {'phaxio': '9101', 'sinch': '01SYNTHETICFAX', 'sip': '1791049108.7'}


class Arrivals(WorkWorld):
    """The work world (mailboxes, people, work items) with the import store and the email store beside it, on the
    real clock (the email connector delivers only what arrives after it was set up)."""

    def __init__(self, engine, folder, port):
        super().__init__(engine)
        self.folder = folder
        self.imports = ImportStore(self.inbound)
        self.intake = IntakeStore(engine, None)  # the connector has no password, so nothing is sealed
        self.connector = self.intake.create_connector(name='Front desk', settings=email_settings(port))
        self.values = ConfigurationValues.from_environment({'FAX_DATA_DIR': str(folder)})
        self.documents = {}

    def notified(self, source, *, account=None, operation=None, digest=None):
        """The provider's (or Asterisk's) notification of one received fax: begins or resumes its import."""
        return self.imports.begin(
            source=source, account=account_identity(source, account or ACCOUNTS[source]),
            operation_id=operation or OPERATIONS[source], backend=source, to_number=FRONT, from_number=SENDER,
            reported_pages=1, report={'id': operation or OPERATIONS[source]}, artifact_digest=digest)

    def fetched(self, begun, text='Synthetic received fax'):
        """The fetcher's lease and the stored document: completes the import as ``inbound.worker`` does."""
        claim = self.imports.claim()
        assert claim is not None and claim['id'] == begun.import_id
        data = pdf_bytes(text)
        path = self.folder / f'{begun.inbound_fax_id}-{hashlib.sha256(data).hexdigest()[:12]}.pdf'
        path.write_bytes(data)
        self.documents[begun.inbound_fax_id] = data
        return self.imports.complete(begun.import_id, artifact_path=str(path), digest=hashlib.sha256(data).hexdigest(),
                                     size=len(data), pages=1)

    def people_at(self, moment):
        """The work service at ``moment`` on the real clock, with this world's synthetic sessions signed in just
        before it (the world's people were made at its fixed time)."""
        sessions = self.tables['access_sessions']
        with self.engine.begin() as connection:
            connection.execute(sessions.update().values(created_at=moment - timedelta(minutes=5),
                                                        last_used_at=moment - timedelta(minutes=1),
                                                        expires_at=moment + timedelta(hours=1)))
        return self.service_at(moment)

    def emailer(self, sender=send):
        return IntakeWorker(self.intake, values=lambda: self.values, sender=sender)

    def rows(self, table, **where):
        found = sa.Table(table, sa.MetaData(), autoload_with=self.engine)
        query = sa.select(found)
        for column, value in where.items():
            query = query.where(found.c[column] == value)
        with self.engine.connect() as connection:
            return [dict(row) for row in connection.execute(query).mappings()]


@pytest.fixture
def arrivals(database, tmp_path, smtp):  # noqa: F811 - the fixtures
    recorder, port = smtp
    world = Arrivals(database, tmp_path, port)
    world.smtp = recorder
    return world


def attachments(recorder):
    """The PDF attached to each message the SMTP server accepted."""
    found = []
    for _, _, content in recorder.messages:
        message = message_from_bytes(content, policy=policy.default)
        [attachment] = list(message.iter_attachments())
        found.append(attachment.get_content())
    return found


def assert_invariant(world, source, inbound_id, *, account=None, operation=None):
    """One record, one email, one work item for the fax, with its provenance kept."""
    operation = operation or OPERATIONS[source]
    faxes = world.rows('inbound_faxes', id=inbound_id)
    imports = world.rows('inbound_imports', inbound_fax_id=inbound_id)
    items = world.rows('intake_items', inbound_fax_id=inbound_id)
    work = world.rows('work_items', inbound_fax_id=inbound_id)
    assert len(faxes) == len(imports) == len(items) == len(work) == 1
    [fax], [imported], [item], [owned] = faxes, imports, items, work
    data = world.documents[inbound_id]
    digest = hashlib.sha256(data).hexdigest()
    # Provenance: the account it arrived on (its non-secret identity), the number, the provider's fax ID, the digest.
    assert imported['account'] == account_identity(source, account or ACCOUNTS[source])
    assert (account or ACCOUNTS[source]) not in imported['account'] or source in ('sinch', 'sip')
    assert (imported['source'], imported['operation_id'], imported['to_number']) == (source, operation, FRONT)
    assert (fax['to_number'], fax['from_number'], fax['provider_sid']) == (FRONT, SENDER, operation)
    assert fax['sha256'] == imported['artifact_digest'] == owned['content_digest'] == digest
    assert world.resource_of(inbound_id)['parent_id'] == 'mailbox-front' and owned['mailbox_id'] == 'front'
    # The email: delivered once, carrying exactly the stored document.
    assert item['state'] == 'delivered' and item['attempts'] >= 1 and item['connector_id'] == world.connector.id
    assert attachments(world.smtp).count(data) == 1
    return fax, imported, item, owned


# One received fax, reported again at every stage ------------------------------------------------------------------

@pytest.mark.parametrize('when', ['before the fetch', 'after the fetch', 'after the email'])
@pytest.mark.parametrize('source', sorted(ACCOUNTS))
def test_one_received_fax_is_one_record_one_email_and_one_work_item_however_often_it_is_reported(arrivals, source,
                                                                                                 when):
    world = arrivals
    begun = world.notified(source)
    if when == 'before the fetch':
        again = world.notified(source)
        assert (again.import_id, again.created) == (begun.import_id, False)
    assert world.fetched(begun).stored
    if when == 'after the fetch':
        assert world.notified(source).created is False
    assert world.emailer().step() is True
    if when == 'after the email':
        assert world.notified(source).created is False
    # Nothing more to email, and the work queue gives the fax exactly one item, however often it is fed.
    assert world.emailer().step() is False
    assert world.work.feed() == 1 and world.work.feed() == 0
    fax, imported, item, owned = assert_invariant(world, source, begun.inbound_fax_id)
    assert len(world.smtp.messages) == 1 and imported['state'] == 'received'
    record(Case(GROUP, f'{source} fax reported again {when}', (source, 'duplicate report', 'email', 'work'),
                ('one record', 'one email', 'one work item', 'provenance kept'), INVARIANT))


# Ownership ----------------------------------------------------------------------------------------------------------

def test_the_owner_is_assigned_once_through_escalation_a_restart_and_a_late_duplicate_report(arrivals):
    world = arrivals
    world.hours = 1
    operator(world, 'dana'), operator(world, 'sam')
    admin = operator(world, 'admin', 'installation', 'role_administrator')
    world.setting('front', backup_principal_id='sam')
    begun = world.notified('phaxio')
    world.fetched(begun)
    assert world.emailer().step() is True
    now = datetime.utcnow()
    assert world.work.feed(installation_hours=1, now=now) == 1
    item = world.item_for(begun.inbound_fax_id)
    world.people_at(now).assign(admin, item['id'], 'dana', version=1)
    later = now + timedelta(hours=2)
    assert world.work.escalate(world.control, now=later) == 1
    # A late duplicate report of the same fax: no new record, email or item, and the owner stays.
    assert world.notified('phaxio').created is False
    assert world.emailer().step() is False and world.work.feed(installation_hours=1, now=later) == 0
    assert world.work.escalate(world.control, now=later + timedelta(minutes=5)) == 0
    # A restarted work worker decides nothing again.
    restarted = WorkStore(world.engine)
    WorkWorker(restarted, control=lambda: world.control,
               values=lambda: SimpleNamespace(work_acknowledge_hours=9)).step(now=later + timedelta(hours=1))
    owned = world.item_for(begun.inbound_fax_id)
    assert owned['owner_principal_id'] == 'sam' and owned['due_at'] == item['due_at']
    kinds = [event['kind'] for event in world.events(item['id'])]
    assert kinds == ['received', 'assigned', 'escalated']
    assert_invariant(world, 'phaxio', begun.inbound_fax_id)
    record(Case(GROUP, 'owner: assigned, escalated once, duplicate report, restart',
                ('phaxio', 'email', 'assign', 'escalate', 'duplicate report', 'restart'),
                ('assigned dana', 'escalated to sam once', 'nothing reassigned'), INVARIANT))


# Provenance ---------------------------------------------------------------------------------------------------------

def test_the_same_fax_id_on_two_accounts_is_two_faxes_each_with_its_own_account(arrivals):
    world = arrivals
    first = world.notified('phaxio')
    second = world.notified('phaxio', account='synthetic-phaxio-key-two')
    assert second.created and second.inbound_fax_id != first.inbound_fax_id
    world.fetched(first, 'first account copy')
    world.fetched(second, 'second account copy')
    while world.emailer().step():
        pass
    assert world.work.feed() == 2
    _, one, _, _ = assert_invariant(world, 'phaxio', first.inbound_fax_id)
    _, two, _, _ = assert_invariant(world, 'phaxio', second.inbound_fax_id, account='synthetic-phaxio-key-two')
    assert one['account'] != two['account'] and len(world.smtp.messages) == 2
    record(Case(GROUP, 'same provider fax ID on two accounts', ('phaxio x2 accounts', 'email', 'work'),
                ('two records', 'two emails', 'each with its own account'), INVARIANT))


def test_a_later_copy_with_other_content_is_refused_and_the_first_copy_is_what_was_emailed(arrivals):
    world = arrivals
    begun = world.notified('sip')
    world.fetched(begun)
    assert world.emailer().step() is True
    world.work.feed()
    other = hashlib.sha256(pdf_bytes('a different document under the same call')).hexdigest()
    replay = world.notified('sip', digest=other)
    assert replay.conflict and replay.import_id == begun.import_id
    assert world.emailer().step() is False and world.work.feed() == 0
    _, imported, _, owned = assert_invariant(world, 'sip', begun.inbound_fax_id)
    assert imported['state'] == 'conflict' and owned['content_digest'] != other
    record(Case(GROUP, 'later copy with other content', ('sip', 'replay with other content', 'email', 'work'),
                ('conflict recorded', 'first copy kept and emailed once'), INVARIANT, 'impossible',
                alternate='the first copy is kept; the later one is refused and recorded as a conflict'))


# The email server fails -----------------------------------------------------------------------------------------

def test_a_temporary_email_refusal_backs_off_and_a_duplicate_report_meanwhile_sends_nothing_extra(arrivals):
    world = arrivals
    begun = world.notified('sinch')
    world.fetched(begun)
    world.smtp.rcpt_reply = '451 Try again later'
    assert world.emailer().step() is True
    [item] = world.rows('intake_items', inbound_fax_id=begun.inbound_fax_id)
    assert item['state'] == 'received' and item['attempts'] == 1 and world.smtp.messages == []
    assert world.notified('sinch').created is False
    assert world.emailer().step() is False  # not due yet: backing off
    world.smtp.rcpt_reply = None
    due = world.intake.claim(now=item['next_attempt_at'] + timedelta(seconds=1))
    world.emailer().deliver(due)
    world.work.feed()
    assert_invariant(world, 'sinch', begun.inbound_fax_id)
    assert len(world.smtp.messages) == 1
    record(Case(GROUP, 'email refused for now, then a duplicate report', ('sinch', 'email 451', 'duplicate report'),
                ('retried once', 'one email'), INVARIANT))


@pytest.mark.parametrize('fault', ['answer lost after the message', 'worker stopped mid-send'])
def test_an_email_that_may_have_gone_is_never_sent_again_by_itself(arrivals, fault):
    world = arrivals
    begun = world.notified('phaxio')
    world.fetched(begun)
    tried = []
    if fault == 'answer lost after the message':
        def lost(settings, password, message):
            tried.append(message['Message-ID'])
            raise AmbiguousFailure('The email server stopped responding after the fax was sent.')
        assert world.emailer(sender=lost).step() is True
        expected = 'Check the inbox before sending it again.'
    else:
        world.intake.feed_inbound()
        claimed = world.intake.claim()
        tried.append(claimed['id'])
        assert world.intake.recover_expired(now=datetime.utcnow() + timedelta(minutes=3)) == 1
        expected = 'check the inbox before sending it again.'
    [item] = world.rows('intake_items', inbound_fax_id=begun.inbound_fax_id)
    assert item['state'] == 'failed' and item['last_error'].endswith(expected)
    # A duplicate report and more worker steps send nothing.
    assert world.notified('phaxio').created is False
    assert world.emailer().step() is False and world.smtp.messages == [] and len(tried) == 1
    assert world.work.feed() == 1
    [owned] = world.rows('work_items', inbound_fax_id=begun.inbound_fax_id)
    assert owned['content_digest'] == hashlib.sha256(world.documents[begun.inbound_fax_id]).hexdigest()
    record(Case(GROUP, f'email: {fault}', ('phaxio', 'email', fault), ('waits for a person', 'not resent'),
                INVARIANT))
