"""Check each connector when it is due, then send any replies that are due.

One check holds a lease on its connector, so two Faxbot processes never check
the same mailbox or folder at once. A message or file is moved out of the way
only after what became of it was recorded. Anything Faxbot cannot decide now
(the mail server stopped answering, the fax engine is not connected, the
sending key was revoked) leaves the message or file where it is for the next
check, and the connector's status line says why.
"""
from datetime import timedelta
import hashlib
import logging
import os

from ...routing.numbers import is_canonical
from ...work.imports import ImportInputError, parse_sidecar
from . import folder as folders, mail, receive, replies, send, text
from .imap import CannotReach, Mailbox, MailboxError, MoveFailed, NoFolder, SignInRefused, TooLarge
from .oauth import TokenUnavailable, Tokens


LOG = logging.getLogger(__name__)
PER_CHECK = 20
RESULT_WAIT = timedelta(days=7)


class Poller:
    """``access()`` returns the access runtime; ``runtime`` is the configuration runtime."""

    def __init__(self, store, access, runtime, *, tokens=None, connect=None, ssl_context=None, smtp_connect=None,
                 watch=None, own_addresses=None):
        self.store, self.access, self.runtime = store, access, runtime
        self.tokens = tokens or Tokens()
        self.connect, self.ssl_context, self.smtp_connect = connect, ssl_context, smtp_connect
        self.watch = watch or folders.Watch()
        self.own_addresses = own_addresses or (lambda: ())

    def values(self):
        return self.runtime.manager.store.read().active.values

    # -- the loop ----------------------------------------------------------------------
    def step(self):
        busy = False
        claimed = self.store.claim()
        if claimed is not None:
            source, lease = claimed
            self.run(source, lease)
            busy = True
        return self.reply_step() or busy

    def run(self, source, lease):
        try:
            ok, result = self.check(source)
        except Exception:
            LOG.warning('A connector check stopped unexpectedly.')
            ok, result = False, text.CHECK_STOPPED
        self.store.finish(source, lease, ok=ok, result=result)
        return ok, result

    def check(self, source):
        if source.kind == 'folder':
            return self.check_folder(source)
        return self.check_mail(source)

    # -- email ---------------------------------------------------------------------------
    def sign_in(self, source):
        settings = source.settings
        try:
            secret = self.store.secret(source)
        except Exception:
            raise MailboxError(text.SECRET_UNREADABLE) from None
        mailbox = Mailbox(settings['imap_host'], settings['imap_port'], connect=self.connect,
                          ssl_context=self.ssl_context)
        try:
            if settings['sign_in'] == 'password':
                mailbox.sign_in_password(settings['username'], secret.get('password', ''))
            else:
                try:
                    token = self.tokens.token(source.id, settings, secret)
                except TokenUnavailable as error:
                    raise MailboxError(text.TOKEN_UNAVAILABLE.format(detail=str(error))) from None
                try:
                    mailbox.sign_in_token(settings['username'], token)
                except SignInRefused:
                    self.tokens.forget(source.id)
                    raise
        except BaseException:
            mailbox.close()
            raise
        return mailbox, secret

    def test(self, source):
        """Sign in and look, without changing anything; returns (ok, sentence)."""
        if source.kind == 'folder':
            try:
                folders.check(source.settings['path'])
                count = len(folders.documents(source.settings['path']))
            except folders.FolderError as error:
                return False, self._folder_problem(source, error)
            return True, text.TEST_OK_FOLDER.format(path=source.settings['path'], count=_count(count, 'file'))
        try:
            mailbox, _ = self.sign_in(source)
        except MailboxError as error:
            return False, str(error)
        try:
            count = mailbox.select(source.settings['folder'])
        except MailboxError as error:
            return False, str(error)
        finally:
            mailbox.close()
        return True, text.TEST_OK_MAIL.format(host=source.settings['imap_host'], folder=source.settings['folder'],
                                              count=_count(count, 'message'))

    def check_mail(self, source):
        settings = source.settings
        try:
            mailbox, secret = self.sign_in(source)
        except MailboxError as error:
            return False, str(error)
        handled, problem = 0, None
        try:
            mailbox.ensure_folder(settings['processed_folder'])
            mailbox.select(settings['folder'])
            limit = int(self.values().max_file_size_mb) * 1024 * 1024
            for uid in mailbox.waiting(PER_CHECK):
                try:
                    raw, received_at = mailbox.fetch(uid, limit * 4 // 3 + 65536)
                    too_large = False
                except TooLarge:
                    raw, received_at = mailbox.header(uid)
                    too_large = True
                message = mail.parse(raw)
                try:
                    if source.direction == 'receive':
                        self.receive_message(source, message, received_at, too_large)
                    else:
                        self.send_message(source, message, received_at, too_large, secret)
                except send.Retry as wait:
                    problem = str(wait)
                    break
                handled += 1
                try:
                    mailbox.done(uid, settings['processed_folder'])
                except MoveFailed:
                    problem = text.MOVE_FAILED.format(folder=settings['processed_folder'])
                    break
        except (CannotReach, NoFolder, MailboxError) as error:
            problem = str(error)
        finally:
            mailbox.close()
        if problem:
            return False, problem
        return True, text.CHECKED.format(count=_count(handled, 'message')) if handled else text.CHECKED_NOTHING

    def _record_message(self, source, message, *, state, reason, received_at, sender=None, reply_to=None,
                        reply_note=None):
        reply = bool(reply_to)
        item, created = self.store.record(
            source.id, source.direction, message.operation_id, '', state=state, reason=reason,
            reference=message.reference, subject=message.subject, sender=sender, source_received_at=received_at,
            reply_to=reply_to if reply else None, reply_state='due' if reply else 'none',
            reply_kind='refused' if reply else None, reply_note=reply_note)
        if not created:
            self.store.seen_again(item)
        return item

    def receive_message(self, source, message, received_at, too_large):
        values = self.values()
        sender = mail.sender(message)
        # Only Faxbot's own Message-IDs: a scanner often mails scans from the very mailbox it sends to.
        if mail.sent_by_faxbot(message):
            return self._record_message(source, message, state='refused', reason=text.OWN_MAIL,
                                        received_at=received_at, sender=sender)
        if too_large:
            return self._record_message(source, message, state='failed', received_at=received_at, sender=sender,
                                        reason=text.TOO_LARGE_RECEIVE.format(limit=values.max_file_size_mb))
        if not message.documents:
            return self._record_message(source, message, state='failed', reason=text.NO_DOCUMENT_RECEIVE,
                                        received_at=received_at, sender=sender)
        destination, label, problem = self.destination(source)
        sidecars = {sidecar.stem: sidecar for sidecar in message.sidecars}
        shared = message.sidecars[0] if len(message.sidecars) == 1 else None
        for document in message.documents:
            details = {}
            sidecar = sidecars.get(document.stem) or shared
            if sidecar is not None:
                try:
                    details = parse_sidecar(sidecar.data, country=values.fax_default_country)
                except ImportInputError as error:
                    self.store.record(source.id, 'receive', message.operation_id, str(document.position),
                                      state='failed', reason=text.BAD_SIDECAR.format(detail=str(error)),
                                      reference=message.reference, subject=document.name, sender=sender,
                                      document_digest=document.digest)
                    continue
            number = details.get('to_number')
            arrival = receive.Arrival(
                operation_id=message.operation_id, part=str(document.position), name=document.name,
                kind=document.kind, data=document.data, digest=document.digest, to_number=number,
                from_number=details.get('from_number'), received_at=received_at, reference=message.reference,
                subject=document.name, sender=sender, pages=details.get('pages'),
                mailbox_label=None if number else label, mailbox_id=None if number else destination,
                report={'subject': message.subject, 'sidecar_operation_id': details.get('operation_id')})
            if number is None and problem:
                self.store.record(source.id, 'receive', message.operation_id, str(document.position),
                                  state='failed', reason=problem, reference=message.reference, subject=document.name,
                                  sender=sender, document_digest=document.digest)
                continue
            receive.file_document(self.access(), values, self.store, source, arrival)

    def destination(self, source):
        """(mailbox ID, mailbox label, reason when there is none) for documents with no sidecar number: they are
        filed straight into the connector's mailbox, whether or not it has a fax number."""
        mailbox_id = source.settings.get('mailbox_id')
        if not mailbox_id:
            return None, None, text.NO_MAILBOX
        box = self.store.mailbox(mailbox_id)
        if box is None:
            return None, None, text.MAILBOX_GONE
        return box['id'], box['label'], None

    def send_message(self, source, message, received_at, too_large, secret):
        settings, values = source.settings, self.values()
        address = mail.sender(message)
        record = lambda reason, state='refused', reply_to=None, note=None: self._record_message(  # noqa: E731
            source, message, state=state, reason=reason, received_at=received_at, sender=address,
            reply_to=reply_to, reply_note=note)
        if mail.sent_by_faxbot(message, self.own_addresses()):
            return record(text.OWN_MAIL)
        if mail.automatic(message):
            return record(text.AUTOMATIC)
        if address is None:
            return record(text.NO_SENDER)
        confirmed_by = None
        if not mail.confirmed(message, address, settings.get('checked_by')):
            if mail.copier_sender(message, address, settings):
                confirmed_by = text.ACCEPTED_COPIER
            elif not mail.microsoft_internal(message, address, settings):
                return record(text.NOT_AUTHENTICATED if settings.get('checked_by') else text.NO_TRUSTED_SERVER)
            else:
                confirmed_by = text.ACCEPTED_INTERNAL
        # The sender is confirmed: from here on they hear what happened, unless the message forbids a reply.
        reply_to = address if mail.may_reply(message) else None
        person_id, person_name = self.store.person_for(source.id, address)
        if person_id is None:
            return record(text.NOT_LISTED.format(address=address), reply_to=reply_to, note=text.REPLY_NOT_LISTED)
        if too_large:
            return record(text.TOO_LARGE_SEND.format(limit=values.max_file_size_mb), state='failed',
                          reply_to=reply_to)
        try:
            number = (mail.tagged_number(message, settings['address'], values.fax_default_country)
                      or mail.subject_number(message.subject, values.fax_default_country))
        except ValueError as error:
            reason = (text.TWO_NUMBERS if str(error) == 'two numbers'
                      else text.BAD_NUMBER.format(text=str(error) or 'The number'))
            return record(reason, reply_to=reply_to)
        if number is None:
            return record(text.NO_NUMBER.format(example=mail.example_address(settings['address'])),
                          reply_to=reply_to)
        if not message.documents:
            return record(text.NO_DOCUMENT_SEND, reply_to=reply_to)
        submission = send.Submission(
            operation_id=message.operation_id, to_number=number, documents=message.documents,
            reference=message.reference, subject=message.subject, sender=address, person_id=person_id,
            person_name=person_name, received_at=received_at, reply_to=reply_to, confirmed_by=confirmed_by)
        return send.submit(self.access(), self.runtime, self.store, source, submission, secret.get('key_token'))

    # -- folders ---------------------------------------------------------------------------
    def _folder_problem(self, source, error):
        path = source.settings['path']
        return (text.FOLDER_MISSING if str(error) == 'missing' else text.FOLDER_UNREADABLE).format(path=path)

    def check_folder(self, source):
        settings, values = source.settings, self.values()
        root, settle = settings['path'], settings['settle_seconds']
        try:
            folders.check(root)
            found = folders.documents(root)
        except folders.FolderError as error:
            return False, self._folder_problem(source, error)
        limit = int(values.max_file_size_mb) * 1024 * 1024
        handled = 0
        for name, path, kind in found[:PER_CHECK]:
            stable, _, since = self.watch.settled(source.id, path, settle)
            sidecar = folders.sidecar_for(root, name)
            # The sidecar is watched from the moment it appears, so both settle together.
            if sidecar is not None and not self.watch.settled(source.id, sidecar, settle)[0]:
                continue
            if not stable:
                continue
            try:
                data = folders.read(path, limit)
            except folders.FolderError:
                self._file_failed(source, root, path, sidecar, name, digest=None,
                                  reason=(text.TOO_LARGE_SEND if source.direction == 'send'
                                          else text.TOO_LARGE_RECEIVE).format(limit=values.max_file_size_mb))
                handled += 1
                continue
            except OSError:
                continue
            digest = hashlib.sha256(data).hexdigest()
            try:
                if source.direction == 'receive':
                    self.receive_file(source, root, path, name, kind, data, digest, sidecar)
                else:
                    if not self.send_file(source, root, path, name, kind, data, digest, sidecar, since):
                        continue
            except send.Retry as wait:
                return False, str(wait)
            self.watch.forget(source.id, path)
            if sidecar:
                self.watch.forget(source.id, sidecar)
            handled += 1
        return True, text.CHECKED.format(count=_count(handled, 'file')) if handled else text.CHECKED_NOTHING

    def _file_failed(self, source, root, path, sidecar, name, *, digest, reason, operation_id=None):
        self.store.record(source.id, source.direction, operation_id or 'f:' + (digest or hashlib.sha256(
            name.encode()).hexdigest()), '', state='failed', reason=reason, reference=name, subject=name,
            document_digest=digest)
        folders.move(root, path, failed=True, reason=reason, sidecar=sidecar)

    def _sidecar(self, sidecar):
        if sidecar is None:
            return {}
        with open(sidecar, 'rb') as handle:
            return parse_sidecar(handle.read(65536), country=self.values().fax_default_country)

    def receive_file(self, source, root, path, name, kind, data, digest, sidecar):
        try:
            details = self._sidecar(sidecar)
        except ImportInputError as error:
            return self._file_failed(source, root, path, sidecar, name, digest=digest,
                                     reason=text.BAD_SIDECAR.format(detail=str(error)))
        destination, label, problem = self.destination(source)
        number = details.get('to_number')
        if number is None and destination is None:
            return self._file_failed(source, root, path, sidecar, name, digest=digest, reason=problem)
        arrival = receive.Arrival(
            operation_id='f:' + digest, part='', name=name, kind=kind, data=data, digest=digest, to_number=number,
            from_number=details.get('from_number'), received_at=details.get('source_received_at'), reference=name,
            subject=name, pages=details.get('pages'), mailbox_label=None if number else label,
            mailbox_id=None if number else destination, report={'sidecar_operation_id': details.get('operation_id')})
        item, outcome = receive.file_document(self.access(), self.values(), self.store, source, arrival)
        failed = outcome in ('failed', 'conflict')
        folders.move(root, path, failed=failed, reason=item.get('reason') if failed else None, sidecar=sidecar)

    def send_file(self, source, root, path, name, kind, data, digest, sidecar, since):
        """Returns False when the file should wait (its sidecar has not arrived yet)."""
        if sidecar is None:
            minutes = source.settings.get('sidecar_minutes', 10)
            if self.watch.clock() - since < minutes * 60:
                return False
            reason = text.NO_SIDECAR_SEND.format(name=os.path.splitext(name)[0] + '.json', minutes=minutes)
            self._file_failed(source, root, path, None, name, digest=digest, reason=reason)
            return True
        try:
            details = self._sidecar(sidecar)
        except ImportInputError as error:
            self._file_failed(source, root, path, sidecar, name, digest=digest,
                              reason=text.BAD_SIDECAR.format(detail=str(error)))
            return True
        number = details.get('to_number')
        if not number or not is_canonical(number):
            reason = text.BAD_NUMBER.format(text=number) if number else text.NO_NUMBER.format(
                example='a sidecar file such as {"to": "+13035550100"}')
            self._file_failed(source, root, path, sidecar, name, digest=digest, reason=reason)
            return True
        document = mail.Attachment(name, kind, data, 1)
        operation_id = 's:' + hashlib.sha256(f'{digest}\n{number}'.encode()).hexdigest()
        submission = send.Submission(operation_id=operation_id, to_number=number, documents=[document],
                                     reference=name, subject=name)
        secret = self.store.secret(source)
        item, _ = send.submit(self.access(), self.runtime, self.store, source, submission, secret.get('key_token'))
        failed = item['state'] in ('failed', 'refused', 'conflict')
        folders.move(root, path, failed=failed, reason=item.get('reason') if failed else None, sidecar=sidecar)
        return True

    # -- replies ---------------------------------------------------------------------------
    def reply_step(self):
        due = self.store.due_replies()
        sent_any = False
        for item in due:
            source = self.store.get(item['source_id'])
            if source is None or source.removed or source.kind != 'email':
                self.store.reply_done(item, state='failed', note='The connector was removed before its reply was sent.')
                continue
            outcome = None
            if item['reply_state'] == 'waiting':
                outcome = self.store.fax_outcome(item['fax_job_id'])
                if outcome is None:
                    self.store.reply_done(item, state='failed', note='The fax no longer exists, so no reply was sent.')
                    continue
                if not replies.needs_result(outcome['state']):
                    if self.store.clock() - item['created_at'] > RESULT_WAIT:
                        self.store.reply_done(item, state='failed', note=text.NO_FINAL_RESULT)
                    else:
                        self.store.reply_wait(item)
                    continue
            message = replies.compose(source, item, outcome=outcome)
            try:
                secret = self.store.secret(source)
                token = (None if source.settings['sign_in'] == 'password'
                         else self.tokens.token(source.id, source.settings, secret))
                replies.send(source.settings, secret, message, token=token, context=self.ssl_context,
                             connect=self.smtp_connect)
            except replies.DefiniteFailure as error:
                self.store.reply_done(item, state='retry' if error.temporary else 'failed', note=str(error),
                                      kind='result' if outcome else None)
                continue
            except replies.AmbiguousFailure as error:
                self.store.reply_done(item, state='uncertain', note=str(error) + ' It is not sent again.')
                continue
            except TokenUnavailable as error:
                self.store.reply_done(item, state='retry', note=text.TOKEN_UNAVAILABLE.format(detail=str(error)))
                continue
            except Exception:
                self.store.reply_done(item, state='retry', note=text.SECRET_UNREADABLE)
                continue
            self.store.reply_done(item, state='sent')
            sent_any = True
        return sent_any


def _count(number, noun):
    return f'1 {noun}' if number == 1 else f'{number} {noun}s'
