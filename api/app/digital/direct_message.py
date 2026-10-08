"""The Direct route: a fax delivered as a Direct Secure Message through the organization's HISP.

Sending (``DirectSender``)
--------------------------
- The fax's PDF goes in an IHE XDM package (``xdm.py``) attached to a message
  from the account's Direct address to the recipient's, with a short text part.
- Security follows the account's setting (ONC Implementation Guide for Direct
  Edge Protocols v1.1 lets the HISP do it for an edge system):
  ``faxbot``: Faxbot is the security agent. It finds the recipient's
  certificate (DNS CERT, then LDAP; ``certificates.discover``), keeps only one
  that chains to the account's trust bundle, signs with the account's
  certificate and encrypts (AES-256-CBC) the whole message wrapped as
  message/rfc822 (RFC 5751 3.1). ``hisp``: Faxbot hands the plain message to
  the HISP, which signs and encrypts it.
- Delivery notification is requested as the Implementation Guide for Delivery
  Notification in Direct v1.0 (2012) says: ``Disposition-Notification-To`` and
  ``Disposition-Notification-Options: X-DIRECT-FINAL-DESTINATION-DELIVERY=
  optional,true``, with a Message-ID made from the attempt.
- Submission is SMTP with STARTTLS and sign-in, step by step, so Faxbot knows
  what the HISP accepted. A refusal before the HISP's final answer to the
  message (connection, TLS, sign-in, sender, recipient, DATA) means nothing
  was accepted: ``DirectRefused``, and the fax may go by its own route in the
  same attempt. 250 after the message: accepted by the HISP, the fax stays in
  progress. A connection lost after the message was sent and before the
  answer: uncertain, never sent again by itself.

Outcomes (``record_notice``)
----------------------------
- ``processed`` MDN: the recipient's HISP received it, decrypted it and
  trusted the sender. With delivery confirmation requested the fax stays in
  progress; without it, this is the last word, and the fax counts as
  delivered to the recipient's HISP.
- ``dispatched`` MDN (with X-DIRECT-FINAL-DESTINATION-DELIVERY): delivered to
  the recipient's system. The fax is delivered.
- ``failed`` MDN or a failed delivery status report (RFC 3464): not
  delivered; the fax may go by its next route.
- No notice within the account's wait (default 60 minutes for each, the
  Direct reference implementation's default of 3,600,000 ms; the guide says
  "a reasonable timeframe"): uncertain (``sweep``). The fax waits for a
  person and is never sent again by itself; a late notice still settles it.

Receiving (``Receiver``)
------------------------
The HISP mailbox is read over IMAP (``intake/sources/imap.Mailbox``). A
notice settles a sent message. Any other message with documents is checked
(decrypted and its signature trusted, when Faxbot is the security agent) and
each PDF or TIFF is filed into the account's mailbox through the generic
import contract (``work/imports.record_import``), keyed by the Message-ID, so
it is filed once. As the security agent Faxbot then sends "processed", and
"dispatched" once filed when the sender asked. A message Faxbot cannot file
(no PDF, an untrusted sender) is recorded as not filed, with one sentence.

Not yet run against a real HISP.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime, getaddresses, parsedate_to_datetime
import hashlib
import logging
import re
import secrets
import smtplib
import ssl

from ..routing.database import utcnow
from . import certificates, lookup, smime, xdm
from .store import DigitalInputError, DigitalStore


OPTIONS = 'X-DIRECT-FINAL-DESTINATION-DELIVERY=optional,true'
FINAL = 'x-direct-final-destination-delivery'
DEFAULT_WAIT = timedelta(minutes=60)
MAX_MESSAGE = 32 * 1024 * 1024
CRLF = b'\r\n'
logger = logging.getLogger(__name__)


class DirectFailure(RuntimeError):
    """Something may have reached the HISP; the attempt is uncertain."""


@dataclass
class Transport:
    """What Direct talks to; tests replace each piece with a fake HISP, resolver or directory."""
    smtp_connect: object = None      # (host, port, timeout) -> smtplib.SMTP-like
    ssl_context: object = None
    dns_lookup: object = None        # name -> [DER certificates]
    ldap_lookup: object = None       # address -> [DER certificates]
    crl_fetch: object = None         # url -> bytes; raises LookupError
    imap_connect: object = None      # (host, port, context, timeout) -> imaplib-like
    timeout: float = 60.0
    # () -> bool: private and local addresses may be reached (DIRECT_ALLOW_PRIVATE_PEERS). Addresses found in
    # certificates and DNS records (issuers, revocation lists, directories) are public-only otherwise.
    allow_private: object = None

    def private_allowed(self):
        return bool(self.allow_private()) if self.allow_private is not None else False

    def fetch(self, url):
        """A revocation list or an issuer's certificate; raises LookupError."""
        if self.crl_fetch is not None:
            return self.crl_fetch(url)
        return lookup.http_fetch(url, allow_private=self.private_allowed())

    def smtp(self, host, port):
        if self.smtp_connect is not None:
            return self.smtp_connect(host, port, self.timeout)
        if port == 465:
            return smtplib.SMTP_SSL(host, port, timeout=self.timeout, context=self.context())
        return smtplib.SMTP(host, port, timeout=self.timeout)

    def context(self):
        return self.ssl_context or ssl.create_default_context()

    def certificates_for(self, address, *, anchors, intermediates=()):
        private = self.private_allowed()
        return certificates.discover(
            address, anchors=anchors, intermediates=intermediates,
            dns_lookup=self.dns_lookup or (lambda name: lookup.dns_certificates(
                name, fetch=lambda url: lookup.https_fetch(url, allow_private=private))),
            ldap_lookup=self.ldap_lookup or (lambda found: lookup.ldap_certificates(
                found, connect=lookup.public_connect(private))),
            crl_fetch=self.fetch)


