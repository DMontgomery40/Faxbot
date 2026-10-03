"""Pure bounded credential invariants; no routes or app-global database."""
import base64
import dataclasses
import hashlib
import importlib
import json
import pickle

import pytest


def module():
    name = 'api.app.access.credentials'
    assert importlib.util.find_spec(name) is not None, 'bounded credential codec is missing'
    return importlib.import_module(name)


def b64(value):
    return base64.urlsafe_b64encode(value).decode().rstrip('=')


# Literal known PBKDF2-HMAC-SHA256 vector: secret 'password', salt bytes 0..15,
# 200000 rounds, 32-byte digest. Verification must retain legacy short secrets.
LEGACY = 'pbkdf2$AAECAwQFBgcICQoLDA0ODw$WB_oMn6_nBke1Dua7Bf4IXYsCiSqNakzyNxknNxg_u4$rounds=200000'


def test_legacy_known_vector_and_wrong_secret():
    codec = module().CredentialCodec()
    assert codec.supports_hash(LEGACY)
    assert codec.verify('password', LEGACY)
    assert not codec.verify('wrong password', LEGACY)


def test_new_password_preserves_whitespace_and_exact_unicode_bounds():
    c = module()
    codec = c.CredentialCodec()
    password = '  twelve plus spaces  '
    prepared = codec.prepare_chosen_password(password)
    encoded = prepared._password_hash_for_storage()
    assert encoded.startswith('scrypt$')
    assert codec.verify(password, encoded)
    assert not codec.verify(password.strip(), encoded)
    assert codec.verify('😀' * 256, codec.prepare_chosen_password('😀' * 256)._password_hash_for_storage())
    for invalid in ('x' * 11, '😀' * 257, '\ud800' * 12, None, b'123456789012'):
        with pytest.raises(c.InvalidCredentialInputError) as error:
            codec.prepare_chosen_password(invalid)
        assert str(error.value) == 'invalid_credential_input'
        assert error.value.__context__ is None


