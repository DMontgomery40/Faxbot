"""HISP accounts and FHIR clients, held as provider accounts (WP-B) of the ``digital`` kind.

They live in the configuration revision's accounts document beside the fax
accounts, as ``{'provider': 'hisp' | 'fhir', 'kind': 'digital', ...}``, so a
fax accepted under a revision uses that revision's credentials (accounts
design §3.5), and every change goes through the audited apply path. They are
never fax accounts: ``accounts.all_accounts`` leaves them out, so they never
become a sending account, a provider route or a receiving number.

Secrets (passwords, private keys) are written only through the API and never
shown back: a view says which secret fields hold a value. Certificates are
public and shown as who they name and until when.

Each account has a plan, so the route planner can price a message through the
shared predictor (``routes.price``): a price per message, a monthly fee and the
messages it includes, with where the price was published and when. A HISP
account without a plan has an unknown price, never zero. A FHIR client posts
to the recipient's own server, so it starts as "no charge per document",
which the administrator can change.
"""
from dataclasses import dataclass, field
from datetime import date
import re

from ..accounts import KEY, PROVIDER_ORDER, RESERVED, AccountsError, documents


DIGITAL = 'digital'
KINDS = ('hisp', 'fhir')
SECURITY = ('faxbot', 'hisp')
ALGORITHMS = ('RS384', 'ES384')
MAX_PEM = 32 * 1024
CURRENCIES = ('USD', 'CAD', 'GBP', 'EUR', 'AUD')
# Published plan evidence a person can start from (source and the day it was read).
PLAN_PRESETS = {
    'inpriva-hdirectmail': {
        'label': 'Inpriva hDirectMail (3 Direct addresses)', 'kind': 'hisp', 'currency': 'USD',
        'monthly_fee': '16.58', 'price_per_message': '0', 'included_messages': None,
        'price_source': 'https://hdirect.inpriva.com/', 'price_date': '2026-10-08',
        'note': 'Published as $199 a year for 3 Direct addresses; Faxbot counts it as $16.58 a month.'},
}


@dataclass(frozen=True)
class Field:
    name: str
    label: str
    kind: str = 'text'          # text | int | bool | pem | choice | money | date | lines
    secret: bool = False
    required: bool = False
    default: object = None
    choices: tuple = ()
    help: str | None = None