def anchors_for(store, account):
    """The trust anchors in force for a HISP account (its newest trust bundle), or []."""
    bundle = store.bundle(account.key)
    if bundle is None:
        return []
    return certificates.load_certificates(bundle['content'].encode('ascii'))


def own_identity(account):
    """(certificate, chain, private key) for signing and decrypting, from the account's settings."""
    loaded = certificates.load_certificates(account.setting('certificate').encode('ascii'))
    key = certificates.load_private_key(account.secret('private_key'))
    return loaded[0], loaded[1:], key


def wait_for(account):
    minutes = account.setting('wait_minutes') or 60
    return timedelta(minutes=int(minutes))


def message_id_for(attempt_id, address):
    domain = address.rpartition('@')[2] or 'faxbot.invalid'
    return f'<faxbot.{attempt_id}.{secrets.token_hex(4)}@{domain}>'


def _header(name, value):
    value = re.sub(r'[\r\n]+', ' ', str(value))
    return f'{name}: {value}'.encode('utf-8')


def _base64(data):
    return smime._base64_lines(data)


def build_message(*, sender, recipient, message_id, document, pages, organization, request_delivery, now=None,
                  document_name='FAX.PDF'):
    """(headers lines, inner entity bytes): the plain message's headers and its multipart/mixed body."""
    now = now or datetime.now(timezone.utc)
    package = xdm.build([(document_name, 'application/pdf', document)], sender=sender, recipient=recipient,
                        organization=organization, now=now)
    mark = smime.boundary('Mixed')
    pages_text = f'{pages} page{"" if pages == 1 else "s"}' if pages else 'a document'
    text = (f'{organization} sent you {pages_text} by Direct message instead of by fax. The document is in the '
            'attached package (IHE XDM).\r\n').encode('utf-8')
    body = CRLF.join([
        f'Content-Type: multipart/mixed; boundary="{mark}"'.encode('ascii'), b'',
        f'--{mark}'.encode('ascii'), b'Content-Type: text/plain; charset=utf-8',
        b'Content-Transfer-Encoding: 7bit', b'', text,
        f'--{mark}'.encode('ascii'), b'Content-Type: application/zip; name="FAX0001.zip"',
        b'Content-Transfer-Encoding: base64', b'Content-Disposition: attachment; filename="FAX0001.zip"', b'',
        _base64(package), f'--{mark}--'.encode('ascii'), b''])
    headers = [_header('From', sender), _header('To', recipient), _header('Subject', f'Document from {organization}'),
               _header('Date', format_datetime(now)), _header('Message-ID', message_id), b'MIME-Version: 1.0']
    if request_delivery:
        headers += [_header('Disposition-Notification-To', sender),
                    _header('Disposition-Notification-Options', OPTIONS)]
    return headers, body


def secure_message(headers, body, *, own_certificate, own_chain, own_key, recipients):
    """The full outgoing message: the plain message wrapped (message/rfc822), signed, encrypted."""
    inner = CRLF.join(headers) + CRLF + body
    wrapped = b'Content-Type: message/rfc822' + CRLF + CRLF + inner
    signed = smime.signed_entity(wrapped, own_certificate, own_key, chain=own_chain)
    enveloped = smime.enveloped_entity(smime.encrypt(signed, recipients))
    outer = [line for line in headers if not line.lower().startswith((b'content-type', b'mime-version'))]
    return CRLF.join(outer + [b'MIME-Version: 1.0']) + CRLF + enveloped


def plain_message(headers, body):
    return CRLF.join(headers) + CRLF + body


def _payload(message):
    data = smime.canonical(message)
    data = re.sub(rb'(?m)^\.', b'..', data)
    if not data.endswith(CRLF):
        data += CRLF
    return data + b'.' + CRLF


