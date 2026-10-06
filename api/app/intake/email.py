"""Build and send one intake email with the original PDF attached.

SMTP steps before the message data are definite: a refusal there means nothing
was delivered. Once the message data has been sent, a lost connection is
ambiguous, so the caller must not resend automatically.
"""
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate
import smtplib
import ssl

from .. import people_time


class DefiniteFailure(RuntimeError):
    """Nothing was delivered. ``temporary`` failures may be retried."""

    def __init__(self, message, *, temporary):
        super().__init__(message)
        self.temporary = temporary


class AmbiguousFailure(RuntimeError):
    """The message may have been delivered."""


@dataclass(frozen=True)
class Delivery:
    reference: str
    accepted: tuple


def _number(value):
    return value or 'an unknown number'


def describe(item, time_zone=''):
    """The email body's sentence; the received time is in the installation's time zone (UTC when unset)."""
    received = people_time.date_and_time(item['received_at'], time_zone)
    pages = item.get('pages')
    count = '' if not pages else (', 1 page' if pages == 1 else f', {pages} pages')
    kind = 'Direct delivery' if item.get('source') == 'direct' else 'Fax'
    return f"{kind} from {_number(item.get('from_number'))} to {_number(item.get('to_number'))}{count}, received {received}."


def subject_for(template, item, time_zone=''):
    received = people_time.date_and_time(item['received_at'], time_zone)
    return template.format(from_number=_number(item.get('from_number')), to_number=_number(item.get('to_number')),
                           pages=item.get('pages') or '', received_at=received)[:200]


def message_id(item_id, sender):
    """Stable per item, so a person's resend can be recognized as the same message."""
    domain = sender.rpartition('@')[2] or 'faxbot.invalid'
    return f'<intake-{item_id}@{domain}>'


def build_message(settings, item, document, *, filename, time_zone=''):
    message = EmailMessage()
    message['From'] = settings.from_address
    message['To'] = ', '.join(settings.recipients)
    message['Subject'] = subject_for(settings.subject_template, item, time_zone)
    message['Date'] = formatdate(usegmt=True)
    message['Message-ID'] = message_id(item['id'], settings.from_address)
    message.set_content(describe(item, time_zone) + '\n')
    message.add_attachment(document, maintype='application', subtype='pdf', filename=filename)
    return message


def test_message(settings, connector_id):
    message = EmailMessage()
    message['From'] = settings.from_address
    message['To'] = ', '.join(settings.recipients)
    message['Subject'] = 'Faxbot test email'
    message['Date'] = formatdate(usegmt=True)
    message['Message-ID'] = f"<intake-test-{connector_id}@{settings.from_address.rpartition('@')[2]}>"
    message.set_content('This is a test from Faxbot. Received faxes will be delivered to this address.\n')
    return message


def _temporary(code):
    return isinstance(code, int) and 400 <= code < 500


def send(settings, password, message, *, timeout=30.0, context=None):
    """Deliver ``message`` to every recipient or raise; returns the Message-ID used."""
    context = context or ssl.create_default_context()
    try:
        if settings.security == 'tls':
            client = smtplib.SMTP_SSL(settings.host, settings.port, timeout=timeout, context=context)
        else:
            client = smtplib.SMTP(settings.host, settings.port, timeout=timeout)
    except (OSError, smtplib.SMTPException):
        raise DefiniteFailure('Faxbot could not reach the email server.', temporary=True) from None
    try:
        try:
            client.ehlo()
            if settings.security == 'starttls':
                client.starttls(context=context)
                client.ehlo()
            if settings.username:
                client.login(settings.username, password)
        except smtplib.SMTPAuthenticationError:
            raise DefiniteFailure('The email server did not accept the user name or password.', temporary=False) from None
        except smtplib.SMTPNotSupportedError:
            raise DefiniteFailure('The email server does not support the chosen security setting.', temporary=False) from None
        except (OSError, smtplib.SMTPException):
            raise DefiniteFailure('The email server closed the connection before the fax was sent.', temporary=True) from None
        try:
            code, _ = client.mail(settings.from_address)
            if code != 250:
                raise DefiniteFailure('The email server refused the sender address.', temporary=_temporary(code))
            refused = []
            for recipient in settings.recipients:
                code, _ = client.rcpt(recipient)
                if code not in (250, 251):
                    refused.append(code)
            if refused:
                raise DefiniteFailure('The email server refused a recipient address.',
                                      temporary=all(_temporary(code) for code in refused))
        except DefiniteFailure:
            raise
        except (OSError, smtplib.SMTPException):
            raise DefiniteFailure('The email server closed the connection before the fax was sent.', temporary=True) from None
        # SMTP needs CRLF line endings; smtplib does not convert a bytes payload.
        payload = message.as_bytes(policy=message.policy.clone(linesep='\r\n'))
        try:
            code, _ = client.data(payload)
        except smtplib.SMTPDataError as error:
            raise DefiniteFailure('The email server refused the message.', temporary=_temporary(error.smtp_code)) from None
        except (OSError, smtplib.SMTPException):
            raise AmbiguousFailure('The email server stopped responding after the fax was sent.') from None
        # smtplib returns, rather than raises, the server's final answer to the message data.
        if code != 250:
            raise DefiniteFailure('The email server refused the message.', temporary=_temporary(code))
        return Delivery(str(message['Message-ID']), tuple(settings.recipients))
    finally:
        try:
            client.quit()
        except (OSError, smtplib.SMTPException):
            client.close()
