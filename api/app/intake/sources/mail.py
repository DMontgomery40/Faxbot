"""Reading one email: its identity, its sender and whether that sender is confirmed, the fax number, attachments.

Identity: the message's Message-ID, hashed so any length fits (``m:`` and its
SHA-256); a message without one is identified by the SHA-256 of its whole raw
bytes (``r:``). The same message polled again has the same identity.

Sender confirmation follows RFC 8601 and RFC 7489. Faxbot reads the
Authentication-Results header written by the administrator's own mail server:
the topmost one carrying that server's name (its authserv-id). Microsoft 365
leaves the name out of its header, so for it Faxbot reads the topmost header
without a name. Border servers remove forged copies carrying their own name;
the topmost is the most recent. The message is confirmed when that header says
``dmarc=pass`` for the From domain, ``dkim=pass`` with a signing domain
(``header.d``) aligned with the From domain, or ``spf=pass`` with an envelope
sender domain (``smtp.mailfrom``) aligned with it. Aligned means the same
domain, or one a subdomain of the other (relaxed alignment without a public
suffix list: a public suffix cannot sign or pass SPF for its own name).

Mail from inside a Microsoft 365 organization often carries no DKIM or SPF
pass at all. Exchange marks it instead: ``X-MS-Exchange-Organization-AuthAs:
Internal`` for a message submitted by an authenticated mailbox in the tenant,
with ``X-MS-Exchange-Organization-AuthSource`` naming the server that decided.
Exchange's header firewall removes every ``X-MS-Exchange-Organization-``
header from messages entering the organization from untrusted sources, and
mailbox delivery keeps AuthAs, AuthSource and AuthMechanism on the stored
message (learn.microsoft.com/exchange/header-firewall-exchange-2013-help,
updated 2025-09-09; techcommunity.microsoft.com, "Demystifying and
troubleshooting hybrid mail flow: when is a message internal?"). So Faxbot
accepts exactly one ``AuthAs: Internal`` and one AuthSource as confirmation
only on a connector set up as Microsoft 365 that reads the mailbox from
Exchange Online itself, and only for a From address in the mailbox's own
domain. A generic IMAP connector never trusts these headers: any server could
have written them.
"""
from dataclasses import dataclass, field
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses, parseaddr
import hashlib
import re

from ...routing.numbers import InvalidNumber, normalize_number


MICROSOFT_365 = 'microsoft365'
EXCHANGE_ONLINE = 'outlook.office365.com'
DOCUMENT_TYPES = {'application/pdf': 'pdf', 'image/tiff': 'tiff', 'image/tif': 'tiff'}
DOCUMENT_SUFFIXES = {'.pdf': 'pdf', '.tif': 'tiff', '.tiff': 'tiff'}
SIDECAR_SUFFIX = '.json'
_FAXBOT_MESSAGE = re.compile(r'<(intake|faxbot-reply)-[^@>]+@[^>]+>')
_NUMBER_TEXT = re.compile(r'\+?\(?\d[\d\s().\-]{5,}\d')


@dataclass(frozen=True)
class Attachment:
    name: str
    kind: str          # 'pdf', 'tiff' or 'sidecar'
    data: bytes
    position: int = 0  # 1-based among documents; 0 for a sidecar

    @property
    def stem(self):
        return self.name.rsplit('.', 1)[0].casefold() if '.' in self.name else self.name.casefold()

    @property
    def digest(self):
        return hashlib.sha256(self.data).hexdigest()


@dataclass
class Message:
    raw: bytes
    parsed: object
    operation_id: str
    reference: str | None
    subject: str
    documents: list = field(default_factory=list)
    sidecars: list = field(default_factory=list)
    unreadable: list = field(default_factory=list)


