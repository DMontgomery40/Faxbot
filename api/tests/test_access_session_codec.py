"""Pure session crypto/ownership contracts; no database, HTTP or app runtime."""
import base64
import copy
import dataclasses
import gc
import hmac
import importlib
import json
import pickle
import weakref

import pytest


KEY = bytes(range(32))
TOKEN = 'fbs_' + 'A' * 43
TOKEN_HASH = '1922f960bddf4e5e7a740875d1be46fcb81e312806060c8ddcbcbcc32d9eb5b0'
CSRF = 'BTlAsAzgAFmj6J44rHc0h8nE-sWnGUI7JGlEqHxQ9AM'
CSRF_HASH = '89ff321148be0db86fd4459eb05db234a46bbb7a0adc960df0091efb7d250bd8'


def module():
    name = 'api.app.access.session_codec'
    assert importlib.util.find_spec(name) is not None, 'bounded session codec is missing'
    return importlib.import_module(name)


def assert_safe_error(error, code):
    assert str(error) == code
    assert error.args == (code,)
    assert error.__cause__ is None
    assert error.__context__ is None


def test_known_vectors_and_same_key_service_roundtrip():
    c = module()
    first = c.SessionCodec(installation_key=KEY)
    second = c.SessionCodec(installation_key=KEY)
    assert first.token_hash(TOKEN) == second.token_hash(TOKEN) == TOKEN_HASH
    assert first.csrf_value(TOKEN) == second.csrf_value(TOKEN) == CSRF
    assert first.csrf_hash(CSRF) == second.csrf_hash(CSRF) == CSRF_HASH
    assert first.verify_csrf(TOKEN, CSRF, CSRF_HASH)
    assert second.verify_csrf(TOKEN, CSRF, CSRF_HASH)


def test_installation_keys_and_bootstrap_domain_cannot_verify_session_material():
    c = module()
    codec = c.SessionCodec(installation_key=KEY)
    other = c.SessionCodec(installation_key=b'\xff' * 32)
    assert other.token_hash(TOKEN) == '743821d279eaf6dcc6f653502ff5db42446a65f6632d3ce2b8d181295f1ab7a7'
    assert other.csrf_value(TOKEN) != CSRF
    assert other.csrf_hash(CSRF) != CSRF_HASH
    assert not other.verify_csrf(TOKEN, CSRF, CSRF_HASH)
    # The literal vectors pin all three session domains. This additionally
    # rules out substituting the actual bootstrap fingerprint domain.
    bootstrap_hash = hmac.digest(KEY, b'faxbot/access/bootstrap/v1\0' + TOKEN.encode('ascii'), 'sha256').hex()
    assert codec.token_hash(TOKEN) != bootstrap_hash
    csrf_digest = base64.urlsafe_b64decode(CSRF + '=')
    assert bytes.fromhex(TOKEN_HASH) != csrf_digest != bytes.fromhex(CSRF_HASH)


def test_prepare_uses_independent_random_token_and_row_id(monkeypatch):
    c = module()
    blocks = iter([(32, b'\0' * 32), (16, b'\xff' * 16)])

    def random_bytes(length):
        expected_length, result = next(blocks)
        assert length == expected_length
        return result

    monkeypatch.setattr(c.secrets, 'token_bytes', random_bytes)
    codec = c.SessionCodec(installation_key=KEY)
    prepared = codec.prepare()
    stored = codec.storage(prepared)
    assert type(prepared) is c.PreparedSession
    assert type(stored) is c.SessionStorage
    assert prepared._token_for_committed_adapter() == TOKEN
    assert prepared._csrf_for_committed_adapter() == CSRF
    assert stored.id == 'f' * 32
    assert stored.token_hash == TOKEN_HASH
    assert stored.csrf_hash == CSRF_HASH
    assert codec.verify_csrf(TOKEN, CSRF, stored.csrf_hash)
    assert list(blocks) == []