def submit_smtp(transport, account, *, sender, recipient, message):
    """'accepted' after a 250 to the message; raises DirectRefusedError before it; DirectFailure when unknown."""
    host, port = account.setting('smtp_host'), int(account.setting('smtp_port') or 587)
    user = account.setting('username') or account.setting('direct_address')
    try:
        client = transport.smtp(host, port)
    except (OSError, smtplib.SMTPException):
        raise DirectRefusedError('Your HISP could not be reached; nothing was sent.') from None
    try:
        try:
            client.ehlo()
            if port != 465:
                if not client.has_extn('starttls'):
                    raise DirectRefusedError('Your HISP did not offer an encrypted connection, so nothing was sent.')
                client.starttls(context=transport.context())
                client.ehlo()
            client.login(user, account.secret('password') or '')
            code, _ = client.mail(sender)
            if code != 250:
                raise DirectRefusedError('Your HISP refused the sender address; nothing was sent.')
            code, _ = client.rcpt(recipient)
            if code not in (250, 251):
                raise DirectRefusedError('Your HISP refused the recipient\'s Direct address; nothing was sent.')
            code, _ = client.docmd('DATA')
            if code != 354:
                raise DirectRefusedError('Your HISP would not take the message; nothing was sent.')
        except smtplib.SMTPAuthenticationError:
            raise DirectRefusedError('Your HISP refused the sign-in name or password; nothing was sent.') from None
        except (OSError, smtplib.SMTPException, ssl.SSLError):
            raise DirectRefusedError('The connection to your HISP failed before the message was sent; nothing was '
                                     'sent.') from None
        try:
            client.send(_payload(message))
            code, _ = client.getreply()
        except (OSError, smtplib.SMTPException):
            # The message left Faxbot and no answer came: the HISP may have taken it.
            raise DirectFailure('The connection to your HISP was lost after the message was sent.') from None
        if code == 250:
            return 'accepted'
        if 400 <= code < 600:
            # The HISP answered that it did not take the message.
            raise DirectRefusedError('Your HISP did not accept the message; nothing was delivered.')
        raise DirectFailure('Your HISP gave an answer Faxbot could not read after the message was sent.')
    finally:
        try:
            client.quit()
        except (OSError, smtplib.SMTPException):
            try:
                client.close()
            except OSError:
                pass


class DirectRefusedError(RuntimeError):
    """Nothing reached the HISP (or it refused the message); one sentence."""


@dataclass
class DirectSubmission:
    store: DigitalStore
    account: object
    transport: Transport
    row: dict
    sender: str
    recipient: str
    message: bytes
    refused: str | None = None   # a refusal found while preparing (no certificate): nothing will be sent

    async def submit(self):
        """SubmissionReceipt(None, 'in_progress') once the HISP accepted it; DirectRefused or DirectFailure."""
        from ..config_runtime import run_lifecycle_step
        from ..outbound_worker import SubmissionReceipt
        from ..routing.transport import DirectRefused
        store, row = self.store, self.row
        if self.refused:
            await run_lifecycle_step(lambda: store.move(row['id'], 'refused', expected=('sending',), kind='refused',
                                                        dedupe=f"{row['id']}:refused", detail=self.refused))
            raise DirectRefused(self.refused)
        try:
            await run_lifecycle_step(lambda: submit_smtp(self.transport, self.account, sender=self.sender,
                                                         recipient=self.recipient, message=self.message))
        except DirectRefusedError as refusal:
            sentence = str(refusal)
            await run_lifecycle_step(lambda: store.move(row['id'], 'refused', expected=('sending',), kind='refused',
                                                        dedupe=f"{row['id']}:refused", detail=sentence))
            raise DirectRefused(sentence) from None
        except DirectFailure as failure:
            sentence = str(failure)
            await run_lifecycle_step(lambda: store.move(row['id'], 'uncertain', expected=('sending',),
                                                        kind='uncertain', dedupe=f"{row['id']}:uncertain",
                                                        detail=sentence))
            raise
        await run_lifecycle_step(lambda: store.move(
            row['id'], 'submitted', expected=('sending',), kind='submitted', dedupe=f"{row['id']}:submitted",
            submitted=True, detail='Your HISP accepted it; Faxbot is waiting for the recipient to confirm.'))
        return SubmissionReceipt(None, 'in_progress')