PLAN_FIELDS = (
    Field('currency', 'Currency', 'choice', default='USD', choices=CURRENCIES),
    Field('price_per_message', 'Price for each message', 'money',
          help='What the service charges for one message; enter 0 when it charges nothing per message.'),
    Field('monthly_fee', 'Monthly fee', 'money', help='Leave it empty when there is none.'),
    Field('included_messages', 'Messages included each month', 'int',
          help='Leave it empty when the monthly fee covers every message.'),
    Field('price_source', 'Where the price is published', 'text', help='A web address, such as the price page.'),
    Field('price_date', 'Day you read the price', 'date'),
)
FIELDS = {
    'hisp': (
        Field('direct_address', 'Your Direct address', required=True,
              help='The address your HISP gave you, such as faxes@direct.yourclinic.example.'),
        Field('smtp_host', 'HISP sending server', required=True, help='From your HISP, such as smtp.hisp.example.'),
        Field('smtp_port', 'Sending port', 'int', default=587),
        Field('username', 'Sign-in name', help='Leave it empty to sign in with your Direct address.'),
        Field('password', 'Password', secret=True, required=True),
        Field('security', 'Who signs and encrypts each message', 'choice', default='faxbot', choices=SECURITY,
              help='faxbot: Faxbot signs and encrypts with your certificate. hisp: your HISP does it for you.'),
        Field('certificate', 'Your Direct certificate', 'pem',
              help='The certificate your HISP or certificate authority issued for your Direct address.'),
        Field('private_key', 'Private key for that certificate', 'pem', secret=True),
        Field('trust_bundle_urls', 'Trust bundle addresses', 'lines',
              help='The trust bundles your HISP belongs to, one web address per line.'),
        Field('request_delivery', 'Ask the recipient to confirm delivery', 'bool', default=True),
        Field('wait_minutes', 'Minutes to wait for each confirmation', 'int', default=60),
        Field('receives', 'Receive Direct messages', 'bool', default=False),
        Field('imap_host', 'HISP mail server for received messages',
              help='From your HISP, such as imap.hisp.example.'),
        Field('imap_port', 'Receiving port', 'int', default=993),
        Field('imap_folder', 'Folder to read', default='INBOX'),
        Field('processed_folder', 'Folder for messages Faxbot filed', default='Faxbot filed'),
        Field('mailbox_id', 'Mailbox that received documents go to',
              help='Direct messages carry no fax number, so choose the mailbox they land in.'),
    ) + PLAN_FIELDS,
    'fhir': (
        Field('client_id', 'Client ID', required=True, help='The ID the recipient\'s system gave Faxbot when you '
                                                            'registered it.'),
        Field('token_url', 'Sign-in address',
              help='Leave it empty: Faxbot reads it from each server\'s published settings.'),
        Field('scope', 'Access requested', default='system/DocumentReference.c',
              help='system/DocumentReference.c for newer servers, system/DocumentReference.write for older ones.'),
        Field('algorithm', 'Signing method', 'choice', default='RS384', choices=ALGORITHMS),
        Field('key_id', 'Key ID'),
        Field('signing_key', 'Signing key', 'pem', secret=True,
              help='Faxbot can make one for you; give the recipient\'s system its public key set.'),
        Field('author', 'Your organization\'s name on each document'),
        Field('document_type', 'Document type code (LOINC)',
              help='Leave it empty to send no type, or use the code the recipient asks for.'),
    ) + PLAN_FIELDS,
}
LABELS = {'hisp': 'Direct messages (HISP)', 'fhir': 'FHIR client'}


@dataclass(frozen=True)
class DigitalAccount:
    key: str
    kind: str
    label: str
    enabled: bool
    settings: dict = field(compare=False, repr=False)
    credentials: dict = field(compare=False, repr=False)

    def setting(self, name):
        definition = next((item for item in FIELDS[self.kind] if item.name == name), None)
        value = self.settings.get(name)
        if value is None and definition is not None:
            return definition.default
        return value

    def secret(self, name):
        return self.credentials.get(name) or None

    @property
    def secrets_set(self):
        return tuple(sorted(name for name, value in self.credentials.items() if value))

    @property
    def missing(self):
        """Labels of what must still be filled in before it can send."""
        missing = [item.label for item in FIELDS[self.kind]
                   if item.required and not (self.secret(item.name) if item.secret else self.setting(item.name))]
        if self.kind == 'hisp' and self.setting('security') == 'faxbot':
            for name in ('certificate', 'private_key'):
                definition = next(item for item in FIELDS['hisp'] if item.name == name)
                if not (self.secret(name) if definition.secret else self.setting(name)):
                    missing.append(definition.label)
        if self.kind == 'fhir' and not self.secret('signing_key'):
            missing.append('Signing key')
        return tuple(missing)

    @property
    def receives(self):
        return self.kind == 'hisp' and bool(self.setting('receives'))

    @property
    def set_up(self):
        return not self.missing


def is_digital(doc):
    return isinstance(doc, dict) and doc.get('kind') == DIGITAL and doc.get('provider') in KINDS


def _account(key, doc):
    return DigitalAccount(key, doc['provider'], doc.get('label') or f"{LABELS[doc['provider']]} ({key})",
                          doc.get('enabled', True) is not False, dict(doc.get('settings') or {}),
                          dict(doc.get('credentials') or {}))


def digital_accounts(values, kind=None):
    """The revision's digital accounts, by key order; ``kind`` narrows to ``hisp`` or ``fhir``."""
    found = [_account(key, doc) for key, doc in sorted(documents(values).items()) if is_digital(doc)]
    return tuple(account for account in found if kind is None or account.kind == kind)


def digital_account(values, key):
    doc = documents(values).get(key)
    return _account(key, doc) if is_digital(doc) else None


