"""Pure protocol checks; fixed signatures never come from the implementation.

SignalWire's published Scheme B vector is pinned in its official SDK tests:
https://github.com/signalwire/signalwire-python/blob/6bdf2ebee27e591690cf67dd2b1ed75218f798d2/tests/unit/security/test_webhook_validator.py#L43
Other fixtures were derived from manually framed UTF-8 messages with OpenSSL.
All tokens, URLs, IDs, and file bytes below are public or synthetic fixtures.
"""
import importlib
import importlib.util

import pytest


def verifier(name):
    # Delayed lookup lets a missing new module produce an assertion RED,
    # without a collection error or importing the application's runtime.
    assert importlib.util.find_spec('api.app.provider_signatures') is not None
    return getattr(importlib.import_module('api.app.provider_signatures'), name)


PHAXIO_URL = 'https://faxbot.invalid/phaxio?job_id=local-42&tag=a%2Bb'
PHAXIO_FIELDS = [('tag', 'one'), ('fax', '{"id":42,"status":"success"}'),
                 ('blank', ''), ('note', 'Résumé'), ('tag', 'two'),
                 ('event_type', 'fax_completed')]
PHAXIO_FILES = [('zfile', b'\x00\xffPDF\r\n'), ('file', b'abc'), ('file', b'')]
PHAXIO_SIGNATURE = '17380b2ea7a000686d57ab9718e2019761042ac3'
PHAXIO_TOKEN = 'synthetic-callback-token'

SW_URL = 'https://mycompany.com/myapp.php?foo=1&bar=2'
SW_FIELDS = [('To', '+18005551212'), ('From', '+14158675309'),
             ('CallSid', 'CA1234567890ABCDE'), ('Digits', '1234'),
             ('Caller', '+14158675309')]
SW_SIGNATURE = 'RSOYDt4T1cUTdK1PDd93/VVr8B8='
SW_DUPLICATE_URL = 'https://faxbot.invalid/signalwire?job_id=local-42&tag=a%2Bb'
SW_DUPLICATE_FIELDS = [('To', '+15550002222'), ('Blank', ''), ('Tag', 'one'),
                       ('From', '+15550001111'), ('Tag', 'two'), ('Note', 'café')]
SW_DUPLICATE_SIGNATURE = '1bkuECk4Sxedex4pclwDTpr+dBE='
SW_TOKEN = 'synthetic-signing-key'


def test_signalwire_accepts_the_official_published_compatibility_vector():
    verify = verifier('verify_signalwire_signature')
    assert verify('12345', SW_URL, SW_FIELDS, SW_SIGNATURE)


@pytest.mark.parametrize('url', [SW_URL, SW_URL.replace('.com/', '.com:443/')])
def test_signalwire_accepts_only_documented_default_https_port_equivalence(url):
    assert verifier('verify_signalwire_signature')('12345', url, SW_FIELDS, SW_SIGNATURE)


@pytest.mark.parametrize('url', ['http://faxbot.invalid/callback', 'http://faxbot.invalid:80/callback'])
def test_signalwire_accepts_default_http_port_equivalence(url):
    assert verifier('verify_signalwire_signature')(SW_TOKEN, url,
        [('FaxStatus', 'delivered'), ('FaxSid', 'remote-42')], 'hjGaKU6jnoQ9tH8OUqlB7xDTpec=')


def test_signalwire_keeps_an_explicit_nonstandard_port_bound_to_the_signature():
    verify = verifier('verify_signalwire_signature')
    fields = [('FaxStatus', 'delivered'), ('FaxSid', 'remote-42')]
    signature = 'W0G630b8HlLXuB8xjRfiF2x0eTU='
    assert verify(SW_TOKEN, 'https://faxbot.invalid:8443/callback', fields, signature)
    assert not verify(SW_TOKEN, 'https://faxbot.invalid/callback', fields, signature)
    assert not verify(SW_TOKEN, 'https://faxbot.invalid:8444/callback', fields, signature)