def test_real_preparations_have_distinct_valid_tokens_and_ids():
    c = module()
    codec = c.SessionCodec(installation_key=KEY)
    first, second = codec.prepare(), codec.prepare()
    tokens = [item._token_for_committed_adapter() for item in (first, second)]
    rows = [codec.storage(item) for item in (first, second)]
    assert tokens[0] != tokens[1]
    assert rows[0].id != rows[1].id
    for prepared, token, row in zip((first, second), tokens, rows):
        assert len(token) == 47
        assert len(base64.urlsafe_b64decode(token[4:] + '=')) == 32
        assert len(row.id) == 32 and row.id == bytes.fromhex(row.id).hex()
        assert row.token_hash == codec.token_hash(token)
        assert codec.verify_csrf(token, prepared._csrf_for_committed_adapter(), row.csrf_hash)


class HostileString(str):
    def __len__(self):
        raise AssertionError('wrong type must reject before user-defined work')


class BytesSubclass(bytes):
    pass


@pytest.mark.parametrize('key', [
    None, '', 'x' * 32, bytearray(32), memoryview(b'x' * 32),
    b'', b'x' * 31, b'x' * 33, b'x' * 1_000_000,
    base64.urlsafe_b64encode(KEY), BytesSubclass(KEY),
])
def test_installation_key_requires_exact_raw_32_bytes(key):
    c = module()
    with pytest.raises(c.AccessUnavailableError) as caught:
        c.SessionCodec(installation_key=key)
    assert_safe_error(caught.value, 'access_unavailable')


BAD_TOKENS = [
    None, b'fbs_' + b'A' * 43, 47, [], {}, HostileString(TOKEN),
    '', 'fbs_' + 'A' * 42, 'fbs_' + 'A' * 44, TOKEN + '=', TOKEN + '\n',
    'FBS_' + 'A' * 43, 'fbk_' + 'A' * 43, 'fbs_' + 'A' * 42 + '/',
    'fbs_' + 'A' * 42 + '+', 'fbs_' + 'A' * 42 + ' ',
    'fbs_' + 'A' * 42 + 'é', 'fbs_' + 'A' * 42 + '\ud800',
    'fbs_' + '😀' * 43, 'x' * 1_000_000,
]


@pytest.mark.parametrize('token', BAD_TOKENS)
def test_malformed_token_rejects_before_decode_or_hmac(token, monkeypatch):
    c = module()
    codec = c.SessionCodec(installation_key=KEY)

    def forbidden(*args, **kwargs):
        pytest.fail('malformed token reached decoding or cryptography')

    monkeypatch.setattr(base64, 'b64decode', forbidden)
    monkeypatch.setattr(hmac, 'digest', forbidden)
    monkeypatch.setattr(hmac, 'compare_digest', forbidden)
    for operation in (codec.token_hash, codec.csrf_value):
        with pytest.raises(c.AuthenticationError) as caught:
            operation(token)
        assert_safe_error(caught.value, 'unauthenticated')
    assert not codec.verify_csrf(token, CSRF, CSRF_HASH)


BAD_CSRF = [
    None, CSRF.encode('ascii'), 43, [], {}, HostileString(CSRF),
    '', 'A' * 42, 'A' * 44, CSRF + '=', CSRF + '\n',
    'A' * 42 + '/', 'A' * 42 + '+', 'A' * 42 + ' ',
    'A' * 42 + 'é', 'A' * 42 + '\ud800', '😀' * 43, 'x' * 1_000_000,
]


@pytest.mark.parametrize('csrf', BAD_CSRF)
def test_malformed_csrf_rejects_before_hmac(csrf, monkeypatch):
    c = module()
    codec = c.SessionCodec(installation_key=KEY)

    def forbidden(*args, **kwargs):
        pytest.fail('malformed CSRF reached cryptography')

    monkeypatch.setattr(hmac, 'digest', forbidden)
    monkeypatch.setattr(hmac, 'compare_digest', forbidden)
    with pytest.raises(c.AuthenticationError) as caught:
        codec.csrf_hash(csrf)
    assert_safe_error(caught.value, 'unauthenticated')
    assert not codec.verify_csrf(TOKEN, csrf, CSRF_HASH)


@pytest.mark.parametrize('tail', ['B', 'C', 'D'])
def test_token_alternate_pad_bits_cannot_authenticate_same_decoded_secret(tail, monkeypatch):
    c = module()
    alternate = TOKEN[:-1] + tail
    assert base64.urlsafe_b64decode(alternate[4:] + '=') == b'\0' * 32

    def forbidden(*args, **kwargs):
        pytest.fail('noncanonical token reached cryptography')

    monkeypatch.setattr(hmac, 'digest', forbidden)
    codec = c.SessionCodec(installation_key=KEY)
    for operation in (codec.token_hash, codec.csrf_value):
        with pytest.raises(c.AuthenticationError):
            operation(alternate)
    assert not codec.verify_csrf(alternate, CSRF, CSRF_HASH)