def default_account(values, kind):
    """The first enabled, set-up account of ``kind``, or None."""
    return next((account for account in digital_accounts(values, kind) if account.enabled and account.set_up), None)


# Checking what the administrator entered ----------------------------------------------------------------------------

def _clean(definition, value):
    label = definition.label
    if value is None or value == '':
        return None
    if definition.kind == 'bool':
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in ('true', 'yes', 'on', '1', 'false', 'no', 'off', '0'):
            return value.strip().lower() in ('true', 'yes', 'on', '1')
        raise AccountsError(f'{label} is a yes or no setting.')
    if definition.kind == 'int':
        if isinstance(value, str) and value.strip().isdigit():
            value = int(value.strip())
        if type(value) is not int or not 0 < value <= 100_000:
            raise AccountsError(f'{label} must be a whole number above zero.')
        if definition.name.endswith('_port') and value > 65535:
            raise AccountsError(f'{label} must be a port number, from 1 to 65535.')
        return value
    if not isinstance(value, str):
        raise AccountsError(f'{label} must be text.')
    text = value.strip()
    if definition.kind == 'pem':
        if len(text) > MAX_PEM or '-----BEGIN ' not in text:
            raise AccountsError(f'Paste {label} as PEM text, starting with -----BEGIN.')
        return text.replace('\r\n', '\n')
    if definition.kind == 'lines':
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if len(lines) > 10 or any(re.fullmatch(r'https://[^\s]{3,500}', line) is None for line in lines):
            raise AccountsError(f'List up to 10 {label.lower()}, each starting with https://.')
        return '\n'.join(lines)
    if len(text) > 1024 or any(ord(char) < 32 for char in text):
        raise AccountsError(f'{label} is too long or has characters Faxbot cannot use.')
    if definition.kind == 'choice' and text not in definition.choices:
        raise AccountsError(f'{label} is one of: {", ".join(definition.choices)}.')
    if definition.kind == 'money':
        from ..routing.costs import InvalidRateCard, parse_amount
        try:
            parse_amount(text, whole_digits=5)
        except InvalidRateCard:
            raise AccountsError(f'Write {label.lower()} as an amount, such as 0.25.') from None
    if definition.kind == 'date':
        try:
            day = date.fromisoformat(text)
        except ValueError:
            raise AccountsError(f'Write {label.lower()} as a day, such as 2026-10-08.') from None
        if day > date.today():
            raise AccountsError(f'{label} cannot be in the future.')
    if definition.name in ('direct_address',) and re.fullmatch(r'[^@\s]{1,64}@[a-z0-9.-]{3,253}', text.lower()) is None:
        raise AccountsError('Write your Direct address in full, such as faxes@direct.yourclinic.example.')
    if definition.name == 'direct_address':
        return text.lower()
    if definition.name in ('smtp_host', 'imap_host') and re.fullmatch(r'[A-Za-z0-9.-]{1,253}', text) is None:
        raise AccountsError(f'{label} is a server name, such as smtp.hisp.example.')
    if definition.name in ('token_url', 'price_source') and re.fullmatch(r'https://[^\s]{3,1000}', text) is None:
        raise AccountsError(f'{label} is a web address starting with https://.')
    return text


def _clean_fields(kind, settings, credentials, current_settings=None, current_credentials=None):
    fields = {item.name: item for item in FIELDS[kind]}
    settings = settings or {}
    credentials = credentials or {}
    if not isinstance(settings, dict) or not isinstance(credentials, dict):
        raise AccountsError('Settings and credentials must each be a list of names and values.')
    clean_settings, clean_credentials = dict(current_settings or {}), dict(current_credentials or {})
    for source, given_as_secret in ((settings, False), (credentials, True)):
        for name, value in source.items():
            definition = fields.get(name)
            if definition is None:
                raise AccountsError(f'This account has no setting called {str(name)[:40]}. Its settings are: '
                                    f'{", ".join(sorted(fields))}.')
            if definition.secret and not given_as_secret:
                raise AccountsError(f'{definition.label} is a secret, so it goes with the credentials.')
            if definition.secret and isinstance(value, str) and re.fullmatch(r'\*+[\s\S]{0,4}', value):
                raise AccountsError(f'Enter {definition.label} itself; Faxbot never shows a saved secret.')
            cleaned = _clean(definition, value)
            target = clean_credentials if definition.secret else clean_settings
            if cleaned is None:
                target.pop(name, None)
            else:
                target[name] = cleaned
    return clean_settings, clean_credentials


