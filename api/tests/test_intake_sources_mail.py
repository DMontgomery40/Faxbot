"""Reading email for intake connectors: identity, sender confirmation, fax numbers, loops, XOAUTH2 and sentences.

Synthetic messages only (example.com, example.org, 555 numbers).
"""
import base64
import json
import string

import pytest

from app.intake.sources import mail, text
from app.intake.sources.imap import Mailbox, SignInRefused, XOAuth2
from app.intake.sources.oauth import GOOGLE_TOKEN, Tokens, TokenUnavailable, google_assertion, xoauth2
from app.intake.sources.settings import SourceInputError, secrets, validate
from api.tests.imap_fake import FakeImap, client_context


def message(*, headers=(), subject='Fax +13035550100', sender='Jane Smith <jane@example.com>',
            message_id='<msg-1@example.com>', attachments=(('scan.pdf', 'application/pdf', b'%PDF-1.4 x'),)):
    from email.message import EmailMessage
    built = EmailMessage()
    for name, value in headers:
        built[name] = value
    if sender:
        built['From'] = sender
    built['To'] = 'fax@example.com'
    built['Subject'] = subject
    if message_id:
        built['Message-ID'] = message_id
    built.set_content('Please fax this.')
    for name, content_type, data in attachments:
        maintype, subtype = content_type.split('/')
        built.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    return built.as_bytes()


GMAIL = ('Authentication-Results', 'mx.google.com; dkim=pass header.i=@example.com header.s=s1 header.b=abc; '
         'spf=pass (google.com: domain of jane@example.com designates 192.0.2.1 as permitted sender) '
         'smtp.mailfrom=jane@example.com; dmarc=pass (p=REJECT sp=REJECT dis=NONE) header.from=example.com')
MICROSOFT = ('Authentication-Results', 'spf=pass (sender IP is 192.0.2.1) smtp.mailfrom=example.com; '
             'dkim=pass (signature was verified) header.d=example.com;dmarc=pass action=none '
             'header.from=example.com;compauth=pass reason=100')


# -- identity ---------------------------------------------------------------------------

def test_identity_is_the_message_id_or_the_whole_message():
    first, again = mail.parse(message()), mail.parse(message(subject='Changed subject'))
    assert first.operation_id == again.operation_id and first.operation_id.startswith('m:')
    assert len(first.operation_id) <= 100 and first.reference == '<msg-1@example.com>'
    bare = mail.parse(message(message_id=None))
    assert bare.operation_id.startswith('r:') and bare.reference is None
    assert mail.parse(message(message_id=None)).operation_id != mail.parse(message(message_id=None,
                                                                               subject='Other')).operation_id
    long = mail.parse(message(message_id='<' + 'x' * 400 + '@example.com>'))
    assert len(long.operation_id) == 66


def test_attachments_are_numbered_documents_and_sidecars_are_kept_apart():
    parsed = mail.parse(message(attachments=(
        ('one.pdf', 'application/pdf', b'%PDF-1.4 one'), ('two.tif', 'image/tiff', b'II*\x00'),
        ('two.json', 'application/json', b'{"to": "+13035550100"}'), ('notes.docx', 'application/octet-stream', b'x'))))
    assert [(item.name, item.kind, item.position) for item in parsed.documents] == [('one.pdf', 'pdf', 1),
                                                                                      ('two.tif', 'tiff', 2)]
    assert [item.stem for item in parsed.sidecars] == ['two'] and parsed.unreadable == ['notes.docx']


# -- sender confirmation ----------------------------------------------------------------

def test_gmail_and_microsoft_results_confirm_an_aligned_sender():
    gmail = mail.parse(message(headers=[GMAIL]))
    assert mail.confirmed(gmail, 'jane@example.com', 'mx.google.com')
    microsoft = mail.parse(message(headers=[MICROSOFT]))
    assert mail.confirmed(microsoft, 'jane@example.com', mail.MICROSOFT_365)
    # The configured server must be the one that wrote it.
    assert not mail.confirmed(gmail, 'jane@example.com', 'mx.example.org')
    assert not mail.confirmed(microsoft, 'jane@example.com', 'mx.google.com')
    assert not mail.confirmed(gmail, 'jane@example.com', '')


def test_only_the_topmost_header_from_the_named_server_counts():
    forged_below = mail.parse(message(headers=[
        ('Authentication-Results', 'mx.google.com; spf=fail smtp.mailfrom=example.com; dkim=none; dmarc=fail '
                                   'header.from=example.com'),
        ('Authentication-Results', 'mx.google.com; dmarc=pass header.from=example.com')]))
    assert not mail.confirmed(forged_below, 'jane@example.com', 'mx.google.com')
    other_on_top = mail.parse(message(headers=[
        ('Authentication-Results', 'relay.example.org; dmarc=pass header.from=example.com'),
        ('Authentication-Results', 'mx.google.com; dmarc=fail header.from=example.com')]))
    assert not mail.confirmed(other_on_top, 'jane@example.com', 'mx.google.com')
    # A forged header naming another server is ignored, wherever it sits.
    forged = mail.parse(message(headers=[('Authentication-Results',
                                          'mx.attacker.example; dmarc=pass header.from=example.com')]))
    assert not mail.confirmed(forged, 'jane@example.com', 'mx.google.com')


