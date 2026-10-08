"""One IMAP session over TLS (imaplib): sign in, list unprocessed messages, fetch one, move it when done.

Faxbot connects with TLS only (IMAP4_SSL, the system's trusted certificates).
Messages are read with BODY.PEEK[], which leaves them unread, and a processed
message is moved to the processed folder (RFC 6851 MOVE, or COPY, a \\Deleted
flag and EXPUNGE where MOVE is missing). Faxbot moves a message only after
what it held was recorded, so a crash in between re-reads it, and the
connector finds the same identity already recorded.
"""
from datetime import datetime, timezone
import imaplib
import re
import socket
import ssl
import time

from .oauth import xoauth2


class MailboxError(RuntimeError):
    """One plain sentence for the connector's status line."""


class CannotReach(MailboxError):
    pass


class SignInRefused(MailboxError):
    pass


class NoFolder(MailboxError):
    pass


class MoveFailed(MailboxError):
    pass


class TooLarge(MailboxError):
    def __init__(self, size):
        super().__init__('The message is too large.')
        self.size = size


_SIZE = re.compile(rb'RFC822\.SIZE (\d+)')
_UIDS = re.compile(rb'\d+')


def _quote(name):
    return '"' + name.replace('\\', '\\\\').replace('"', '\\"') + '"'


class XOAuth2:
    """The SASL exchange imaplib drives: the initial response once, then an empty answer to an error challenge."""

    def __init__(self, user, token):
        self.response, self.calls = xoauth2(user, token), 0

    def __call__(self, challenge):
        self.calls += 1
        return self.response if self.calls == 1 else b''


class Mailbox:
    """``connect(host, port, ssl_context, timeout)`` returns an imaplib client; tests pass a stand-in."""

    def __init__(self, host, port, *, connect=None, ssl_context=None, timeout=30.0):
        self.host = host
        try:
            self.client = (connect or _connect)(host, port, ssl_context or ssl.create_default_context(), timeout)
        except (OSError, imaplib.IMAP4.error, ssl.SSLError):
            raise CannotReach(f'Faxbot could not reach the mail server {host}.') from None
        self.capabilities = {value.upper() for value in getattr(self.client, 'capabilities', ())}

    def _ok(self, result, error=MailboxError, message='The mail server stopped answering during the check.'):
        status, data = result
        if status != 'OK':
            raise error(message)
        return data

    def sign_in_password(self, user, password):
        try:
            self.client.login(user, password)
        except imaplib.IMAP4.error:
            raise SignInRefused('The mail server did not accept the user name and password.') from None
        except OSError:
            raise CannotReach(f'Faxbot could not reach the mail server {self.host}.') from None

    def sign_in_token(self, user, token):
        try:
            self.client.authenticate('XOAUTH2', XOAuth2(user, token))
        except imaplib.IMAP4.error:
            raise SignInRefused('The mail server did not accept the sign-in token.') from None
        except OSError:
            raise CannotReach(f'Faxbot could not reach the mail server {self.host}.') from None

    def select(self, folder):
        try:
            status, data = self.client.select(_quote(folder))
        except (imaplib.IMAP4.error, OSError):
            raise MailboxError('The mail server stopped answering during the check.') from None
        if status != 'OK':
            raise NoFolder(f'The mailbox has no folder named {folder}.')
        try:
            return int(data[0])
        except (TypeError, ValueError, IndexError):
            return 0

    def ensure_folder(self, folder):
        """Create the processed folder when it is missing (an existing folder answers NO, which is fine)."""
        try:
            status, data = self.client.list('', _quote(folder))
            if status == 'OK' and any(item for item in data if item):
                return
            self.client.create(_quote(folder))
        except (imaplib.IMAP4.error, OSError):
            raise MailboxError('The mail server stopped answering during the check.') from None

    def waiting(self, limit):
        """UIDs of the messages in the selected folder, oldest first, at most ``limit``."""
        try:
            data = self._ok(self.client.uid('SEARCH', None, 'ALL'))
        except (imaplib.IMAP4.error, OSError):
            raise MailboxError('The mail server stopped answering during the check.') from None
        uids = [int(value) for value in _UIDS.findall(b' '.join(item for item in data if item))]
        return sorted(uids)[:limit]

    def fetch(self, uid, max_bytes):
        """(raw message, received time as naive UTC) for one UID; raises TooLarge before downloading."""
        try:
            data = self._ok(self.client.uid('FETCH', str(uid), '(RFC822.SIZE)'))
            sizes = [int(match.group(1)) for item in data if isinstance(item, bytes)
                     for match in [_SIZE.search(item)] if match]
            if sizes and sizes[0] > max_bytes:
                raise TooLarge(sizes[0])
            data = self._ok(self.client.uid('FETCH', str(uid), '(INTERNALDATE BODY.PEEK[])'))
        except (imaplib.IMAP4.error, OSError):
            raise MailboxError('The mail server stopped answering during the check.') from None
        for item in data:
            if isinstance(item, tuple) and len(item) == 2:
                header, body = item
                return body, _internal_date(header)
        raise MailboxError('The mail server stopped answering during the check.')

    def header(self, uid):
        """The message's header only (for a message too large to download)."""
        try:
            data = self._ok(self.client.uid('FETCH', str(uid), '(INTERNALDATE BODY.PEEK[HEADER])'))
        except (imaplib.IMAP4.error, OSError):
            raise MailboxError('The mail server stopped answering during the check.') from None
        for item in data:
            if isinstance(item, tuple) and len(item) == 2:
                return item[1], _internal_date(item[0])
        raise MailboxError('The mail server stopped answering during the check.')

    def done(self, uid, folder):
        """Move one message to the processed folder."""
        try:
            if 'MOVE' in self.capabilities:
                status, _ = self.client.uid('MOVE', str(uid), _quote(folder))
                if status == 'OK':
                    return
            status, _ = self.client.uid('COPY', str(uid), _quote(folder))
            if status != 'OK':
                raise MoveFailed(f'The mail server would not move processed messages to {folder}.')
            self._ok(self.client.uid('STORE', str(uid), '+FLAGS.SILENT', r'(\Deleted)'), MoveFailed,
                     f'The mail server would not move processed messages to {folder}.')
            if 'UIDPLUS' in self.capabilities:
                self.client.uid('EXPUNGE', str(uid))
            else:
                self.client.expunge()
        except (imaplib.IMAP4.error, OSError):
            raise MoveFailed(f'The mail server would not move processed messages to {folder}.') from None

    def close(self):
        try:
            self.client.logout()
        except Exception:
            try:
                self.client.shutdown()
            except Exception:
                pass


def _connect(host, port, context, timeout):
    return imaplib.IMAP4_SSL(host, port, ssl_context=context, timeout=timeout)


def _internal_date(header):
    moment = imaplib.Internaldate2tuple(header if isinstance(header, bytes) else b'')
    if moment is None:
        return None
    return datetime.fromtimestamp(time.mktime(moment), timezone.utc).replace(tzinfo=None)


def reachable(host, port, timeout=5.0):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False