def _check_keys(kind, settings, credentials):
    """The certificate and private key belong together, and a FHIR signing key fits its method."""
    from . import certificates
    if kind == 'hisp':
        certificate = settings.get('certificate')
        private_key = credentials.get('private_key')
        if certificate:
            try:
                loaded = certificates.load_certificates(certificate.encode('ascii'))
            except certificates.CertificateRefused as error:
                raise AccountsError(str(error)) from None
            if private_key:
                key = _private_key(private_key)
                if not _same_key(loaded[0].public_key(), key.public_key()):
                    raise AccountsError('The private key does not belong to this certificate.')
    if kind == 'fhir' and credentials.get('signing_key'):
        from cryptography.hazmat.primitives.asymmetric import ec, rsa
        key = _private_key(credentials['signing_key'])
        algorithm = settings.get('algorithm') or 'RS384'
        if algorithm == 'RS384' and not isinstance(key, rsa.RSAPrivateKey):
            raise AccountsError('The RS384 signing method needs an RSA key.')
        if algorithm == 'ES384' and not (isinstance(key, ec.EllipticCurvePrivateKey)
                                         and key.curve.name == 'secp384r1'):
            raise AccountsError('The ES384 signing method needs a P-384 key.')


def _private_key(text):
    from . import certificates
    try:
        return certificates.load_private_key(text)
    except certificates.CertificateRefused as error:
        raise AccountsError(str(error)) from None