@pytest.mark.parametrize('tail', ['N', 'O', 'P'])
def test_csrf_alternate_pad_bits_cannot_verify_same_decoded_value(tail, monkeypatch):
    c = module()
    alternate = CSRF[:-1] + tail
    assert base64.urlsafe_b64decode(alternate + '=') == base64.urlsafe_b64decode(CSRF + '=')

    def forbidden(*args, **kwargs):
        pytest.fail('noncanonical CSRF reached cryptography')

    monkeypatch.setattr(hmac, 'digest', forbidden)
    codec = c.SessionCodec(installation_key=KEY)
    with pytest.raises(c.AuthenticationError):
        codec.csrf_hash(alternate)
    assert not codec.verify_csrf(TOKEN, alternate, CSRF_HASH)


@pytest.mark.parametrize('stored', [
    None, CSRF_HASH.encode('ascii'), 64, [], {}, HostileString(CSRF_HASH),
    '', '0' * 63, '0' * 65, CSRF_HASH.upper(), CSRF_HASH + '\n',
    'g' * 64, 'é' * 64, 'x' * 1_000_000,
])
def test_invalid_stored_hash_is_false_before_crypto(stored, monkeypatch):
    c = module()
    codec = c.SessionCodec(installation_key=KEY)

    def forbidden(*args, **kwargs):
        pytest.fail('malformed stored hash reached cryptography')

    monkeypatch.setattr(hmac, 'digest', forbidden)
    monkeypatch.setattr(hmac, 'compare_digest', forbidden)
    assert not codec.verify_csrf(TOKEN, CSRF, stored)


@pytest.mark.parametrize('supplied,stored,expected', [
    (CSRF, CSRF_HASH, True), ('A' * 43, CSRF_HASH, False),
    (CSRF, '0' * 64, False), ('A' * 43, '0' * 64, False),
    ('A' * 43, '6ef81473e82768d632e1e5dfa8ba748de6abfda7b65865a6b9831c0722fdbdbf', False),
])
def test_valid_syntax_always_compares_both_csrf_constraints(supplied, stored, expected, monkeypatch):
    c = module()
    codec = c.SessionCodec(installation_key=KEY)
    compare, comparisons = hmac.compare_digest, []

    def record(left, right):
        comparisons.append((left, right))
        return compare(left, right)

    monkeypatch.setattr(hmac, 'compare_digest', record)
    assert codec.verify_csrf(TOKEN, supplied, stored) is expected
    assert len(comparisons) == 2
    assert any({left, right} == {supplied, CSRF} for left, right in comparisons)
    assert any(stored in (left, right) and len(left) == len(right) == 64
               for left, right in comparisons)


def test_private_preparation_and_storage_reject_generic_disclosure_and_mutation():
    c = module()
    codec = c.SessionCodec(installation_key=KEY)
    prepared = codec.prepare()
    stored = codec.storage(prepared)
    for item in (prepared, stored):
        assert repr(item) == type(item).__name__ + '()'
        assert not dataclasses.is_dataclass(item)
        for serializer in (dataclasses.asdict, json.dumps, pickle.dumps, copy.copy,
                           copy.deepcopy, dict, vars):
            with pytest.raises(TypeError):
                serializer(item)
        for name in ('token', 'csrf', 'secret', 'committed', 'to_json'):
            assert not hasattr(item, name)
        with pytest.raises(AttributeError):
            item.committed = True
        with pytest.raises(AttributeError):
            del item._material
    with pytest.raises(AttributeError):
        stored.token_hash = 'replacement'
    with pytest.raises(AttributeError):
        prepared._material = ()
    assert not hasattr(stored, '_token_for_committed_adapter')
    assert not hasattr(stored, '_csrf_for_committed_adapter')
    assert prepared._token_for_committed_adapter() not in stored._material
    assert prepared._csrf_for_committed_adapter() not in stored._material
    assert prepared._token_for_committed_adapter() not in repr(stored)
    assert stored.token_hash not in repr(stored)
    for cls in (c.PreparedSession, c.SessionStorage):
        with pytest.raises(TypeError):
            cls(object(), ())