class DirectSender:
    def __init__(self, store, account, *, transport=None):
        self.store, self.account = store, account
        self.transport = transport or Transport()

    def prepare(self, *, claim, job, view, document, values, now=None):
        """Build and record the message (state ``sending``); returns its DirectSubmission. Runs in a worker thread."""
        account, store = self.account, self.store
        sender, recipient = account.setting('direct_address'), view['address']
        organization = (getattr(values, 'direct_organization', '') or '').strip() or account.label
        message_id = message_id_for(claim.attempt_id, sender)
        security = account.setting('security') or 'faxbot'
        headers, body = build_message(sender=sender, recipient=recipient, message_id=message_id, document=document,
                                      pages=job.get('pages'), organization=organization,
                                      request_delivery=bool(account.setting('request_delivery')), now=now)
        refused, fingerprint, tried = None, None, []
        if security == 'faxbot':
            anchors = anchors_for(store, account)
            own, chain, key = own_identity(account)
            found = self.transport.certificates_for(recipient, anchors=anchors)
            tried = found.tried
            if not found.certificates:
                refused = ('Faxbot found no trusted certificate for this Direct address, so nothing was sent.'
                           if not found.refused else f'{found.refused[0]} Nothing was sent.')
                message = b''
            else:
                fingerprint = certificates.fingerprint(found.certificates[0])
                message = secure_message(headers, body, own_certificate=own, own_chain=chain, own_key=key,
                                         recipients=found.certificates)
        else:
            message = plain_message(headers, body)
        if len(message) > MAX_MESSAGE:
            refused = 'The document is too large for a Direct message, so nothing was sent.'
        row, _ = store.begin_message(
            direction='out', kind='direct', account_key=account.key, address_id=view['id'], job_id=claim.job_id,
            attempt_id=claim.attempt_id, message_id=message_id, counterpart=recipient, state='sending',
            security=security, digest=hashlib.sha256(document).hexdigest(), size=len(document),
            pages=job.get('pages'), certificate_sha256=fingerprint)
        if tried:
            store.event(row['id'], 'certificate', dedupe=f"{row['id']}:certificate",
                        details={'tried': [list(item) for item in tried]})
        return DirectSubmission(store, account, self.transport, row, sender, recipient, message, refused)


# Delivery notices --------------------------------------------------------------------------------------------------

@dataclass
class Notice:
    kind: str                    # 'processed' | 'dispatched' | 'failed' | 'delayed'
    original_message_id: str
    final_recipient: str | None = None
    detail: dict = field(default_factory=dict)


def _fields(text):
    """RFC 822-style fields of a notification part, lower-case names; blank lines separate groups (kept merged)."""
    found = {}
    current = None
    for line in text.splitlines():
        if line[:1] in (' ', '\t') and current:
            found[current] += ' ' + line.strip()
            continue
        name, colon, value = line.partition(':')
        if colon:
            current = name.strip().lower()
            found.setdefault(current, value.strip())
    return found


def _address(value):
    if not value:
        return None
    _, _, address = value.partition(';')
    return (address or value).strip().strip('<>').lower() or None


def read_notice(entity):
    """A ``Notice`` from a multipart/report message entity, or None when it is not a delivery notice."""
    headers, body = smime.split_headers(entity)
    content_type = headers.get('content-type', '')
    if smime.media_type(content_type) != 'multipart/report':
        return None
    report = (smime.parameter(content_type, 'report-type') or '').lower()
    mark = smime.parameter(content_type, 'boundary')
    if not mark:
        return None
    try:
        parts = smime.split_multipart(body, mark)
    except smime.SmimeError:
        return None
    for part in parts[1:]:
        part_headers, part_body = smime.split_headers(part)
        kind = smime.media_type(part_headers.get('content-type'))
        text = smime.transfer_decode(part_headers, part_body).decode('utf-8', 'replace')
        if report == 'disposition-notification' and kind == 'message/disposition-notification':
            fields = _fields(text)
            disposition = fields.get('disposition', '')
            _, _, action = disposition.partition(';')
            action = action.strip().lower()
            base, _, modifier = action.partition('/')
            original = fields.get('original-message-id')
            if not original:
                return None
            if base in ('processed', 'dispatched') and modifier.strip() not in ('error',):
                notice_kind = base
            elif base in ('failed', 'deleted', 'denied') or modifier.strip() == 'error':
                notice_kind = 'failed'
            else:
                return None
            return Notice(notice_kind, original.strip(), _address(fields.get('final-recipient')),
                          {'disposition': disposition[:200], 'final_destination': FINAL in text.lower(),
                           'reporting_ua': (fields.get('reporting-ua') or '')[:200]})
        if report == 'delivery-status' and kind == 'message/delivery-status':
            fields = _fields(text)
            action = (fields.get('action') or '').lower()
            original = None
            for other in parts[2:]:
                other_headers, other_body = smime.split_headers(other)
                if smime.media_type(other_headers.get('content-type')) in ('message/rfc822', 'text/rfc822-headers'):
                    original = _fields(other_body.decode('utf-8', 'replace').split('\r\n\r\n')[0]).get('message-id')
            if not original:
                return None
            notice_kind = 'failed' if action == 'failed' else 'delayed' if action == 'delayed' else (
                'relayed' if action in ('delivered', 'relayed', 'expanded') else None)
            if notice_kind is None:
                return None
            return Notice(notice_kind, original.strip(), _address(fields.get('final-recipient')),
                          {'action': action, 'status': (fields.get('status') or '')[:20]})
    return None


