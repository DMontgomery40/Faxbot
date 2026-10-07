"""Deliver intake items over email, once each, with retries only for definite failures."""
from pathlib import Path
import re

from ..routing.database import utcnow
from .email import AmbiguousFailure, DefiniteFailure, build_message, send


MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024


class DocumentUnavailable(RuntimeError):
    pass


class ConnectorSecrets:
    """Seal connector passwords with the installation configuration key, bound to the connector."""

    def __init__(self, configuration):
        self.configuration = configuration

    def _context(self):
        with self.configuration.engine.connect() as connection:
            head = self.configuration._head(connection)
        if head is None:
            raise RuntimeError('Installation configuration is not ready.')
        return self.configuration._cipher(), head['installation_id']

    def seal(self, password, record_id):
        cipher, installation = self._context()
        return cipher.seal({'password': password}, installation_id=installation, kind='intake_connector',
                           record_id=record_id)

    def open(self, envelope, record_id):
        cipher, installation = self._context()
        payload = cipher.open(envelope, installation_id=installation, kind='intake_connector', record_id=record_id)
        password = payload.get('password')
        return password if isinstance(password, str) else ''


def load_document(location, values):
    """Read the original PDF from local storage or the configured S3 bucket."""
    if not isinstance(location, str) or not location:
        raise DocumentUnavailable()
    if location.startswith('s3://'):
        from ..storage import S3Storage
        storage = S3Storage(bucket=values.s3_bucket, prefix=values.s3_prefix, region=values.s3_region,
                            endpoint=values.s3_endpoint_url, kms_key=values.s3_kms_key_id)
        stream, _ = storage.get_pdf_stream(location)
        with stream:
            data = stream.read(MAX_ATTACHMENT_BYTES + 1)
    else:
        path = Path(location)
        if path.is_symlink() or not path.is_file():
            raise DocumentUnavailable()
        with path.open('rb') as handle:
            data = handle.read(MAX_ATTACHMENT_BYTES + 1)
    if not data.startswith(b'%PDF'):
        raise DocumentUnavailable()
    return data


def attachment_name(item):
    digits = re.sub(r'[^0-9]', '', item.get('from_number') or '') or 'unknown'
    return f"fax-{item['received_at']:%Y%m%d-%H%M}-{digits[-15:]}.pdf"


class IntakeWorker:
    def __init__(self, store, *, values, sender=send):
        """``values`` returns the active configuration values (storage settings)."""
        self.store, self.values, self.sender = store, values, sender

    def deliver(self, item):
        store = self.store
        connector = store.connector_for(item['to_number'])
        if connector is None:
            store.record_failed(item, message='No email delivery is set up for this number.')
            return
        try:
            document = load_document(store.document(item), self.values())
        except Exception:
            store.record_failed(item, message='The fax document is no longer available.', connector_id=connector.id)
            return
        if len(document) > MAX_ATTACHMENT_BYTES:
            store.record_failed(item, message='This fax is too large to send by email.', connector_id=connector.id)
            return
        try:
            password = store.password(connector.id)
        except Exception:
            store.record_failed(item, message='The saved email password could not be read; enter it again.',
                                connector_id=connector.id)
            return
        message = build_message(connector.settings, item, document, filename=attachment_name(item),
                                time_zone=getattr(self.values(), 'time_zone', '') or '')
        # Experimental encoded pages: attach the decoded original beside the fax as received.
        from ..codec import receive as codec_receive
        from ..codec.store import KeySeal
        configuration = getattr(getattr(store, 'secrets', None), 'configuration', None)
        attachment, note = codec_receive.email_extras(
            store.engine, item, document, folder=getattr(self.values(), 'fax_data_dir', '.'),
            seal=KeySeal(configuration) if configuration is not None else None)
        codec_receive.attach(message, attachment, note)
        try:
            delivery = self.sender(connector.settings, password, message)
        except DefiniteFailure as error:
            if error.temporary:
                store.record_retry(item, message=str(error), connector_id=connector.id)
            else:
                store.record_failed(item, message=str(error), connector_id=connector.id)
            return
        except AmbiguousFailure as error:
            store.record_failed(item, message=str(error) + ' Check the inbox before sending it again.',
                                connector_id=connector.id)
            return
        store.record_delivered(item, connector_id=connector.id, reference=delivery.reference,
                               recipients=delivery.accepted, now=utcnow())

    def step(self):
        self.store.recover_expired()
        self.store.feed_inbound()
        item = self.store.claim()
        if item is None:
            return False
        self.deliver(item)
        return True