def test_storage_requires_original_exact_owned_preparation():
    c = module()
    codec = c.SessionCodec(installation_key=KEY)
    original = codec.prepare()
    copied = object.__new__(c.PreparedSession)
    object.__setattr__(copied, '_material', original._material)

    class Subclass(c.PreparedSession):
        pass

    subclass = object.__new__(Subclass)
    object.__setattr__(subclass, '_material', original._material)
    forged = object.__new__(c.PreparedSession)
    foreign = c.SessionCodec(installation_key=KEY).prepare()
    for rejected in (copied, subclass, forged, foreign, None, {}, codec.storage(original)):
        with pytest.raises(c.AuthenticationError) as caught:
            codec.storage(rejected)
        assert_safe_error(caught.value, 'unauthenticated')
    assert codec.storage(original).token_hash == codec.token_hash(original._token_for_committed_adapter())


def test_equal_but_replaced_private_tuple_is_rejected():
    c = module()
    codec = c.SessionCodec(installation_key=KEY)
    prepared = codec.prepare()
    replacement = tuple(list(prepared._material))
    assert replacement == prepared._material and replacement is not prepared._material
    object.__setattr__(prepared, '_material', replacement)
    with pytest.raises(c.AuthenticationError) as caught:
        codec.storage(prepared)
    assert_safe_error(caught.value, 'unauthenticated')


def test_independent_registry_rejects_swapped_or_missing_private_tuple():
    c = module()
    codec = c.SessionCodec(installation_key=KEY)
    first, second = codec.prepare(), codec.prepare()
    object.__setattr__(first, '_material', second._material)
    with pytest.raises(c.AuthenticationError):
        codec.storage(first)
    object.__delattr__(second, '_material')
    with pytest.raises(c.AuthenticationError) as caught:
        codec.storage(second)
    assert_safe_error(caught.value, 'unauthenticated')


def test_codec_does_not_retain_discarded_preparation():
    codec = module().SessionCodec(installation_key=KEY)
    prepared = codec.prepare()
    reference = weakref.ref(prepared)
    stored = codec.storage(prepared)
    del prepared
    gc.collect()
    assert reference() is None
    assert len(stored.token_hash) == len(stored.csrf_hash) == 64


@pytest.mark.parametrize('failing_call', [1, 2])
@pytest.mark.parametrize('failure', ['raises', 'short', 'long', 'wrong_type', 'subclass'])
def test_rng_failure_at_either_random_draw_is_fixed_and_has_no_context(failing_call, failure, monkeypatch):
    c = module()
    calls = 0

    def broken_random(length):
        nonlocal calls
        calls += 1
        if calls != failing_call:
            return b'\0' * length
        if failure == 'raises':
            raise RuntimeError('private random backend detail')
        if failure == 'short':
            return b'\0' * (length - 1)
        if failure == 'long':
            return b'\0' * (length + 1)
        if failure == 'subclass':
            return BytesSubclass(b'\0' * length)
        return 'private random backend detail'

    monkeypatch.setattr(c.secrets, 'token_bytes', broken_random)
    with pytest.raises(c.AccessUnavailableError) as caught:
        c.SessionCodec(installation_key=KEY).prepare()
    assert_safe_error(caught.value, 'access_unavailable')
    assert calls == failing_call


@pytest.mark.parametrize('failure', ['raises', 'wrong_length', 'wrong_type'])
def test_crypto_backend_failure_never_retains_secret_exception_context(failure, monkeypatch):
    c = module()
    codec = c.SessionCodec(installation_key=KEY)

    def unavailable(*args, **kwargs):
        if failure == 'raises':
            raise ValueError('private key and token backend detail')
        return b'x' * 31 if failure == 'wrong_length' else 'private backend detail'

    monkeypatch.setattr(hmac, 'digest', unavailable)
    for operation in (lambda: codec.token_hash(TOKEN), lambda: codec.csrf_value(TOKEN),
                      lambda: codec.csrf_hash(CSRF), lambda: codec.verify_csrf(TOKEN, CSRF, CSRF_HASH),
                      codec.prepare):
        with pytest.raises(c.AccessUnavailableError) as caught:
            operation()
        assert_safe_error(caught.value, 'access_unavailable')