def test_alignment_spf_and_dkim():
    unaligned_spf = mail.parse(message(headers=[('Authentication-Results',
                                                 'mx.google.com; spf=pass smtp.mailfrom=bounce@example.org; '
                                                 'dkim=none; dmarc=none header.from=example.com')]))
    assert not mail.confirmed(unaligned_spf, 'jane@example.com', 'mx.google.com')
    subdomain_spf = mail.parse(message(headers=[('Authentication-Results',
                                                 'mx.google.com; spf=pass smtp.mailfrom=bounce@mail.example.com')]))
    assert mail.confirmed(subdomain_spf, 'jane@example.com', 'mx.google.com')
    other_dkim = mail.parse(message(headers=[('Authentication-Results',
                                              'mx.google.com; dkim=pass header.d=example.org; spf=softfail '
                                              'smtp.mailfrom=example.com')]))
    assert not mail.confirmed(other_dkim, 'jane@example.com', 'mx.google.com')
    other_dmarc = mail.parse(message(headers=[('Authentication-Results',
                                               'mx.google.com; dmarc=pass header.from=example.org')]))
    assert not mail.confirmed(other_dmarc, 'jane@example.com', 'mx.google.com')
    assert not mail.aligned('com', 'example.com') and mail.aligned('a.example.com', 'example.com')


INTERNAL = [('X-MS-Exchange-Organization-AuthAs', 'Internal'),
            ('X-MS-Exchange-Organization-AuthSource', 'SN6PR04MB4224.namprd04.prod.outlook.com'),
            ('X-MS-Exchange-Organization-AuthMechanism', '04')]
M365 = {'provider': 'microsoft365', 'imap_host': 'outlook.office365.com', 'checked_by': mail.MICROSOFT_365,
        'address': 'fax@example.com'}


def test_microsoft_365_internal_mail_is_confirmed_only_on_a_microsoft_365_connector():
    internal = mail.parse(message(headers=INTERNAL))
    assert mail.microsoft_internal(internal, 'jane@example.com', M365)
    # The same headers mean nothing on any other connector: any server could have written them.
    assert not mail.microsoft_internal(internal, 'jane@example.com', {**M365, 'provider': 'other'})
    assert not mail.microsoft_internal(internal, 'jane@example.com', {**M365, 'imap_host': 'mail.example.com'})
    assert not mail.microsoft_internal(internal, 'jane@example.com', {**M365, 'checked_by': 'mx.example.com'})
    # Only the mailbox's own domain, exactly one Internal mark and one source.
    assert not mail.microsoft_internal(internal, 'jane@example.org', M365)
    anonymous = mail.parse(message(headers=[('X-MS-Exchange-Organization-AuthAs', 'Anonymous'), *INTERNAL[1:]]))
    assert not mail.microsoft_internal(anonymous, 'jane@example.com', M365)
    doubled = mail.parse(message(headers=[('X-MS-Exchange-Organization-AuthAs', 'Internal'), *INTERNAL]))
    assert not mail.microsoft_internal(doubled, 'jane@example.com', M365)
    sourceless = mail.parse(message(headers=INTERNAL[:1]))
    assert not mail.microsoft_internal(sourceless, 'jane@example.com', M365)


def test_comments_with_separators_do_not_split_results():
    server, results = mail.parse_results('mx.google.com 1; spf=pass (a; b=c (nested; x)) smtp.mailfrom='
                                         '"jane@example.com"; dkim=fail (bad; sig) header.d=example.com')
    assert server == 'mx.google.com'
    assert results == [('spf', 'pass', {'smtp.mailfrom': 'jane@example.com'}),
                       ('dkim', 'fail', {'header.d': 'example.com'})]
    assert mail.parse_results('none')[1] == [] and mail.parse_results('')[0] is None


def test_sender_must_be_exactly_one_address():
    assert mail.sender(mail.parse(message())) == 'jane@example.com'
    assert mail.sender(mail.parse(message(sender='a@example.com, b@example.com'))) is None
    assert mail.sender(mail.parse(message(sender=None))) is None


# -- numbers ----------------------------------------------------------------------------

