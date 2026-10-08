"""A connector's settings: validated, with presets for Microsoft 365 and Google Workspace.

Secrets never live here; they are sealed separately (``store.SourceSecrets``).

Mail services, as published (checked 2026-10-07):

- Microsoft 365: IMAP outlook.office365.com:993, SMTP smtp.office365.com:587
  (STARTTLS). Basic authentication (passwords and app passwords) is off for
  IMAP in every tenant since October 2022, so Faxbot signs in with OAuth 2.0
  (XOAUTH2) as an app: client credentials for scope
  ``https://outlook.office365.com/.default``, with IMAP.AccessAsApp (and
  SMTP.SendAsApp for replies) granted in Entra, then New-ServicePrincipal and
  Add-MailboxPermission in Exchange Online PowerShell. Microsoft's
  Authentication-Results header carries no server name.
  learn.microsoft.com/exchange/client-developer/legacy-protocols/how-to-authenticate-an-imap-pop-smtp-application-by-using-oauth
  learn.microsoft.com/exchange/clients-and-mobile-in-exchange-online/deprecation-of-basic-authentication-exchange-online
  learn.microsoft.com/defender-office-365/message-headers-eop-mdo
- Google Workspace: IMAP imap.gmail.com:993, SMTP smtp.gmail.com:465 (TLS).
  Plain passwords stopped working for IMAP and SMTP on March 14, 2025; an app
  password works only with 2-Step Verification. Faxbot signs in as a service
  account with domain-wide delegation (scope ``https://mail.google.com/``, the
  mailbox as ``sub``), or with an app password. Gmail's Authentication-Results
  header is written by mx.google.com.
  knowledge.workspace.google.com/admin/sync/transition-from-less-secure-apps-to-oauth
  developers.google.com/identity/protocols/oauth2/service-account
  developers.google.com/workspace/gmail/imap/xoauth2-protocol
"""
import json
import os
import re

from .mail import MICROSOFT_365


class SourceInputError(ValueError):
    """One plain sentence about what to fix."""


KINDS = ('email', 'folder')
DIRECTIONS = ('receive', 'send')
PROVIDERS = ('microsoft365', 'google', 'other')
SIGN_INS = ('password', 'microsoft_app', 'google_service_account', 'oauth_refresh')
SECURITY = ('tls', 'starttls')
PRESETS = {
    'microsoft365': {'imap_host': 'outlook.office365.com', 'imap_port': 993, 'smtp_host': 'smtp.office365.com',
                     'smtp_port': 587, 'smtp_security': 'starttls', 'sign_in': 'microsoft_app',
                     'checked_by': MICROSOFT_365},
    'google': {'imap_host': 'imap.gmail.com', 'imap_port': 993, 'smtp_host': 'smtp.gmail.com', 'smtp_port': 465,
               'smtp_security': 'tls', 'sign_in': 'google_service_account', 'checked_by': 'mx.google.com'},
    'other': {'imap_port': 993, 'smtp_port': 587, 'smtp_security': 'starttls', 'sign_in': 'password'},
}
# What each sign-in needs sealed, by field name.
SECRET_FIELDS = {
    'password': ('password',),
    'microsoft_app': ('client_secret',),
    'google_service_account': ('service_account',),
    'oauth_refresh': ('client_secret', 'refresh_token'),
}
DEFAULT_CHECK_SECONDS = {'email': 60, 'folder': 15}
_HOST = re.compile(r'[A-Za-z0-9.-]{1,253}')
_ADDRESS = re.compile(r'[^@\s<>"]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,63}')
_FOLDER = re.compile(r'[^\x00-\x1f\x7f"\\]{1,200}')
_SERVER = re.compile(r'[A-Za-z0-9.-]{1,253}')
_TENANT = re.compile(r'[A-Za-z0-9.-]{1,100}')
_CLIENT = re.compile(r'[A-Za-z0-9._~-]{1,200}')


def _text(data, name, limit, *, required=False, default=''):
    value = data.get(name, default)
    if value is None:
        value = default
    if not isinstance(value, str):
        raise SourceInputError(f'{name.replace("_", " ").capitalize()} must be text.')
    value = value.strip()
    if required and not value:
        raise SourceInputError(f'Enter the {name.replace("_", " ")}.')
    if len(value) > limit or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise SourceInputError(f'The {name.replace("_", " ")} can be up to {limit} characters of plain text.')
    return value