NOTICE_TEXT = {
    'processed': "The recipient's HISP accepted it; Faxbot is waiting for confirmation that it was delivered.",
    'processed_final': "Delivered to the recipient's HISP, which confirmed it.",
    'dispatched': "Delivered: the recipient's system confirmed it.",
    'failed': "Not delivered: the recipient's HISP said it could not deliver it.",
}


def record_notice(store, notice, *, delivery=None, account=None, now=None):
    """Apply one delivery notice to the sent message it names, once. Returns the message's new state or None."""
    row = store.message_by_id(notice.original_message_id, direction='out')
    if row is None or row['kind'] != 'direct':
        return None
    dedupe = f"{row['id']}:notice:{notice.kind}:{notice.final_recipient or ''}"
    details = {'kind': notice.kind, **notice.detail}
    wants_dispatched = account is None or bool(account.setting('request_delivery'))
    if notice.kind == 'processed':
        state = 'processed' if wants_dispatched else 'dispatched'
        text = NOTICE_TEXT['processed' if wants_dispatched else 'processed_final']
        moved = store.move(row['id'], state, expected=('submitted', 'uncertain'), kind='processed', dedupe=dedupe,
                           details=details, detail=text, now=now)
        fax = 'in_progress' if wants_dispatched else 'success'
    elif notice.kind == 'dispatched':
        moved = store.move(row['id'], 'dispatched', expected=('submitted', 'processed', 'uncertain'),
                           kind='dispatched', dedupe=dedupe, details=details, detail=NOTICE_TEXT['dispatched'],
                           now=now)
        fax = 'success'
    elif notice.kind == 'failed':
        moved = store.move(row['id'], 'failed', expected=('submitted', 'processed', 'uncertain'), kind='failed',
                           dedupe=dedupe, details=details, detail=NOTICE_TEXT['failed'], now=now)
        fax = 'failed'
    else:
        store.event(row['id'], notice.kind, dedupe=dedupe, details=details, now=now)
        return row['state']
    if moved and delivery is not None and row['job_id'] and row['attempt_id']:
        _observe(delivery, row, fax, f"digital:{row['id']}:{notice.kind}",
                 error=NOTICE_TEXT['failed'] if fax == 'failed' else None)
    current = store.message(row['id'])
    return current['state'] if current else None


def _observe(delivery, row, status, event_key, *, error=None):
    """Tell the fax's delivery record what the Direct message's notice said."""
    from ..outbound_store import DeliveryConflict
    try:
        _, profile = delivery.attempt_context(row['job_id'], row['attempt_id'])
        delivery.observe(row['job_id'], attempt_id=row['attempt_id'], profile_id=profile.id, provider_sid=None,
                         status=status, event_key=event_key, **({'error': error[:200]} if error else {}))
    except DeliveryConflict as conflict:
        # The fax moved on (a person settled it); the notice stays on the message as evidence.
        logger.info('A Direct notice did not change its fax: %s', conflict)


def sweep(store, *, delivery=None, accounts_for=None, now=None):
    """Messages whose notice did not arrive within their account's wait become uncertain (never failed)."""
    now = now or utcnow()
    changed = 0
    for row in store.in_states(('submitted', 'processed'), kind='direct', limit=200):
        account = accounts_for(row['account_key']) if accounts_for else None
        wait = wait_for(account) if account is not None else DEFAULT_WAIT
        since = row['updated_at'] if row['state'] == 'processed' else (row['submitted_at'] or row['updated_at'])
        if since + wait > now:
            continue
        if row['state'] == 'processed' and account is not None and not account.setting('request_delivery'):
            continue
        what = 'delivery confirmation' if row['state'] == 'processed' else 'confirmation from the recipient\'s HISP'
        sentence = (f'No {what} arrived within {int(wait.total_seconds() // 60)} minutes, so Faxbot cannot tell '
                    'whether it was delivered. It waits for you and is never sent again by itself.')
        if store.move(row['id'], 'uncertain', expected=(row['state'],), kind='timeout',
                      dedupe=f"{row['id']}:timeout:{row['state']}", details={'after': row['state']},
                      detail=sentence, now=now):
            changed += 1
            if delivery is not None and row['job_id'] and row['attempt_id']:
                from ..outbound_store import DeliveryConflict
                try:
                    _, profile = delivery.attempt_context(row['job_id'], row['attempt_id'])
                    delivery.record_unconfirmed(row['job_id'], attempt_id=row['attempt_id'], profile_id=profile.id,
                                                event_key=f"digital:{row['id']}:timeout", category='notice_missing')
                except DeliveryConflict as conflict:
                    logger.info('A Direct timeout did not change its fax: %s', conflict)
    return changed


# Receiving ----------------------------------------------------------------------------------------------------------

@dataclass
class Received:
    message_id: str
    sender: str | None
    documents: list            # [(name, media type, bytes)]
    notice: Notice | None = None
    refused: str | None = None
    wants_dispatched: bool = False
    sender_certificate: object = None
    received_at: datetime | None = None
    report: bool = False         # a delivery notice or bounce: never recorded or filed as a received message
    # The message IDs this message answers (In-Reply-To, then References), so a reply can be matched to its
    # request (work/expectations.py). What the sender stated, never proof.
    replies_to: tuple = ()


