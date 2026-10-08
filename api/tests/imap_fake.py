"""A small IMAP4rev1 server over TLS for connector tests: real sockets, real imaplib on the other side.

It speaks the commands Faxbot's mailbox client sends (CAPABILITY, LOGIN,
AUTHENTICATE XOAUTH2, LIST, CREATE, SELECT, UID SEARCH/FETCH/MOVE/COPY/STORE/
EXPUNGE, EXPUNGE, LOGOUT) and records what it saw, including the exact XOAUTH2
response and whether the client answered an error challenge with an empty line.
Its certificate is made for each test run; nothing touches a real mailbox.
"""
from datetime import datetime, timedelta, timezone
import base64
import ipaddress
import re
import socket
import ssl
import threading

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


def certificate(directory):
    """A self-signed certificate for localhost; returns (certificate path, key path)."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'localhost')])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost'),
                                                        x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]),
                           critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    cert_path, key_path = directory / 'imap-cert.pem', directory / 'imap-key.pem'
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return cert_path, key_path


def client_context(cert_path):
    return ssl.create_default_context(cafile=str(cert_path))


class FakeImap:
    def __init__(self, directory, *, password=None, token=None, user='fax@example.com', move=True):
        self.user, self.password, self.token, self.move = user, password, token, move
        self.folders = {'INBOX': []}
        self.next_uid = 1
        self.refuse_moves = False
        self.logins, self.xoauth2, self.empty_answers, self.commands = [], [], 0, []
        cert, key = certificate(directory)
        self.cert = cert
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(str(cert), str(key))
        self.server = socket.socket()
        self.server.bind(('127.0.0.1', 0))
        self.server.listen(5)
        self.port = self.server.getsockname()[1]
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def add(self, raw, folder='INBOX', when=None):
        with self.lock:
            uid = self.next_uid
            self.next_uid += 1
            self.folders.setdefault(folder, []).append((uid, raw, when or datetime(2026, 10, 7, 9, 0)))
            return uid

    def count(self, folder):
        with self.lock:
            return len(self.folders.get(folder, []))

    def close(self):
        try:
            self.server.close()
        except OSError:
            pass

    # -- the server --------------------------------------------------------------------
    def _serve(self):
        while True:
            try:
                raw, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self._session, args=(raw,), daemon=True).start()

    def _session(self, raw):
        try:
            connection = self.context.wrap_socket(raw, server_side=True)
        except (OSError, ssl.SSLError):
            raw.close()
            return
        reader = connection.makefile('rb')
        send = lambda data: connection.sendall(data if isinstance(data, bytes) else data.encode())  # noqa: E731
        send('* OK [CAPABILITY IMAP4rev1 AUTH=XOAUTH2' + (' MOVE' if self.move else '') + ' UIDPLUS] Fake ready\r\n')
        selected = None
        try:
            while True:
                line = reader.readline()
                if not line:
                    return
                text = line.decode('utf-8', 'replace').rstrip('\r\n')
                tag, _, rest = text.partition(' ')
                command, _, arguments = rest.partition(' ')
                command = command.upper()
                self.commands.append(command + (' ' + arguments.split(' ', 1)[0].upper() if command == 'UID' else ''))
                if command == 'CAPABILITY':
                    send('* CAPABILITY IMAP4rev1 AUTH=XOAUTH2' + (' MOVE' if self.move else '') + ' UIDPLUS\r\n')
                    send(f'{tag} OK done\r\n')
                elif command == 'LOGIN':
                    user, password = _strings(arguments)
                    self.logins.append(user)
                    good = self.password is not None and user == self.user and password == self.password
                    send(f'{tag} OK logged in\r\n' if good else f'{tag} NO [AUTHENTICATIONFAILED] Invalid\r\n')
                elif command == 'AUTHENTICATE':
                    send('+ \r\n')
                    response = reader.readline().rstrip(b'\r\n')
                    decoded = base64.b64decode(response)
                    self.xoauth2.append(decoded)
                    expected = f'user={self.user}\x01auth=Bearer {self.token}\x01\x01'.encode()
                    if self.token is not None and decoded == expected:
                        send(f'{tag} OK Success\r\n')
                    else:
                        error = base64.b64encode(b'{"status":"401","schemes":"bearer"}').decode()
                        send(f'+ {error}\r\n')
                        answer = reader.readline().rstrip(b'\r\n')
                        if answer == b'':
                            self.empty_answers += 1
                        send(f'{tag} NO [AUTHENTICATIONFAILED] Invalid credentials\r\n')
                elif command == 'LIST':
                    name = _strings(arguments)[-1]
                    with self.lock:
                        if name in self.folders:
                            send(f'* LIST () "/" "{name}"\r\n')
                    send(f'{tag} OK listed\r\n')
                elif command == 'CREATE':
                    name = _strings(arguments)[0]
                    with self.lock:
                        self.folders.setdefault(name, [])
                    send(f'{tag} OK created\r\n')
                elif command == 'SELECT':
                    name = _strings(arguments)[0]
                    with self.lock:
                        found = name in self.folders
                        total = len(self.folders.get(name, []))
                    if not found:
                        send(f'{tag} NO no such folder\r\n')
                        continue
                    selected = name
                    send(f'* {total} EXISTS\r\n* OK [UIDVALIDITY 1] ok\r\n{tag} OK [READ-WRITE] selected\r\n')
                elif command == 'UID':
                    self._uid(tag, arguments, selected, send)
                elif command == 'EXPUNGE':
                    self._expunge(selected)
                    send(f'{tag} OK expunged\r\n')
                elif command == 'NOOP':
                    send(f'{tag} OK\r\n')
                elif command == 'LOGOUT':
                    send(f'* BYE bye\r\n{tag} OK bye\r\n')
                    return
                else:
                    send(f'{tag} BAD unknown\r\n')
        except (OSError, ssl.SSLError, ValueError):
            return
        finally:
            try:
                connection.close()
            except OSError:
                pass

    def _find(self, folder, uid):
        for position, entry in enumerate(self.folders.get(folder, [])):
            if entry[0] == uid:
                return position, entry
        return None, None

    def _uid(self, tag, arguments, selected, send):
        verb, _, rest = arguments.partition(' ')
        verb = verb.upper()
        if verb == 'SEARCH':
            with self.lock:
                uids = [str(entry[0]) for entry in self.folders.get(selected, []) if len(entry) == 3]
            send('* SEARCH' + ''.join(' ' + uid for uid in uids) + f'\r\n{tag} OK searched\r\n')
            return
        uid_text, _, rest = rest.partition(' ')
        uid = int(uid_text)
        with self.lock:
            position, entry = self._find(selected, uid)
        if entry is None:
            send(f'{tag} OK nothing\r\n')
            return
        if verb == 'FETCH':
            items = rest.upper()
            raw, when = entry[1], entry[2]
            stamp = when.strftime('%d-%b-%Y %H:%M:%S +0000')
            if 'RFC822.SIZE' in items and 'BODY' not in items:
                send(f'* {position + 1} FETCH (UID {uid} RFC822.SIZE {len(raw)})\r\n{tag} OK fetched\r\n')
                return
            body = raw.split(b'\r\n\r\n', 1)[0] + b'\r\n\r\n' if 'HEADER' in items else raw
            section = 'BODY[HEADER]' if 'HEADER' in items else 'BODY[]'
            send(f'* {position + 1} FETCH (UID {uid} INTERNALDATE "{stamp}" {section} {{{len(body)}}}\r\n'.encode()
                 + body + b')\r\n' + f'{tag} OK fetched\r\n'.encode())
        elif verb in ('MOVE', 'COPY'):
            target = _strings(rest)[0]
            if self.refuse_moves:
                send(f'{tag} NO cannot move\r\n')
                return
            with self.lock:
                self.folders.setdefault(target, []).append(entry)
                if verb == 'MOVE':
                    self.folders[selected].pop(position)
            send(f'{tag} OK moved\r\n')
        elif verb == 'STORE':
            with self.lock:
                self.folders[selected][position] = entry + ('deleted',)
            send(f'{tag} OK stored\r\n')
        elif verb == 'EXPUNGE':
            self._expunge(selected)
            send(f'{tag} OK expunged\r\n')
        else:
            send(f'{tag} BAD unknown\r\n')

    def _expunge(self, selected):
        with self.lock:
            self.folders[selected] = [entry for entry in self.folders.get(selected, []) if len(entry) == 3]


def _strings(text):
    """IMAP strings: "quoted" (with \\ escapes) or atoms, in order."""
    found = []
    for match in re.finditer(r'"((?:[^"\\]|\\.)*)"|(\S+)', text):
        if match.group(1) is not None:
            found.append(re.sub(r'\\(.)', r'\1', match.group(1)))
        else:
            found.append(match.group(2))
    return found