def parse(raw):
    """Parse raw RFC 5322 bytes; never raises on malformed mail (the email package tolerates it)."""
    parsed = BytesParser(policy=policy.default).parsebytes(raw)
    message_id = _header(parsed, 'Message-ID').strip()
    if message_id and len(message_id) <= 998 and all(32 < ord(c) < 127 for c in message_id):
        operation_id, reference = 'm:' + hashlib.sha256(message_id.encode('ascii')).hexdigest(), message_id[:255]
    else:
        operation_id, reference = 'r:' + hashlib.sha256(raw).hexdigest(), None
    subject = ' '.join(_header(parsed, 'Subject').split())[:200]
    message = Message(raw, parsed, operation_id, reference, subject)
    position = 0
    for part in parsed.walk():
        if part.is_multipart():
            continue
        name = _filename(part)
        content_type = part.get_content_type()
        suffix = ('.' + name.rsplit('.', 1)[-1].casefold()) if '.' in name else ''
        kind = DOCUMENT_TYPES.get(content_type) or DOCUMENT_SUFFIXES.get(suffix)
        if kind is None and suffix == SIDECAR_SUFFIX and content_type in ('application/json', 'text/plain',
                                                                          'application/octet-stream'):
            kind = 'sidecar'
        if kind is None:
            if part.get_content_disposition() == 'attachment' and content_type not in ('text/plain', 'text/html'):
                message.unreadable.append(name or content_type)
            continue
        try:
            data = part.get_payload(decode=True) or b''
        except Exception:
            data = b''
        if kind == 'sidecar':
            message.sidecars.append(Attachment(name, 'sidecar', data))
            continue
        position += 1
        message.documents.append(Attachment(name or f'attachment-{position}.{kind}', kind, data, position))
    return message


def _header(parsed, name):
    try:
        value = parsed.get(name)
    except Exception:
        return ''
    return str(value) if value is not None else ''


def _filename(part):
    try:
        name = part.get_filename() or ''
    except Exception:
        name = ''
    name = name.replace('\\', '/').rsplit('/', 1)[-1]
    return ''.join(c for c in name if c.isprintable())[:200].strip()


# -- sender -------------------------------------------------------------------------

def sender(message):
    """The single From address, lower-cased, or None when there is not exactly one."""
    values = message.parsed.get_all('From') or []
    if len(values) != 1:
        return None
    addresses = [address for _, address in getaddresses([str(values[0])]) if address]
    if len(addresses) != 1 or addresses[0].count('@') != 1:
        return None
    local, domain = addresses[0].rsplit('@', 1)
    if not local or '.' not in domain:
        return None
    return f'{local}@{domain}'.casefold()


def domain_of(address):
    if not address:
        return ''
    address = address.strip().strip('<>')
    return address.rsplit('@', 1)[-1].strip().strip('.').casefold()


def aligned(first, second):
    """Same domain, or one a subdomain of the other (both with at least two labels)."""
    if not first or not second or first.count('.') < 1 or second.count('.') < 1:
        return False
    return first == second or first.endswith('.' + second) or second.endswith('.' + first)


# -- Authentication-Results (RFC 8601) -------------------------------------------------

def _without_comments(value):
    """Remove (comments), which may hold ';' or '=', keeping "quoted strings" intact."""
    out, depth, quoted, escaped = [], 0, False, False
    for char in value:
        if escaped:
            if depth == 0:
                out.append(char)
            escaped = False
            continue
        if char == '\\':
            escaped = True
            if depth == 0:
                out.append(char)
            continue
        if quoted:
            out.append(char)
            if char == '"':
                quoted = False
            continue
        if char == '"' and depth == 0:
            quoted = True
            out.append(char)
        elif char == '(':
            depth += 1
        elif char == ')' and depth:
            depth -= 1
        elif depth == 0:
            out.append(char)
    return ''.join(out)


def parse_results(value):
    """(authserv-id or None, [(method, result, {property: value})]) from one header value."""
    text = ' '.join(_without_comments(str(value)).split())
    segments = [segment.strip() for segment in text.split(';')]
    if not segments or not segments[0]:
        return None, []
    first = segments[0]
    if '=' in first.split(' ', 1)[0]:
        server, results = None, segments          # Microsoft 365: no authserv-id
    else:
        server, results = first.split(' ', 1)[0].casefold(), segments[1:]
    parsed = []
    for segment in results:
        tokens = segment.split()
        if not tokens or '=' not in tokens[0]:
            continue
        method, result = tokens[0].split('=', 1)
        properties = {}
        for token in tokens[1:]:
            if '=' in token:
                name, item = token.split('=', 1)
                properties.setdefault(name.casefold(), item.strip('"').casefold())
        parsed.append((method.split('/', 1)[0].casefold(), result.casefold(), properties))
    return server, parsed


def trusted_results(message, server):
    """The topmost Authentication-Results header from the administrator's own mail server, parsed."""
    if not server:
        return None
    wanted = None if server == MICROSOFT_365 else server.strip().casefold()
    for value in message.parsed.get_all('Authentication-Results') or []:
        found, results = parse_results(value)
        if found == wanted:
            return results
    return None