def _number(data, name, low, high, default):
    value = data.get(name, default)
    if value is None:
        value = default
    if type(value) is not int or not low <= value <= high:
        raise SourceInputError(f'The {name.replace("_", " ")} must be a whole number from {low} to {high}.')
    return value


def _choice(data, name, choices, default):
    value = data.get(name, default) or default
    if value not in choices:
        raise SourceInputError(f'The {name.replace("_", " ")} must be one of: {", ".join(choices)}.')
    return value


def address(value, what='email address'):
    if not isinstance(value, str) or not _ADDRESS.fullmatch(value.strip()):
        raise SourceInputError(f'Enter a complete {what}, such as fax@example.com.')
    return value.strip()


def validate(kind, direction, data):
    """The stored settings for one connector, with defaults filled in; raises SourceInputError."""
    if kind not in KINDS:
        raise SourceInputError('Choose an email mailbox or a folder.')
    if direction not in DIRECTIONS:
        raise SourceInputError('Choose whether the connector brings documents in or sends faxes.')
    if not isinstance(data, dict):
        raise SourceInputError('The connector settings are missing.')
    settings = {'check_seconds': _number(data, 'check_seconds', 10, 86400, DEFAULT_CHECK_SECONDS[kind])}
    if direction == 'receive':
        mailbox = _text(data, 'mailbox_id', 40)
        settings['mailbox_id'] = mailbox or None
    if kind == 'folder':
        path = _text(data, 'path', 400, required=True)
        if not os.path.isabs(path) or '\x00' in path or os.path.normpath(path) != path.rstrip('/') or path == '/':
            raise SourceInputError('Enter the folder as a full path inside the Faxbot container, such as /scans.')
        settings.update(path=os.path.normpath(path), settle_seconds=_number(data, 'settle_seconds', 2, 3600, 10))
        if direction == 'send':
            settings['sidecar_minutes'] = _number(data, 'sidecar_minutes', 1, 1440, 10)
        return settings
    provider = _choice(data, 'provider', PROVIDERS, 'other')
    preset = PRESETS[provider]
    mailbox_address = address(data.get('address'), 'mailbox address')
    settings.update(
        provider=provider, address=mailbox_address,
        username=_text(data, 'username', 320) or mailbox_address,
        imap_host=_host(data, 'imap_host', preset), imap_port=_number(data, 'imap_port', 1, 65535, preset['imap_port']),
        folder=_folder(data, 'folder', 'INBOX'),
        processed_folder=_folder(data, 'processed_folder', 'Faxbot processed'),
        sign_in=_choice(data, 'sign_in', SIGN_INS, preset['sign_in']))
    if settings['folder'].casefold() == settings['processed_folder'].casefold():
        raise SourceInputError('Choose a different folder for processed messages than the one Faxbot checks.')
    if settings['sign_in'] == 'microsoft_app':
        settings.update(tenant_id=_pattern(data, 'tenant_id', _TENANT, 'Enter the directory (tenant) ID.'),
                        client_id=_pattern(data, 'client_id', _CLIENT, 'Enter the application (client) ID.'))
    elif settings['sign_in'] == 'oauth_refresh':
        token_url = _text(data, 'token_url', 400, required=True)
        if not token_url.startswith('https://'):
            raise SourceInputError('The token address must start with https://.')
        settings.update(token_url=token_url,
                        client_id=_pattern(data, 'client_id', _CLIENT, 'Enter the application (client) ID.'))
    if direction == 'send':
        settings.update(smtp_host=_host(data, 'smtp_host', preset),
                        smtp_port=_number(data, 'smtp_port', 1, 65535, preset['smtp_port']),
                        smtp_security=_choice(data, 'smtp_security', SECURITY, preset['smtp_security']))
        checked_by = _text(data, 'checked_by', 253) or preset.get('checked_by', '')
        if checked_by != MICROSOFT_365 and not _SERVER.fullmatch(checked_by or '-'):
            raise SourceInputError('Enter the name your mail server writes when it checks senders, such as '
                                   'mx.example.com.')
        if not checked_by:
            raise SourceInputError('Enter the name your mail server writes when it checks senders, such as '
                                   'mx.google.com for Google Workspace.')
        settings['checked_by'] = checked_by.casefold() if checked_by != MICROSOFT_365 else checked_by
    return settings


