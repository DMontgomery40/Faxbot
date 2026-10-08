"""The one email reply an email-to-fax sender gets: refused, sent, not sent, or uncertain with what to do.

Replies go from the connector's own mailbox address through its outgoing mail
server, signed in the same way as the mailbox (password or XOAUTH2). Each
carries ``Auto-Submitted: auto-replied`` (RFC 3834) and a Message-ID fixed per
item, and answers the original message. SMTP steps before the message data are
definite failures; a lost connection after the data was sent is uncertain, and
an uncertain reply is never sent again.
"""
from email.message import EmailMessage
from email.utils import formatdate
import smtplib
import ssl

from ...intake.email import AmbiguousFailure, DefiniteFailure
from .oauth import xoauth2
from . import text


def reply_id(item_id, address):
    domain = address.rpartition('@')[2] or 'faxbot.invalid'
    return f'<faxbot-reply-{item_id}@{domain}>'


def _number(value):
    return value or 'the number you gave'


def compose(source, item, *, outcome=None):
    """The reply for an item; ``outcome`` is the sent fax's outcome for a result reply."""
    settings = source.settings
    number = _number(item.get('to_number'))
    if item['reply_kind'] == 'refused' or outcome is None:
        subject = text.REPLY_SUBJECT_REFUSED.format(subject=item.get('subject') or 'your message')
        reason = item.get('reply_note') or item.get('reason') or ''
        if reason == text.REPLY_NOT_LISTED:
            reason = text.REPLY_NOT_LISTED.format(address=settings.get('address', ''))
        body = text.REPLY_REFUSED.format(reason=_plain(reason))
    elif outcome['state'] == 'success':
        subject = text.REPLY_SUBJECT_SENT.format(number=number)
        body = text.REPLY_SENT.format(number=number, pages=text.pages_phrase(outcome.get('pages')))
    elif outcome['state'] in ('failed', 'cancelled'):
        subject = text.REPLY_SUBJECT_FAILED.format(number=number)
        detail = _plain(outcome.get('error') or 'The fax could not be delivered.')
        body = text.REPLY_FAILED.format(number=number, detail=detail)
    else:
        subject = text.REPLY_SUBJECT_UNCERTAIN.format(number=number)
        body = text.REPLY_UNCERTAIN.format(number=number)
    message = EmailMessage()
    message['From'] = settings['address']
    message['To'] = item['reply_to']
    message['Subject'] = ' '.join(subject.split())[:200]
    message['Date'] = formatdate(usegmt=True)
    message['Message-ID'] = reply_id(item['id'], settings['address'])
    message['Auto-Submitted'] = 'auto-replied'
    if item.get('reference'):
        message['In-Reply-To'] = item['reference']
        message['References'] = item['reference']
    message.set_content(body + '\n')
    return message


def _plain(sentence):
    """A stored reason without the administrator's prefix ("Not sent: ...")."""
    sentence = (sentence or '').strip()
    for prefix in ('Not sent: ', 'Not filed: '):
        if sentence.startswith(prefix):
            sentence = sentence[len(prefix)].upper() + sentence[len(prefix) + 1:]
    return sentence


def needs_result(state):
    return state in ('success', 'failed', 'cancelled', 'reconciliation_required')


def _temporary(code):
    return isinstance(code, int) and 400 <= code < 500


def send(settings, secret, message, *, token=None, timeout=30.0, context=None, connect=None):
    """Deliver one reply or raise DefiniteFailure/AmbiguousFailure; ``token`` signs in with XOAUTH2."""
    context = context or ssl.create_default_context()
    host, port = settings['smtp_host'], settings['smtp_port']
    try:
        if connect is not None:
            client = connect(host, port, timeout)
        elif settings['smtp_security'] == 'tls':
            client = smtplib.SMTP_SSL(host, port, timeout=timeout, context=context)
        else:
            client = smtplib.SMTP(host, port, timeout=timeout)
    except (OSError, smtplib.SMTPException):
        raise DefiniteFailure('Faxbot could not reach the outgoing mail server.', temporary=True) from None
    try:
        try:
            client.ehlo()
            if settings['smtp_security'] == 'starttls' and connect is None:
                client.starttls(context=context)
                client.ehlo()
            if token is not None:
                client.auth('XOAUTH2', lambda challenge=None: xoauth2(settings['username'], token)
                            if challenge is None else '')
            else:
                client.login(settings['username'], secret.get('password', ''))
        except smtplib.SMTPAuthenticationError:
            raise DefiniteFailure('The outgoing mail server did not accept the sign-in.', temporary=False) from None
        except (OSError, smtplib.SMTPException):
            raise DefiniteFailure('The outgoing mail server closed the connection before the reply was sent.',
                                  temporary=True) from None
        try:
            code, _ = client.mail(settings['address'])
            if code != 250:
                raise DefiniteFailure('The outgoing mail server refused the sender address.',
                                      temporary=_temporary(code))
            code, _ = client.rcpt(message['To'])
            if code not in (250, 251):
                raise DefiniteFailure('The outgoing mail server refused the reply address.',
                                      temporary=_temporary(code))
        except DefiniteFailure:
            raise
        except (OSError, smtplib.SMTPException):
            raise DefiniteFailure('The outgoing mail server closed the connection before the reply was sent.',
                                  temporary=True) from None
        payload = message.as_bytes(policy=message.policy.clone(linesep='\r\n'))
        try:
            code, _ = client.data(payload)
        except smtplib.SMTPDataError as error:
            raise DefiniteFailure('The outgoing mail server refused the reply.',
                                  temporary=_temporary(error.smtp_code)) from None
        except (OSError, smtplib.SMTPException):
            raise AmbiguousFailure('The outgoing mail server stopped answering after the reply was sent.') from None
        if code != 250:
            raise DefiniteFailure('The outgoing mail server refused the reply.', temporary=_temporary(code))
    finally:
        try:
            client.quit()
        except (OSError, smtplib.SMTPException):
            client.close()