def confirmed(message, address, server):
    """Whether the From address passed aligned DMARC, DKIM or SPF at the administrator's mail server."""
    results = trusted_results(message, server)
    if not results or not address:
        return False
    domain = domain_of(address)
    for method, result, properties in results:
        if result != 'pass':
            continue
        if method == 'dmarc' and properties.get('header.from', domain) == domain:
            return True
        if method == 'dkim':
            signer = properties.get('header.d') or domain_of(properties.get('header.i', ''))
            if aligned(signer, domain):
                return True
        if method == 'spf':
            envelope = properties.get('smtp.mailfrom') or ''
            if aligned(domain_of(envelope) if '@' in envelope else envelope.strip('.'), domain):
                return True
    return False


def microsoft_internal(message, address, settings):
    """Sent from inside the connector's own Microsoft 365 organization (see the module notes)."""
    if (settings.get('provider') != MICROSOFT_365 or settings.get('imap_host') != EXCHANGE_ONLINE
            or settings.get('checked_by') != MICROSOFT_365 or not address):
        return False
    marks = message.parsed.get_all('X-MS-Exchange-Organization-AuthAs') or []
    sources = message.parsed.get_all('X-MS-Exchange-Organization-AuthSource') or []
    if len(marks) != 1 or str(marks[0]).strip().casefold() != 'internal':
        return False
    if len(sources) != 1 or not str(sources[0]).strip():
        return False
    own = domain_of(settings.get('address'))
    return bool(own) and domain_of(address) == own


# -- loops and automatic mail (RFC 3834) ------------------------------------------------

def sent_by_faxbot(message, own_addresses=()):
    """Faxbot's own email: an intake delivery, a connector reply, or mail from a connector's own address."""
    if _FAXBOT_MESSAGE.fullmatch(_header(message.parsed, 'Message-ID').strip() or ''):
        return True
    address = sender(message)
    return bool(address and address in {value.casefold() for value in own_addresses if value})


def automatic(message):
    """Sent automatically: Auto-Submitted other than "no" (RFC 3834)."""
    value = _header(message.parsed, 'Auto-Submitted').strip().casefold()
    return bool(value) and value != 'no'


def may_reply(message):
    """No reply to a null return path, a mailing list or bulk mail, or automatic mail."""
    if automatic(message):
        return False
    return_path = _header(message.parsed, 'Return-Path').strip()
    if return_path in ('<>', '') and message.parsed.get('Return-Path') is not None:
        return False
    if message.parsed.get('List-Id') is not None or message.parsed.get('List-Unsubscribe') is not None:
        return False
    return _header(message.parsed, 'Precedence').strip().casefold() not in ('bulk', 'list', 'junk')


# -- the fax number of an email to fax -----------------------------------------------

def tagged_number(message, mailbox_address, country):
    """``fax+13035550100@example.com``: the number tagged onto the connector's own address, or None."""
    if not mailbox_address or '@' not in mailbox_address:
        return None
    local, domain = mailbox_address.casefold().rsplit('@', 1)
    values = []
    for name in ('To', 'Cc', 'Delivered-To', 'X-Original-To'):
        values.extend(str(value) for value in (message.parsed.get_all(name) or []))
    found = set()
    for _, address in getaddresses(values):
        address = address.casefold()
        if address.count('@') != 1:
            continue
        their_local, their_domain = address.rsplit('@', 1)
        if their_domain != domain or '+' not in their_local:
            continue
        base, tag = their_local.split('+', 1)
        if base != local:
            continue
        found.add(_tag_number(tag, country))
    if len(found) > 1:
        raise ValueError('two numbers')
    return next(iter(found), None)


def _tag_number(tag, country):
    """A tag is read as dialed in the installation's country, else as a full number with its country code."""
    attempts = [tag] if tag.startswith('+') else [tag, '+' + tag]
    for text in attempts:
        try:
            return normalize_number(text, country=country)
        except (InvalidNumber, ValueError):
            continue
    raise InvalidNumber(tag)


def subject_number(subject, country):
    """The one fax number written in the subject, or None when there is none."""
    found = set()
    for match in _NUMBER_TEXT.finditer(subject or ''):
        text = match.group(0).strip()
        try:
            found.add(normalize_number(text, country=country))
        except (InvalidNumber, ValueError):
            if sum(c.isdigit() for c in text) >= 7:
                raise InvalidNumber(text) from None
    if len(found) > 1:
        raise ValueError('two numbers')
    return next(iter(found), None)


def example_address(mailbox_address):
    if not mailbox_address or '@' not in mailbox_address:
        return 'the fax mailbox with the number added, such as fax+13035550100@example.com'
    local, domain = mailbox_address.rsplit('@', 1)
    return f'{local}+13035550100@{domain}'


def display_address(value):
    return parseaddr(value or '')[1] or ''
