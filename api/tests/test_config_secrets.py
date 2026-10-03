"""Private configuration envelopes retain identity and reject tampering."""
import pytest
import os
import stat
import json
import subprocess
import sys
from pathlib import Path
from cryptography.fernet import Fernet

from app.config_secrets import ConfigurationCipher, ConfigurationSecretError
from app import config_secrets


def test_encrypted_configuration_is_bound_to_its_installation_kind_and_record():
    cipher = ConfigurationCipher(Fernet.generate_key())
    payload = {'PHAXIO_API_SECRET': 'synthetic-private-secret', 'literal': 'line1\nline2'}
    envelope = cipher.seal(payload, installation_id='installation-one', kind='revision', record_id='revision-one')
    assert 'synthetic-private-secret' not in envelope
    assert cipher.open(envelope, installation_id='installation-one', kind='revision', record_id='revision-one') == payload
    for context in (
        {'installation_id': 'installation-two', 'kind': 'revision', 'record_id': 'revision-one'},
        {'installation_id': 'installation-one', 'kind': 'profile', 'record_id': 'revision-one'},
        {'installation_id': 'installation-one', 'kind': 'revision', 'record_id': 'revision-two'},
    ):
        with pytest.raises(ConfigurationSecretError):
            cipher.open(envelope, **context)
    wrong = ConfigurationCipher(Fernet.generate_key())
    with pytest.raises(ConfigurationSecretError):
        wrong.open(envelope, installation_id='installation-one', kind='revision', record_id='revision-one')
    with pytest.raises(ConfigurationSecretError):
        cipher.open(envelope[:-8]+'tampered', installation_id='installation-one', kind='revision', record_id='revision-one')


def test_installation_key_is_private_stable_and_never_replaced(tmp_path):
    path = tmp_path / 'configuration.key'
    with pytest.raises(ConfigurationSecretError):
        config_secrets.load_installation_key(path, allow_create=False)
    assert not path.exists()
    first = config_secrets.load_installation_key(path, allow_create=True)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert config_secrets.load_installation_key(path, allow_create=True) == first
    assert config_secrets.load_installation_key(path, allow_create=False) == first
    ConfigurationCipher(first)
    path.write_bytes(b'invalid-existing-key')
    with pytest.raises(ConfigurationSecretError):
        config_secrets.load_installation_key(path, allow_create=True)
    assert path.read_bytes() == b'invalid-existing-key'


@pytest.mark.parametrize('kind', ['public', 'directory', 'symlink', 'dangling', 'fifo'])
def test_key_loader_refuses_unsafe_files_without_replacing_them(tmp_path, kind):
    path = tmp_path / 'configuration.key'
    if kind == 'public':
        path.write_bytes(Fernet.generate_key())
        path.chmod(0o644)
    elif kind == 'directory':
        path.mkdir()
    elif kind in ('symlink', 'dangling'):
        target = tmp_path / 'target'
        if kind == 'symlink':
            target.write_bytes(Fernet.generate_key())
            target.chmod(0o600)
        path.symlink_to(target)
    else:
        os.mkfifo(path, 0o600)
    before = path.lstat()
    with pytest.raises(ConfigurationSecretError):
        config_secrets.load_installation_key(path, allow_create=True)
    assert path.lstat().st_ino == before.st_ino


def test_key_publication_failure_keeps_complete_key_for_retry(tmp_path, monkeypatch):
    path = tmp_path / 'configuration.key'
    original = os.fsync
    def fail_directory(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError('synthetic private failure')
        return original(fd)
    monkeypatch.setattr(config_secrets.os, 'fsync', fail_directory)
    with pytest.raises(ConfigurationSecretError) as caught:
        config_secrets.load_installation_key(path, allow_create=True)
    assert 'synthetic private' not in str(caught.value)
    first = path.read_bytes()
    ConfigurationCipher(first)
    monkeypatch.setattr(config_secrets.os, 'fsync', original)
    assert config_secrets.load_installation_key(path, allow_create=True) == first
    assert list(tmp_path.iterdir()) == [path]


def test_independent_processes_agree_on_one_complete_key(tmp_path):
    path = tmp_path / 'configuration.key'
    script = '''
import hashlib, sys
from app.config_secrets import load_installation_key
print(hashlib.sha256(load_installation_key(sys.argv[1], allow_create=True)).hexdigest())
'''
    environment = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1])}
    children = [subprocess.Popen([sys.executable, '-c', script, str(path)],
                env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                for _ in range(6)]
    results = [child.communicate(timeout=20) for child in children]
    assert all(child.returncode == 0 for child in children)
    assert len({stdout.strip() for stdout, _ in results}) == 1
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize('record', [[], {'version': True}, {'version': 2},
    {'version': 1, 'installation_id': 'one', 'kind': 'revision', 'record_id': 'one', 'payload': []}])
def test_authenticated_but_invalid_record_is_rejected(record):
    key = Fernet.generate_key()
    envelope = Fernet(key).encrypt(json.dumps(record).encode()).decode()
    with pytest.raises(ConfigurationSecretError):
        ConfigurationCipher(key).open(envelope, installation_id='one', kind='revision', record_id='one')


def test_key_failure_before_publication_leaves_no_canonical_key(tmp_path, monkeypatch):
    def fail_write(*args):
        raise OSError('synthetic-secret-in-os-error')
    monkeypatch.setattr(config_secrets.os, 'write', fail_write)
    with pytest.raises(ConfigurationSecretError) as caught:
        config_secrets.load_installation_key(tmp_path / 'configuration.key', allow_create=True)
    assert 'synthetic-secret' not in str(caught.value)
    assert list(tmp_path.iterdir()) == []


def test_payload_limits_and_invalid_values_are_safe():
    cipher = ConfigurationCipher(Fernet.generate_key())
    context = dict(installation_id='one', kind='revision', record_id='one')
    for payload in ({'secret': 'x' * (1024 * 1024)}, {'secret': float('nan')}, {'secret': b'private'}):
        with pytest.raises(ConfigurationSecretError):
            cipher.seal(payload, **context)
    for envelope in ('x' * (2 * 1024 * 1024 + 1), 'private-\ud800', None):
        with pytest.raises(ConfigurationSecretError):
            cipher.open(envelope, **context)