def test_fax_number_from_an_address_tag_or_the_subject():
    tagged = mail.parse(message(headers=[('Cc', 'fax+13035550199@example.com')], subject='Referral'))
    assert mail.tagged_number(tagged, 'fax@example.com', 'US') == '+13035550199'
    national = mail.parse(message(headers=[('Cc', 'Fax+3035550199@Example.com')], subject='x'))
    assert mail.tagged_number(national, 'fax@example.com', 'US') == '+13035550199'
    assert mail.tagged_number(tagged, 'other@example.com', 'US') is None
    assert mail.subject_number('Fax to +1 (303) 555-0100 please', 'US') == '+13035550100'
    assert mail.subject_number('Referral for Dana', 'US') is None
    with pytest.raises(ValueError, match='two numbers'):
        mail.subject_number('+13035550100 and +13035550111', 'US')
    with pytest.raises(ValueError):
        mail.subject_number('Call 0000000', 'US')
    assert mail.example_address('fax@example.com') == 'fax+13035550100@example.com'


# -- loops and automatic mail ---------------------------------------------------------------

def test_faxbot_own_mail_and_automatic_mail_are_recognized():
    assert mail.sent_by_faxbot(mail.parse(message(message_id='<intake-abc123@example.com>')))
    assert mail.sent_by_faxbot(mail.parse(message(message_id='<faxbot-reply-abc@example.com>')))
    assert mail.sent_by_faxbot(mail.parse(message(sender='fax@example.com')), ('FAX@example.com',))
    assert not mail.sent_by_faxbot(mail.parse(message()), ('fax@example.com',))
    assert mail.automatic(mail.parse(message(headers=[('Auto-Submitted', 'auto-replied')])))
    assert not mail.automatic(mail.parse(message(headers=[('Auto-Submitted', 'no')])))
    assert mail.may_reply(mail.parse(message()))
    for header in (('Return-Path', '<>'), ('List-Id', '<staff.example.com>'), ('Precedence', 'bulk'),
                   ('Auto-Submitted', 'auto-generated')):
        assert not mail.may_reply(mail.parse(message(headers=[header]))), header


# -- XOAUTH2 ----------------------------------------------------------------------------------

def test_xoauth2_framing_matches_the_published_example():
    # Microsoft's documented example: user=test@contoso.onmicrosoft.com, token EwBAAl3BAAUFFpUAo7J3Ve0bjLBWZWCclRC3EoAA.
    framed = xoauth2('test@contoso.onmicrosoft.com', 'EwBAAl3BAAUFFpUAo7J3Ve0bjLBWZWCclRC3EoAA')
    assert base64.b64encode(framed.encode()).decode() == (
        'dXNlcj10ZXN0QGNvbnRvc28ub25taWNyb3NvZnQuY29tAWF1dGg9QmVhcmVyIEV3QkFBbDNCQUFVRkZwVUFvN0ozVmUwYmpMQldaV0Nj'
        'bFJDM0VvQUEBAQ==')
    exchange = XOAuth2('a@example.com', 't')
    assert exchange(b'') == 'user=a@example.com\x01auth=Bearer t\x01\x01' and exchange(b'{"status":"401"}') == b''


def test_xoauth2_over_tls_signs_in_once_and_answers_an_error_with_an_empty_line(tmp_path):
    server = FakeImap(tmp_path, token='good-token')
    try:
        context = client_context(server.cert)
        good = Mailbox('localhost', server.port, ssl_context=context)
        good.sign_in_token('fax@example.com', 'good-token')
        good.close()
        assert server.xoauth2 == [b'user=fax@example.com\x01auth=Bearer good-token\x01\x01']
        bad = Mailbox('localhost', server.port, ssl_context=context)
        with pytest.raises(SignInRefused, match='did not accept the sign-in token'):
            bad.sign_in_token('fax@example.com', 'stale-token')
        bad.close()
        assert server.empty_answers == 1 and len(server.xoauth2) == 2
    finally:
        server.close()


def test_tls_is_required_and_verified(tmp_path):
    from app.intake.sources.imap import CannotReach
    server = FakeImap(tmp_path, password='pw')
    try:
        # The system's trusted certificates do not include the test certificate: no plaintext fallback.
        with pytest.raises(CannotReach, match='could not reach the mail server localhost'):
            Mailbox('localhost', server.port)
    finally:
        server.close()