@pytest.mark.parametrize('encoded', [
    '', None, 'x' * 257, '😀' * 256,
    LEGACY.replace('rounds=200000', 'rounds=200001'),
    LEGACY.replace('rounds=200000', 'rounds=0200000'),
    LEGACY + '$extra', LEGACY.replace('pbkdf2$', 'pbkdf2-sha256$'),
    LEGACY.replace('AAECAwQFBgcICQoLDA0ODw', 'AAECAwQFBgcICQoLDA0ODw=='),
    LEGACY.replace('AAECAwQFBgcICQoLDA0ODw', 'AAECAwQFBgcICQoLDA0ODx'),
    LEGACY.replace('AAECAwQFBgcICQoLDA0ODw', 'AAECAwQFBgcICQoLDA0ODw\n'),
    LEGACY.replace('AAECAwQFBgcICQoLDA0ODw', b64(b'x' * 15)),
    LEGACY.replace('WB_oMn6_nBke1Dua7Bf4IXYsCiSqNakzyNxknNxg_u4', b64(b'x' * 31)),
    'scrypt$AAECAwQFBgcICQoLDA0ODw$' + b64(b'x' * 32) + '$n=32768$r=8$p=1',
    'scrypt$AAECAwQFBgcICQoLDA0ODw$' + b64(b'x' * 32) + '$n=16384$p=1$r=8',
])
def test_malformed_formats_reject_before_kdf(encoded, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('malformed input reached KDF')
    monkeypatch.setattr(hashlib, 'scrypt', forbidden)
    monkeypatch.setattr(hashlib, 'pbkdf2_hmac', forbidden)
    codec = module().CredentialCodec()
    assert not codec.supports_hash(encoded)
    assert not codec.verify('password', encoded)


def test_verification_secret_bounds_reject_before_kdf(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('invalid secret reached KDF')
    monkeypatch.setattr(hashlib, 'pbkdf2_hmac', forbidden)
    codec = module().CredentialCodec()
    for invalid in ('', 'x' * 1025, '😀' * 257, '\ud800', None, b'password'):
        assert not codec.verify(invalid, LEGACY)


def test_fixed_kdf_unavailability_is_distinct_and_suppresses_backend_details(monkeypatch):
    c = module()
    codec = c.CredentialCodec()
    prepared = codec.prepare_chosen_password('twelve or more chars')
    encoded = prepared._password_hash_for_storage()
    def unavailable(*args, **kwargs):
        raise ValueError('secret-dependent backend detail')
    monkeypatch.setattr(hashlib, 'scrypt', unavailable)
    assert codec.supports_hash(encoded)
    for operation in (lambda: codec.verify('twelve or more chars', encoded),
                      lambda: codec.prepare_chosen_password('twelve or more chars'),
                      codec.prepare_temporary_password, codec.prepare_new_key,
                      lambda: codec.prepare_key_rotation('abcdef012345')):
        with pytest.raises(c.CredentialServiceError) as error:
            operation()
        assert str(error.value) == 'credential_service_unavailable'
        assert 'secret-dependent' not in repr(error.value)
        assert error.value.__cause__ is None
        assert error.value.__context__ is None
    # A structurally supported legacy hash also cannot silently become a miss.
    monkeypatch.setattr(hashlib, 'pbkdf2_hmac', unavailable)
    with pytest.raises(c.CredentialServiceError):
        codec.verify('password', LEGACY)


def test_missing_scrypt_has_no_pbkdf2_fallback(monkeypatch):
    c = module()
    monkeypatch.delattr(hashlib, 'scrypt')
    def forbidden(*args, **kwargs):
        pytest.fail('new credentials used fallback KDF')
    monkeypatch.setattr(hashlib, 'pbkdf2_hmac', forbidden)
    with pytest.raises(c.CredentialServiceError):
        c.CredentialCodec().prepare_chosen_password('long enough password')


def test_preparations_are_private_immutable_and_do_not_serialize():
    c = module()
    codec = c.CredentialCodec()
    temporary = codec.prepare_temporary_password()
    chosen = codec.prepare_chosen_password('long enough password')
    key = codec.prepare_new_key()
    rotation = codec.prepare_key_rotation(key.public_key_id)
    secret = temporary._temporary_secret_for_committed_adapter()
    assert len(base64.urlsafe_b64decode(secret + '=')) == 32
    assert codec.verify(secret, temporary._password_hash_for_storage())
    assert key.row_id != key.public_key_id and len(key.row_id) == 32
    for prepared in (temporary, chosen, key, rotation):
        assert repr(prepared) == type(prepared).__name__ + '()'
        assert not dataclasses.is_dataclass(prepared)
        with pytest.raises(TypeError):
            dataclasses.asdict(prepared)
        with pytest.raises(TypeError):
            json.dumps(prepared)
        with pytest.raises(TypeError):
            pickle.dumps(prepared)
        with pytest.raises(AttributeError):
            prepared.committed = True
        with pytest.raises(AttributeError):
            prepared._password_hash = 'replacement'
        with pytest.raises(TypeError):
            type(prepared)()
        assert not hasattr(prepared, 'secret') and not hasattr(prepared, 'token')
    for prepared in (key, rotation):
        token = prepared._token_for_committed_adapter()
        prefix = 'fbk_live_' + key.public_key_id + '_'
        assert token.startswith(prefix)
        assert len(base64.urlsafe_b64decode(token[len(prefix):] + '=')) == 32
        assert codec.verify(token[len(prefix):], prepared._key_hash_for_storage())
        assert token not in repr(prepared)
    assert rotation.public_key_id == key.public_key_id
    assert not hasattr(rotation, 'row_id')


@pytest.mark.parametrize('login,expected', [('  Alice.Example+1@clinic  ', ('Alice.Example+1@clinic', 'alice.example+1@clinic')), ('a' * 100, ('a' * 100, 'a' * 100))])
def test_shared_login_validation(login, expected):
    assert module().normalize_login(login) == expected


@pytest.mark.parametrize('login', ['', ' ', '_alice', '-alice', 'a b', 'alice\ninside', 'älice', 'a' * 101, None, b'alice'])
def test_malformed_login_is_fixed_safe_error(login):
    c = module()
    with pytest.raises(c.InvalidCredentialInputError) as error:
        c.normalize_login(login)
    assert str(error.value) == 'invalid_credential_input'
    assert error.value.__context__ is None


@pytest.mark.parametrize('identity', ['env', 'ABCDEF012345', 'abcdef01234', 'abcdef0123456', 'abcdef01234g', None])
def test_rotation_rejects_invalid_public_id_before_hashing(identity, monkeypatch):
    c = module()
    def forbidden(*args, **kwargs):
        pytest.fail('invalid public ID reached KDF')
    monkeypatch.setattr(hashlib, 'scrypt', forbidden)
    with pytest.raises(c.InvalidCredentialInputError):
        c.CredentialCodec().prepare_key_rotation(identity)