def _is_bounce(notice):
    """A delivery status report (RFC 3464) rather than a security agent's delivery notice."""
    return 'action' in notice.detail


def _documents_in(entity, depth=0):
    """[(name, media type, bytes)] of attachments, opening XDM packages; nested parts are searched."""
    if depth > 6:
        return []
    headers, body = smime.split_headers(entity)
    kind = smime.media_type(headers.get('content-type'))
    if kind.startswith('multipart/'):
        mark = smime.parameter(headers.get('content-type'), 'boundary')
        found = []
        for part in smime.split_multipart(body, mark) if mark else []:
            found += _documents_in(part, depth + 1)
        return found
    if kind == 'message/rfc822':
        return _documents_in(body, depth + 1)
    if kind.startswith('text/'):
        return []
    disposition = headers.get('content-disposition', '')
    name = smime.parameter(disposition, 'filename') or smime.parameter(headers.get('content-type'), 'name') or 'document'
    data = smime.transfer_decode(headers, body)
    if kind in ('application/zip', 'application/x-zip-compressed') or name.lower().endswith('.zip'):
        try:
            return [(item.name, item.media_type, item.data) for item in xdm.read(data)]
        except xdm.XdmError:
            return [(name, kind, data)]
    return [(name, kind, data)]


def open_message(raw, account, *, store, transport, now=None):
    """Read one message from the HISP mailbox: a notice, or documents from a trusted sender."""
    raw = smime.canonical(raw)
    headers, _ = smime.split_headers(raw)
    message_id = (headers.get('message-id') or '').strip()
    sender = (getaddresses([headers.get('from', '')]) or [('', '')])[0][1].lower() or None
    try:
        received_at = parsedate_to_datetime(headers.get('date')).astimezone(timezone.utc).replace(tzinfo=None) \
            if headers.get('date') else None
    except (TypeError, ValueError):
        received_at = None
    if not message_id:
        message_id = '<sha256.' + hashlib.sha256(raw).hexdigest() + '@faxbot.invalid>'
    entity = raw
    signer = None
    kind = smime.media_type(headers.get('content-type'))
    if account.setting('security') == 'faxbot':
        if kind not in ('application/pkcs7-mime', 'application/x-pkcs7-mime'):
            report = read_notice(raw)
            if report is not None:
                # An unencrypted report: a mail server's bounce may count (``Receiver.handle`` decides); an
                # unencrypted delivery notice from a security agent is never trusted. Neither is a received message.
                return Received(message_id, sender, [], notice=report if _is_bounce(report) else None,
                                received_at=received_at, report=True)
            return Received(message_id, sender, [], refused='It was not encrypted, so Faxbot did not trust it.',
                            received_at=received_at)
        own, _, key = own_identity(account)
        try:
            opened = smime.decrypt(smime.transfer_decode(headers, raw.partition(CRLF + CRLF)[2]), own, key)
            content, signature, opaque = smime.unwrap_signed(opened)
            signed = smime.verify_detached(content, signature, opaque=opaque)
        except smime.SmimeError as error:
            return Received(message_id, sender, [], refused=str(error), received_at=received_at)
        try:
            certificates.check(signed.certificate, anchors=anchors_for(store, account),
                               intermediates=signed.certificates, address=sender, purpose='sign',
                               crl_fetch=transport.fetch)
        except certificates.CertificateRefused as refusal:
            return Received(message_id, sender, [], refused=f'The sender is not trusted: {refusal}',
                            received_at=received_at)
        signer = signed.certificate
        entity = signed.content
        inner_headers, inner_body = smime.split_headers(entity)
        if smime.media_type(inner_headers.get('content-type')) == 'message/rfc822':
            entity = inner_body
            wrapped_headers, _ = smime.split_headers(entity)
            headers = {**headers, **wrapped_headers}
    notice = read_notice(entity)
    if notice is not None:
        return Received(message_id, sender, [], notice=notice, sender_certificate=signer, received_at=received_at,
                        report=True)
    options = (headers.get('disposition-notification-options') or '').lower()
    wants = bool(headers.get('disposition-notification-to')) and FINAL in options
    try:
        documents = _documents_in(entity)
    except smime.SmimeError as error:
        return Received(message_id, sender, [], refused=str(error), received_at=received_at)
    return Received(message_id, sender, documents, wants_dispatched=wants, sender_certificate=signer,
                    received_at=received_at, replies_to=replies_to(headers))


def replies_to(headers):
    """The message IDs a message names in In-Reply-To and References (RFC 5322 3.6.4), at most 20, in order."""
    found = []
    for name in ('in-reply-to', 'references'):
        for value in re.findall(r'<[^<>\s]{1,510}>', headers.get(name) or ''):
            if value not in found:
                found.append(value)
    return tuple(found[:20])