def test_signalwire_preserves_blank_and_repeated_fields_without_mutation():
    verify = verifier('verify_signalwire_signature')
    fields = list(SW_DUPLICATE_FIELDS)
    assert verify(SW_TOKEN, SW_DUPLICATE_URL, fields, SW_DUPLICATE_SIGNATURE)
    assert fields == SW_DUPLICATE_FIELDS
    # Reordering unlike keys is harmless; repeated values retain wire order.
    assert verify(SW_TOKEN, SW_DUPLICATE_URL, fields[1:] + fields[:1], SW_DUPLICATE_SIGNATURE)
    assert not verify(SW_TOKEN, SW_DUPLICATE_URL,
        [(name, value) for name, value in fields if name != 'Blank'], SW_DUPLICATE_SIGNATURE)
    assert not verify(SW_TOKEN, SW_DUPLICATE_URL,
        [(name, value) for name, value in fields if name != 'Tag'] + [('Tag', 'two'), ('Tag', 'one')],
        SW_DUPLICATE_SIGNATURE)


def test_signalwire_rejects_a_changed_key_public_query_or_form_value():
    verify = verifier('verify_signalwire_signature')
    assert not verify('other-synthetic-key', SW_URL, SW_FIELDS, SW_SIGNATURE)
    assert not verify('12345', SW_URL.replace('foo=1', 'foo=2'), SW_FIELDS, SW_SIGNATURE)
    assert not verify('12345', SW_URL, SW_FIELDS + [('Extra', 'value')], SW_SIGNATURE)


def test_phaxio_verifies_the_fixed_multipart_digest_without_mutation():
    verify = verifier('verify_phaxio_signature')
    fields, files = list(PHAXIO_FIELDS), list(PHAXIO_FILES)
    assert verify(PHAXIO_TOKEN, PHAXIO_URL, fields, files, PHAXIO_SIGNATURE)
    assert fields == PHAXIO_FIELDS
    assert files == PHAXIO_FILES
    reordered = fields[:1] + fields[2:3] + fields[1:2] + fields[3:]
    assert verify(PHAXIO_TOKEN, PHAXIO_URL, reordered, files, PHAXIO_SIGNATURE)


def test_phaxio_keeps_token_url_query_and_binary_file_content_bound():
    verify = verifier('verify_phaxio_signature')
    assert not verify('synthetic-api-secret', PHAXIO_URL, PHAXIO_FIELDS, PHAXIO_FILES, PHAXIO_SIGNATURE)
    assert not verify(PHAXIO_TOKEN, PHAXIO_URL.replace('local-42', 'local-43'),
                      PHAXIO_FIELDS, PHAXIO_FILES, PHAXIO_SIGNATURE)
    assert not verify(PHAXIO_TOKEN, PHAXIO_URL, PHAXIO_FIELDS,
                      [('zfile', b'\x00\xfePDF\r\n')] + PHAXIO_FILES[1:], PHAXIO_SIGNATURE)


def test_phaxio_preserves_blank_and_duplicate_field_and_file_occurrences():
    verify = verifier('verify_phaxio_signature')
    assert not verify(PHAXIO_TOKEN, PHAXIO_URL,
        [(name, value) for name, value in PHAXIO_FIELDS if name != 'blank'], PHAXIO_FILES, PHAXIO_SIGNATURE)
    assert not verify(PHAXIO_TOKEN, PHAXIO_URL,
        [(name, value) for name, value in PHAXIO_FIELDS if name != 'tag'] + [('tag', 'two'), ('tag', 'one')],
        PHAXIO_FILES, PHAXIO_SIGNATURE)
    assert not verify(PHAXIO_TOKEN, PHAXIO_URL, PHAXIO_FIELDS,
        [PHAXIO_FILES[0], PHAXIO_FILES[2], PHAXIO_FILES[1]], PHAXIO_SIGNATURE)


def test_phaxio_can_verify_empty_parameter_and_file_sequences():
    assert verifier('verify_phaxio_signature')(PHAXIO_TOKEN, 'https://faxbot.invalid/phaxio', (), (),
        'b6df9c7843e71e0f87617d245709f63bb5b47d4f')