def _same_key(first, second):
    from cryptography.hazmat.primitives import serialization
    form = (serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return first.public_bytes(*form) == second.public_bytes(*form)


def _label(value, fallback):
    if value is None:
        return fallback
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 100:
        raise AccountsError('Give the account a name of up to 100 characters.')
    return value.strip()


def added(values, body, *, provider_ids=()):
    """The accounts document with one digital account added. Raises AccountsError."""
    docs = dict(documents(values))
    key = body.get('key')
    if not isinstance(key, str) or KEY.fullmatch(key) is None:
        raise AccountsError('Choose a key of up to 32 lowercase letters, digits, - or _, such as hisp or fhir-epic.')
    if key in RESERVED or key in PROVIDER_ORDER or key in set(provider_ids) or key in ('relay', 'digital'):
        raise AccountsError(f'{key} is kept for Faxbot itself or for a fax provider. Choose another key, such as '
                            f'{key}-account.')
    if key in docs:
        raise AccountsError(f'There is already an account called {key}. Choose another key.', 409)
    kind = body.get('provider')
    if kind not in KINDS:
        raise AccountsError('Choose a HISP account (hisp) or a FHIR client (fhir).')
    settings, credentials = _clean_fields(kind, body.get('settings'), body.get('credentials'))
    if kind == 'fhir':
        # Posting to the recipient's own server has no per-document charge unless you enter one.
        settings.setdefault('price_per_message', '0')
    _check_keys(kind, settings, credentials)
    docs[key] = {'provider': kind, 'kind': DIGITAL, 'label': _label(body.get('label'), f'{LABELS[kind]} ({key})'),
                 'enabled': True, 'settings': settings, 'credentials': credentials}
    return docs


def patched(values, key, body):
    """The accounts document after one change to a digital account. Raises AccountsError."""
    docs = dict(documents(values))
    doc = docs.get(key)
    if not is_digital(doc):
        raise AccountsError('Faxbot has no Direct or FHIR account with this key.', 404)
    current = dict(doc)
    if body.get('label') is not None:
        current['label'] = _label(body['label'], current.get('label'))
    if body.get('enabled') is not None:
        current['enabled'] = bool(body['enabled'])
    if body.get('settings') is not None or body.get('credentials') is not None:
        settings, credentials = _clean_fields(doc['provider'], body.get('settings'), body.get('credentials'),
                                              doc.get('settings'), doc.get('credentials'))
        _check_keys(doc['provider'], settings, credentials)
        current['settings'], current['credentials'] = settings, credentials
    docs[key] = current
    return docs


def with_signing_key(values, key, *, algorithm=None):
    """The accounts document with a new FHIR signing key made by Faxbot (never shown; its public key set is)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec, rsa
    import secrets
    docs = dict(documents(values))
    doc = docs.get(key)
    if not is_digital(doc) or doc['provider'] != 'fhir':
        raise AccountsError('Faxbot has no FHIR client with this key.', 404)
    settings = dict(doc.get('settings') or {})
    algorithm = algorithm or settings.get('algorithm') or 'RS384'
    if algorithm not in ALGORITHMS:
        raise AccountsError('Choose RS384 or ES384.')
    private = (rsa.generate_private_key(public_exponent=65537, key_size=3072) if algorithm == 'RS384'
               else ec.generate_private_key(ec.SECP384R1()))
    text = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                 serialization.NoEncryption()).decode('ascii')
    settings['algorithm'] = algorithm
    settings['key_id'] = f'faxbot-{secrets.token_hex(8)}'
    credentials = dict(doc.get('credentials') or {})
    credentials['signing_key'] = text
    docs[key] = {**doc, 'settings': settings, 'credentials': credentials}
    return docs


# Views -----------------------------------------------------------------------------------------------------------

def _certificate_view(account):
    from . import certificates
    text = account.setting('certificate')
    if not text:
        return None
    try:
        loaded = certificates.load_certificates(text.encode('ascii'))
    except certificates.CertificateRefused:
        return {'sentence': 'The certificate could not be read.', 'fingerprint': None}
    return {'sentence': certificates.describe(loaded[0]), 'fingerprint': certificates.fingerprint(loaded[0])}


def health(account, *, bundle=None):
    """(state, one sentence): ready, not_set_up or off."""
    if not account.enabled:
        return 'off', 'It is off, so no fax goes this way.'
    if account.missing:
        return 'not_set_up', f'Fill in {", ".join(account.missing).lower()} before Faxbot can use it.'
    if account.kind == 'hisp' and account.setting('security') == 'faxbot' and bundle is None:
        return 'not_set_up', 'Load a trust bundle before Faxbot can check recipients\' certificates.'
    if account.receives and not (account.setting('imap_host') and account.setting('mailbox_id')):
        return 'not_set_up', ('Fill in the HISP mail server for received messages and the mailbox they go to, or '
                              'turn receiving off.')
    return 'ready', 'Ready to send.' if not account.receives else 'Ready to send and receive.'


def account_view(account, *, bundle=None, plan_sentence=None, jwks=None):
    state, sentence = health(account, bundle=bundle)
    settings = {item.name: account.setting(item.name) for item in FIELDS[account.kind] if not item.secret}
    view = {'key': account.key, 'provider': account.kind, 'label': account.label, 'enabled': account.enabled,
            'settings': settings, 'secrets_set': list(account.secrets_set), 'missing': list(account.missing),
            'health': {'state': state, 'sentence': sentence}, 'plan': plan_sentence}
    if account.kind == 'hisp':
        view['certificate'] = _certificate_view(account)
        view['trust_bundle'] = None if bundle is None else {
            'anchors': bundle['anchors'], 'loaded_at': bundle['created_at'], 'source_url': bundle['source_url'],
            'loaded_by': bundle['created_by_name']}
    if account.kind == 'fhir':
        view['public_keys'] = jwks
    return view


def kinds_view():
    return [{'id': kind, 'label': LABELS[kind],
             'fields': [{'name': item.name, 'label': item.label, 'kind': item.kind, 'secret': item.secret,
                         'required': item.required, 'default': item.default, 'choices': list(item.choices),
                         'help': item.help} for item in FIELDS[kind]]} for kind in KINDS]