def notice_message(*, account, original_message_id, recipient, disposition, now=None):
    """(headers, body) of an MDN (RFC 3798) from this account about a message it received."""
    now = now or datetime.now(timezone.utc)
    own_address = account.setting('direct_address')
    mark = smime.boundary('Report')
    fields = ['Reporting-UA: faxbot; Faxbot', f'Original-Recipient: rfc822;{own_address}',
              f'Final-Recipient: rfc822;{own_address}', f'Original-Message-ID: {original_message_id}',
              f'Disposition: automatic-action/MDN-sent-automatically; {disposition}']
    if disposition == 'dispatched':
        fields.append(f'{FINAL.upper()}: true')
    text = {'processed': 'Your message was received and its security checked.',
            'dispatched': 'Your message was delivered to its destination.',
            'failed': 'Your message could not be delivered to its destination.'}[disposition]
    body = CRLF.join([
        f'Content-Type: multipart/report; report-type=disposition-notification; boundary="{mark}"'.encode('ascii'),
        b'', f'--{mark}'.encode('ascii'), b'Content-Type: text/plain; charset=utf-8', b'', text.encode('ascii'),
        f'--{mark}'.encode('ascii'), b'Content-Type: message/disposition-notification', b'',
        CRLF.join(line.encode('utf-8') for line in fields), f'--{mark}--'.encode('ascii'), b''])
    headers = [_header('From', own_address), _header('To', recipient),
               _header('Subject', f'Delivery notice: {disposition}'), _header('Date', format_datetime(now)),
               _header('Message-ID', message_id_for('notice', own_address)), b'MIME-Version: 1.0']
    return headers, body


