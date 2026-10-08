"""Background work for digital routes, once a minute: HISP mailboxes, Direct timeouts, uncertain FHIR documents.

- Each HISP account that is on and set up has its mailbox read
  (``direct_message.Receiver``): delivery notices settle sent messages, and
  received documents are filed into the account's mailbox through the generic
  import contract, once each (keyed by the Message-ID).
- Sent Direct messages whose notice did not come within the account's wait
  become uncertain (``direct_message.sweep``), never failed.
- Uncertain FHIR documents are looked up by their identifier at most every
  15 minutes (``fhir.reconcile``); found means delivered. Nothing is ever sent
  again by itself.
"""
from datetime import timedelta
import hashlib
import logging

from ..routing.database import utcnow
from . import direct_message, fhir
from .accounts import digital_accounts
from .store import DigitalStore


RECONCILE_EVERY = timedelta(minutes=15)
logger = logging.getLogger(__name__)


def filer(access_source, values):
    """``file_document`` for ``Receiver``: one received PDF or TIFF into the account's mailbox, once."""
    def file_document(account, received, index, name, media_type, data):
        from ..inbound.acquisition import account_identity
        from ..intake.sources import documents
        from ..work.imports import ImportConflict, ImportManifest, record_import
        access = access_source()
        if access is None:
            raise direct_message.DirectFailure('Received documents cannot be filed until Faxbot has started.')
        kind = 'pdf' if data.startswith(b'%PDF-') else 'tiff'
        try:
            path, pages, digest, converted = documents.to_pdf(data, kind, values.fax_data_dir)
        except documents.Unreadable:
            return {'status': 'unreadable'}
        operation = hashlib.sha256(received.message_id.encode('utf-8')).hexdigest()
        manifest = ImportManifest(source_system='Direct message', operation_id=operation, revision=str(index),
                                  source_received_at=received.received_at, to_number=None, from_number=None,
                                  pages=pages)
        report = {'source_system': 'Direct message', 'operation_id': operation, 'revision': str(index),
                  'message_id': received.message_id[:300], 'sender': received.sender, 'file_name': name[:200],
                  'source_sha256': hashlib.sha256(data).hexdigest(), 'account': account.key}
        try:
            status, import_id, inbound_id = record_import(
                access, values, account=account_identity('import', 'digital:' + account.key), manifest=manifest,
                path=path, digest=digest, report=report, compare=not converted,
                mailbox_id=account.setting('mailbox_id'))
        except ImportConflict:
            return {'status': 'conflict'}
        finally:
            documents.discard(path)
        return {'status': status, 'import_id': import_id, 'inbound_fax_id': inbound_id}
    return file_document


class DigitalWorker:
    def __init__(self, engine, *, values, access=None, delivery=None, transport=None, fhir_transport=None,
                 mailbox_factory=None):
        self.engine = engine
        self.values = values                    # callable: the configuration in force now
        self.access = access or (lambda: None)
        self.delivery = delivery                # callable: the OutboundStore, or None
        self.transport = transport or direct_message.Transport()
        self.fhir_transport = fhir_transport or fhir.Transport()
        self.mailbox_factory = mailbox_factory  # (account) -> Mailbox-like, for tests
        self.problems = {}                      # account key -> the last mailbox problem, for health

    def step(self, now=None):
        values = self.values()
        store = DigitalStore(self.engine)
        delivery = self.delivery() if self.delivery else None
        accounts = {account.key: account for account in digital_accounts(values)}
        for account in accounts.values():
            if account.kind != 'hisp' or not account.enabled or not account.set_up:
                continue
            receiver = direct_message.Receiver(
                store, account, transport=self.transport, file_document=filer(self.access, values),
                delivery=delivery,
                mailbox_factory=(lambda account=account: self.mailbox_factory(account)) if self.mailbox_factory
                else None)
            receiver.check(now=now)
            self.problems[account.key] = receiver.last_problem
        direct_message.sweep(store, delivery=delivery, accounts_for=accounts.get, now=now)
        self.reconcile_fhir(store, accounts, delivery, now=now)
        return False

    def reconcile_fhir(self, store, accounts, delivery, *, now=None):
        moment = now or utcnow()
        for row in store.in_states(('uncertain',), kind='fhir', limit=20):
            account = accounts.get(row['account_key'])
            if account is None or not account.enabled:
                continue
            asked = [event for event in store.events_for(row['id']) if event['kind'] in ('not_found', 'search_failed')]
            if asked and asked[-1]['created_at'] + RECONCILE_EVERY > moment:
                continue
            fhir.reconcile(store, row, account, self.fhir_transport, delivery=delivery, now=moment)