def _host(data, name, preset):
    value = _text(data, name, 253) or preset.get(name, '')
    if not value or not _HOST.fullmatch(value):
        raise SourceInputError(f'Enter the {"mail" if name == "imap_host" else "outgoing mail"} server address, '
                               'such as mail.example.com.')
    return value.casefold()


def _folder(data, name, default):
    value = _text(data, name, 200) or default
    if not _FOLDER.fullmatch(value):
        raise SourceInputError(f'The {name.replace("_", " ")} name cannot hold quotes, backslashes or control '
                               'characters.')
    return value


def _pattern(data, name, pattern, message):
    value = _text(data, name, 200)
    if not pattern.fullmatch(value or ''):
        raise SourceInputError(message)
    return value


def secrets(sign_in, supplied, current=None):
    """The sealed fields for a sign-in: supplied values replace stored ones; missing ones are refused."""
    current = dict(current or {})
    supplied = supplied or {}
    if not isinstance(supplied, dict):
        raise SourceInputError('The sign-in details are missing.')
    result = {}
    for name in SECRET_FIELDS[sign_in]:
        value = supplied.get(name)
        if value in (None, ''):
            value = current.get(name)
        if not isinstance(value, str) or not value.strip():
            raise SourceInputError(_MISSING[name])
        if len(value) > 16384:
            raise SourceInputError('A sign-in detail is too long.')
        result[name] = value.strip()
    if sign_in == 'google_service_account':
        service_account(result['service_account'])
    return result


_MISSING = {
    'password': 'Enter the mailbox password or app password.',
    'client_secret': 'Enter the client secret.',
    'service_account': 'Paste the service account key file (JSON).',
    'refresh_token': 'Enter the refresh token.',
}


def service_account(text):
    """The parts of a Google service account key Faxbot uses; raises SourceInputError."""
    try:
        data = json.loads(text)
    except ValueError:
        raise SourceInputError('The service account key is not valid JSON; paste the whole key file.') from None
    if (not isinstance(data, dict) or data.get('type') != 'service_account'
            or not isinstance(data.get('client_email'), str) or not isinstance(data.get('private_key'), str)
            or '-----BEGIN PRIVATE KEY-----' not in data['private_key']):
        raise SourceInputError('This is not a Google service account key file.')
    return {'client_email': data['client_email'], 'private_key': data['private_key'],
            'private_key_id': data.get('private_key_id') if isinstance(data.get('private_key_id'), str) else None}


def what_to_set(provider):
    """One sentence telling the administrator what the mail service needs before Faxbot can sign in."""
    return GUIDANCE.get(provider, GUIDANCE['other'])


GUIDANCE = {
    'microsoft365': ('Microsoft 365 turned off passwords for IMAP in 2022. In Microsoft Entra, register an app, give it '
                     'the Office 365 Exchange Online application permissions IMAP.AccessAsApp and SMTP.SendAsApp, and '
                     'grant admin consent. Then in Exchange Online PowerShell run New-ServicePrincipal for the app and '
                     'Add-MailboxPermission with FullAccess on this mailbox. Enter the tenant ID, client ID and a '
                     'client secret here. Mail your own staff send from inside your Microsoft 365 organization is '
                     'accepted by the internal mark Exchange gives it.'),
    'google': ('Google Workspace stopped accepting plain passwords on March 14, 2025. Create a service account, turn '
               'on domain-wide delegation for it with the scope https://mail.google.com/ in the Admin console under '
               'Security, API controls, and paste its key file here. An app password also works when the mailbox '
               'uses 2-Step Verification. Set up DKIM for your domain under Apps, Google Workspace, Gmail, '
               'Authenticate email, so mail from your own staff passes the sender check.'),
    'other': ('Enter your mail server\'s IMAP address and a user name and password. Faxbot connects with TLS only, on '
              'port 993 unless you choose another.'),
}