class Receiver:
    """Reads one HISP account's mailbox: settles sent messages and files received documents, once each."""

    def __init__(self, store, account, *, transport=None, file_document=None, delivery=None, mailbox_factory=None,
                 left=None):
        self.store, self.account = store, account
        self.transport = transport or Transport()
        self.file_document = file_document      # (account, received, index, name, media type, bytes) -> outcome
        self.delivery = delivery
        self.mailbox_factory = mailbox_factory  # () -> intake/sources/imap.Mailbox-like (tests: a fake HISP)
        self.last_problem = None
        # UIDs of messages left in the mailbox while receiving is off, so newer notices are still reached.
        self.left = left if left is not None else set()

    def _mailbox(self):
        if self.mailbox_factory is not None:
            return self.mailbox_factory()
        from ..intake.sources.imap import Mailbox
        account = self.account
        mailbox = Mailbox(account.setting('imap_host'), int(account.setting('imap_port') or 993),
                          connect=self.transport.imap_connect, ssl_context=self.transport.ssl_context)
        mailbox.sign_in_password(account.setting('username') or account.setting('direct_address'),
                                 account.secret('password') or '')
        return mailbox

    def check(self, *, limit=25, now=None):
        """Read up to ``limit`` waiting messages; returns how many were handled."""
        from ..intake.sources.imap import MailboxError, TooLarge
        account = self.account
        try:
            mailbox = self._mailbox()
        except MailboxError as error:
            self.last_problem = str(error)
            return 0
        handled = 0
        try:
            mailbox.select(account.setting('imap_folder') or 'INBOX')
            done_folder = account.setting('processed_folder') or 'Faxbot filed'
            mailbox.ensure_folder(done_folder)
            waiting = [uid for uid in mailbox.waiting(limit + len(self.left)) if uid not in self.left][:limit]
            for uid in waiting:
                try:
                    raw, _ = mailbox.fetch(uid, MAX_MESSAGE)
                except TooLarge:
                    if not account.receives:
                        self.left.add(uid)
                        continue
                    head, _ = mailbox.header(uid)
                    self.too_large(head, now=now)
                else:
                    if self.handle(raw, now=now) == 'left':
                        self.left.add(uid)
                        continue
                mailbox.done(uid, done_folder)
                handled += 1
            self.last_problem = None
        except MailboxError as error:
            self.last_problem = str(error)
        finally:
            mailbox.close()
        return handled

    def handle(self, raw, *, now=None):
        """Record and act on one message; safe to repeat (it is keyed by its Message-ID)."""
        store, account = self.store, self.account
        received = open_message(raw, account, store=store, transport=self.transport, now=now)
        if received.report:
            notice = received.notice
            sent = store.message_by_id(notice.original_message_id, direction='out') if notice else None
            if sent is None:
                return 'notice'
            if _is_bounce(notice):
                # A mail server's bounce comes from its own address, so it counts only as a failure of a message still
                # waiting for its recipient's first notice, matched by Faxbot's own unguessable Message-ID.
                if notice.kind == 'failed' and sent['state'] in ('submitted', 'uncertain'):
                    record_notice(store, notice, delivery=self.delivery, account=account, now=now)
            elif _same_party(received.sender, sent['counterpart']):
                # A delivery notice must come from the address the message went to (or its domain).
                record_notice(store, notice, delivery=self.delivery, account=account, now=now)
            return 'notice'
        if not account.receives:
            # Receiving is off: only notices are read; every other message stays in the mailbox as it is.
            return 'left'
        pdfs = [item for item in received.documents if item[1] in ('application/pdf', 'image/tiff')
                or item[0].lower().endswith(('.pdf', '.tif', '.tiff'))]
        if received.refused:
            state, detail = 'not_filed', received.refused[:300]
        elif not pdfs:
            state, detail = 'not_filed', ('It carried no PDF or TIFF document, so there was nothing to file.'
                                          if received.documents else 'It carried no document.')
        else:
            state, detail = 'filed', None
        try:
            row, created = store.begin_message(
                direction='in', kind='direct', account_key=account.key, message_id=received.message_id,
                counterpart=received.sender or 'unknown', state='filed' if state == 'filed' else 'not_filed',
                security=account.setting('security'), detail=detail,
                certificate_sha256=(certificates.fingerprint(received.sender_certificate)
                                    if received.sender_certificate is not None else None), now=now)
        except DigitalInputError:
            return 'duplicate'
        if state == 'filed' and self.file_document is not None:
            # Filing is keyed by the Message-ID and the document's place in it, so a repeat files nothing twice.
            for index, (name, media_type, data) in enumerate(pdfs, start=1):
                outcome = self.file_document(account, received, index, name, media_type, data)
                store.event(row['id'], 'filed', dedupe=f"{row['id']}:filed:{index}",
                            details={'document': index, 'name': name[:120], **(outcome or {})}, now=now)
        if account.setting('security') == 'faxbot' and received.sender and not received.refused:
            self._notices(row, received, filed_ok=state == 'filed', now=now)
        if not created:
            return 'duplicate'
        return 'filed' if state == 'filed' else 'not_filed'

    def too_large(self, head, *, now=None):
        """A message over the size limit is recorded as not filed, with its sender and Message-ID, and moved on."""
        headers, _ = smime.split_headers(smime.canonical(head) + CRLF)
        message_id = (headers.get('message-id') or '').strip() or (
            '<sha256.' + hashlib.sha256(head).hexdigest() + '@faxbot.invalid>')
        sender = (getaddresses([headers.get('from', '')]) or [('', '')])[0][1].lower() or 'unknown'
        try:
            self.store.begin_message(direction='in', kind='direct', account_key=self.account.key,
                                     message_id=message_id, counterpart=sender, state='not_filed',
                                     security=self.account.setting('security'), now=now,
                                     detail=f'It was larger than {MAX_MESSAGE // (1024 * 1024)} MB, so Faxbot did not '
                                            'download it. Ask the sender to send it in smaller parts.')
        except DigitalInputError:
            # Another worker recorded it at the same moment.
            pass

    def _notices(self, row, received, *, filed_ok, now=None):
        """As the receiving security agent: processed once, then dispatched (or failed) when asked for."""
        sends = [('processed', 'mdn_processed')]
        if received.wants_dispatched:
            sends.append(('dispatched' if filed_ok else 'failed', 'mdn_final'))
        for disposition, kind in sends:
            if any(event['kind'] == kind for event in self.store.events_for(row['id'])):
                continue
            try:
                self._send_notice(received, disposition)
            except (DirectRefusedError, DirectFailure, certificates.CertificateRefused) as error:
                self.store.event(row['id'], 'mdn_unsent', dedupe=f"{row['id']}:{kind}:unsent:{secrets.token_hex(4)}",
                                 details={'disposition': disposition, 'why': str(error)[:200]}, now=now)
                return
            self.store.event(row['id'], kind, dedupe=f"{row['id']}:{kind}", details={'disposition': disposition},
                             now=now)

    def _send_notice(self, received, disposition):
        account = self.account
        headers, body = notice_message(account=account, original_message_id=received.message_id,
                                       recipient=received.sender, disposition=disposition)
        # The sender's published encryption certificate; its signing certificate only when that one may encrypt.
        found = self.transport.certificates_for(received.sender, anchors=anchors_for(self.store, account))
        recipients = list(found.certificates)
        signer = received.sender_certificate
        if not recipients and signer is not None and certificates.usable_for_encryption(signer):
            recipients = [signer]
        if not recipients:
            raise DirectRefusedError('There is no certificate to answer the sender with.')
        own, chain, key = own_identity(account)
        message = secure_message(headers, body, own_certificate=own, own_chain=chain, own_key=key,
                                 recipients=recipients)
        submit_smtp(self.transport, account, sender=account.setting('direct_address'), recipient=received.sender,
                    message=message)


def _same_party(sender, counterpart):
    if not sender or not counterpart:
        return False
    return sender == counterpart or sender.rpartition('@')[2] == counterpart.rpartition('@')[2]