@pytest.mark.parametrize('name, signature, fields, files', [
    ('verify_phaxio_signature', PHAXIO_SIGNATURE, PHAXIO_FIELDS, PHAXIO_FILES),
    ('verify_signalwire_signature', SW_DUPLICATE_SIGNATURE, SW_DUPLICATE_FIELDS, None),
])
def test_malformed_inputs_fail_closed_without_exposing_an_exception(name, signature, fields, files):
    verify = verifier(name)
    def check(token, url, supplied_fields, supplied_signature, supplied_files=files):
        args = (token, url, supplied_fields)
        if name == 'verify_phaxio_signature':
            args += (supplied_files,)
        return verify(*args, supplied_signature)
    url = PHAXIO_URL if files is not None else SW_DUPLICATE_URL
    token = PHAXIO_TOKEN if files is not None else SW_TOKEN
    for invalid_signature in (None, b'not-text', '', signature[:-1], signature + '\n', 'é' * len(signature)):
        assert check(token, url, fields, invalid_signature) is False
    for invalid_token in ('', None, b'not-text', '\ud800'):
        assert check(invalid_token, url, fields, signature) is False
    for invalid_url in ('', None, 'ftp://faxbot.invalid/callback', 'https:///callback',
                        'https://faxbot.invalid:bad/callback', 'https://faxbot.invalid:99999/callback',
                        'https://faxbot.invalid/\ncallback', 'https://faxbot.invalid/\ud800'):
        assert check(token, invalid_url, fields, signature) is False
    for invalid_fields in (None, {'field': 'value'}, 'ab', [('field',)],
                           ['ab'], [('field', 1)], [('field', '\ud800')]):
        assert check(token, url, invalid_fields, signature) is False
    if files is not None:
        for invalid_files in (None, {'file': b'abc'}, [('file', 'not-bytes')], [('file', bytearray(b'abc'))]):
            assert check(token, url, fields, signature, invalid_files) is False


def test_hex_and_base64_signatures_are_not_interchangeable_or_trimmed():
    assert not verifier('verify_phaxio_signature')(PHAXIO_TOKEN, PHAXIO_URL, PHAXIO_FIELDS, PHAXIO_FILES,
                                                  'FzgLLqegAGhtV6uXGOIBl2EEKsM=')
    assert not verifier('verify_phaxio_signature')(PHAXIO_TOKEN, PHAXIO_URL, PHAXIO_FIELDS, PHAXIO_FILES,
                                                  PHAXIO_SIGNATURE.upper())
    assert not verifier('verify_signalwire_signature')(SW_TOKEN, SW_DUPLICATE_URL, SW_DUPLICATE_FIELDS,
                                                       'd5b92e1029384b179d7b1e29725c034e9afe7411')
    assert not verifier('verify_signalwire_signature')(SW_TOKEN, SW_DUPLICATE_URL, SW_DUPLICATE_FIELDS,
                                                       SW_DUPLICATE_SIGNATURE[:-2] + 'F=')


@pytest.mark.parametrize('url, phaxio_signature, signalwire_signature', [
    ('https://user:pass@faxbot.invalid/callback',
     '59098e0d66041768f89cb5dde6bbaabcf69c5c43', 'vL0MsVbFLfuO/ALk+YLciEp62Gk='),
    ('https://user@faxbot.invalid/callback',
     '2a3ba5291f2ccdbc0330b74bfbc69bf57f4c1213', 'JItCoat5p8WzR2HOG3onvaznmpg='),
    ('https://faxbot.invalid/callback#fragment',
     '5c8c964f896e1e5aff8be3a2623293f0bebdec3c', 'LF806DZXRKSkYNcWk63xkwRtFQo='),
    ('https://faxbot.invalid/callback#',
     'e86357da98cbb05b2bab9092115e3a3a755ef3d4', 'Jpq0FJhvebggJ9Wzg+WwBG+lWX8='),
])
@pytest.mark.parametrize('name', ['verify_phaxio_signature', 'verify_signalwire_signature'])
def test_public_callback_url_rejects_userinfo_or_fragment_even_with_a_matching_digest(
        name, url, phaxio_signature, signalwire_signature):
    fields = [('FaxSid', 'remote-42')]
    if name == 'verify_phaxio_signature':
        assert not verifier(name)(PHAXIO_TOKEN, url, fields, (), phaxio_signature)
    else:
        assert not verifier(name)(SW_TOKEN, url, fields, signalwire_signature)