def test_google_service_account_assertion_is_signed_for_the_mailbox():
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
    key = json.dumps({'type': 'service_account', 'client_email': 'faxbot@project.iam.gserviceaccount.com',
                      'private_key': pem, 'private_key_id': 'kid-1'})
    assertion = google_assertion(key, 'fax@example.com', now=1_800_000_000)
    header, claims, signature = assertion.split('.')
    pad = lambda value: value + '=' * (-len(value) % 4)  # noqa: E731
    assert json.loads(base64.urlsafe_b64decode(pad(header))) == {'alg': 'RS256', 'typ': 'JWT', 'kid': 'kid-1'}
    assert json.loads(base64.urlsafe_b64decode(pad(claims))) == {
        'iss': 'faxbot@project.iam.gserviceaccount.com', 'sub': 'fax@example.com', 'scope': 'https://mail.google.com/',
        'aud': GOOGLE_TOKEN, 'iat': 1_800_000_000, 'exp': 1_800_003_600}
    private.public_key().verify(base64.urlsafe_b64decode(pad(signature)), f'{header}.{claims}'.encode(),
                                padding.PKCS1v15(), hashes.SHA256())


def test_tokens_are_cached_and_a_refusal_is_one_plain_sentence():
    calls = []

    def post(url, data):
        calls.append((url, data))
        return (200, {'access_token': 'token-1', 'expires_in': 3600}) if len(calls) == 1 else (401, {'error': 'x'})
    tokens = Tokens(post=post, clock=lambda: 1000.0)
    settings = {'sign_in': 'microsoft_app', 'tenant_id': 'contoso.onmicrosoft.com', 'client_id': 'client-1'}
    assert tokens.token('s1', settings, {'client_secret': 'secret-1'}) == 'token-1'
    assert tokens.token('s1', settings, {'client_secret': 'secret-1'}) == 'token-1'
    assert calls[0][0] == 'https://login.microsoftonline.com/contoso.onmicrosoft.com/oauth2/v2.0/token'
    assert calls[0][1]['scope'] == 'https://outlook.office365.com/.default' and len(calls) == 1
    tokens.forget('s1')
    with pytest.raises(TokenUnavailable) as refused:
        tokens.token('s1', settings, {'client_secret': 'secret-1'})
    assert str(refused.value) == 'the sign-in service refused the app, key or secret.'
    assert 'secret-1' not in str(refused.value)


# -- settings ---------------------------------------------------------------------------------

def test_presets_fill_in_each_mail_service():
    microsoft = validate('email', 'send', {'provider': 'microsoft365', 'address': 'fax@example.com',
                                           'tenant_id': 'contoso.onmicrosoft.com', 'client_id': 'abc-123'})
    assert (microsoft['imap_host'], microsoft['smtp_host'], microsoft['sign_in'], microsoft['checked_by']) == (
        'outlook.office365.com', 'smtp.office365.com', 'microsoft_app', mail.MICROSOFT_365)
    google = validate('email', 'receive', {'provider': 'google', 'address': 'scans@example.com',
                                           'sign_in': 'password'})
    assert google['imap_host'] == 'imap.gmail.com' and google['username'] == 'scans@example.com'
    with pytest.raises(SourceInputError, match='full path inside the Faxbot container'):
        validate('folder', 'receive', {'path': 'scans'})
    with pytest.raises(SourceInputError, match='different folder'):
        validate('email', 'receive', {'address': 'a@example.com', 'imap_host': 'mail.example.com',
                                      'processed_folder': 'inbox'})
    with pytest.raises(SourceInputError, match='Enter the mailbox password'):
        secrets('password', {})
    with pytest.raises(SourceInputError, match='not a Google service account key'):
        secrets('google_service_account', {'service_account': '{"type": "user"}'})


# -- every explanation sentence --------------------------------------------------------------

def test_every_sentence_formats_and_reads_as_one_plain_sentence():
    values = {'mailbox': 'Front desk', 'number': '+13035550100', 'count': 'twice', 'address': 'jane@example.com',
              'person': 'Jane Smith', 'example': 'fax+13035550100@example.com', 'text': '555', 'name': 'scan.pdf',
              'limit': 20, 'detail': 'Fix it.', 'minutes': 10, 'host': 'mail.example.com', 'folder': 'INBOX',
              'path': '/scans', 'reason': 'It was refused.', 'subject': 'Referral', 'pages': ', 2 pages'}
    sentences = {name: value for name, value in vars(text).items() if name.isupper() and isinstance(value, str)}
    assert len(sentences) > 50
    for name, sentence in sentences.items():
        fields = {field for _, field, _, _ in string.Formatter().parse(sentence) if field}
        rendered = sentence.format(**{field: values[field] for field in fields})
        assert '{' not in rendered and '  ' not in rendered, name
        if not name.startswith('REPLY_SUBJECT'):
            assert rendered[-1] in '.)' or rendered.endswith(': It was refused.'), name
        for word in ('canonical', 'durably', 'UUID', 'true', 'false', 'None', 'null', 'XOAUTH2', 'IMAP4'):
            assert word not in rendered.split(), (name, word)
    assert text.times(1) == 'once' and text.times(2) == 'twice' and text.times(5) == '5 times'
    assert text.pages_phrase(1) == ', 1 page' and text.pages_phrase(None) == ''
