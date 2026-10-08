"""A partner on your own network with its own certificate: exactly the pinned certificate, checked at the handshake.

A real TLS server on 127.0.0.1 presents a self-signed certificate made here.
The pin (Builder AT's ``direct_certificate_pins``, created here with its
0045 columns when this branch does not have that revision yet) decides:

- the pinned certificate: the request goes through;
- another certificate: refused after the handshake and before one byte of the
  request is written, and the partner shows the change;
- no pin: normal verification, which refuses a self-signed certificate.
"""
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import ipaddress
import ssl
from uuid import uuid4

import pytest
import sqlalchemy as sa
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from api.app.direct.service import CertificateChanged, HttpClient, PartnerUnreachable
from api.app.direct.store import DirectStore
from api.app.schema import create_database_engine, upgrade_schema


NOW = datetime(2026, 10, 7, 9, 0)


def _certificate(folder, name):
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'faxbot.lan')])
    moment = datetime.now(timezone.utc)
    certificate = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
                   .serial_number(x509.random_serial_number()).not_valid_before(moment - timedelta(days=1))
                   .not_valid_after(moment + timedelta(days=30))
                   .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]),
                                  critical=False)
                   .sign(key, hashes.SHA256()))
    cert_path, key_path = folder / f'{name}.pem', folder / f'{name}.key'
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return cert_path, key_path, hashlib.sha256(certificate.public_bytes(serialization.Encoding.DER)).hexdigest()


class Partner:
    """A TLS server that records every byte of request it reads after the handshake."""

    def __init__(self, cert_path, key_path):
        self.context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        self.context.load_cert_chain(cert_path, key_path)
        self.received = []
        self.finished = asyncio.Event()

    async def handle(self, reader, writer):
        data = b''
        try:
            while b'\r\n\r\n' not in data:
                chunk = await reader.read(65536)
                if not chunk:
                    break
                data += chunk
            if data:
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 15\r\n'
                             b'Connection: close\r\n\r\n{"answer":"ok"}')
                await writer.drain()
        except (ssl.SSLError, ConnectionError):
            pass
        finally:
            self.received.append(data)
            writer.close()
            self.finished.set()

    async def __aenter__(self):
        self.server = await asyncio.start_server(self.handle, '127.0.0.1', 0, ssl=self.context)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self.server.close()
        await self.server.wait_closed()

    async def settled(self):
        await asyncio.wait_for(self.finished.wait(), 10)
        self.finished.clear()
        return self.received[-1]


@pytest.fixture
def store(tmp_path):
    engine = create_database_engine('sqlite:///' + str(tmp_path / 'pins.db'))
    upgrade_schema(engine)
    with engine.begin() as connection:
        # Builder AT's 0045 table, exactly as 45a4fafc defines it (it merges into this branch later).
        if not sa.inspect(connection).has_table('direct_certificate_pins'):
            connection.execute(sa.text(
                'CREATE TABLE direct_certificate_pins (id VARCHAR(40) NOT NULL PRIMARY KEY, peer_id VARCHAR(40) '
                'NOT NULL, host VARCHAR(253) NOT NULL, certificate_sha256 VARCHAR(64) NOT NULL, suggestion_id '
                'VARCHAR(40), created_at DATETIME NOT NULL)'))
    return DirectStore(engine)


def _enroll(store, port):
    peer_id = uuid4().hex
    with store.engine.begin() as connection:
        connection.execute(store.peers.insert().values(
            id=peer_id, organization='Branch office', phone_number='+15550100002',
            endpoint_url=f'https://127.0.0.1:{port}', signing_key='s' * 43, exchange_key='e' * 43, state='verified',
            challenge_failures=0, version=1, created_at=NOW, updated_at=NOW))
    return peer_id


def _pin(store, peer_id, certificate, at=NOW):
    with store.engine.begin() as connection:
        connection.execute(sa.text('INSERT INTO direct_certificate_pins (id, peer_id, host, certificate_sha256, '
                                   'created_at) VALUES (:id, :peer, :host, :sha, :at)'),
                           {'id': uuid4().hex, 'peer': peer_id, 'host': '127.0.0.1', 'sha': certificate, 'at': at})


def _client(store):
    return HttpClient(timeout=10.0, allow_private=lambda: True, pins=store.certificate_pin,
                      changed=store.note_certificate)


@pytest.mark.asyncio
async def test_the_pinned_certificate_is_accepted_and_nothing_else_is(store, tmp_path):
    cert, key, fingerprint = _certificate(tmp_path, 'partner')
    _, _, other = _certificate(tmp_path, 'other')
    async with Partner(cert, key) as partner:
        peer_id = _enroll(store, partner.port)
        _pin(store, peer_id, fingerprint)
        status, body = await _client(store).request('GET', f'https://127.0.0.1:{partner.port}/direct/card')
        assert (status, body) == (200, {'answer': 'ok'})
        assert (await partner.settled()).startswith(b'GET /direct/card HTTP/1.1')
        assert store.get_peer(peer_id)['certificate_changed_at'] is None

        # The partner now presents a certificate other than the pinned one (a newer pin names another).
        _pin(store, peer_id, other, at=NOW + timedelta(minutes=1))
        with pytest.raises(CertificateChanged) as refused:
            await _client(store).request('POST', f'https://127.0.0.1:{partner.port}/direct/deliveries',
                                         content=b'secret document bytes')
        # Refused at the handshake: the partner read not one byte of the request.
        assert await partner.settled() == b''
        assert refused.value.seen == fingerprint
        assert 'not the one it had when you enrolled it' in str(refused.value)
        changed = store.get_peer(peer_id)
        assert changed['certificate_changed_sha256'] == fingerprint and changed['certificate_changed_at'] is not None

        # Pinned again to the certificate it presents: accepted, and the change is cleared.
        _pin(store, peer_id, fingerprint, at=NOW + timedelta(minutes=2))
        status, _ = await _client(store).request('GET', f'https://127.0.0.1:{partner.port}/direct/card')
        assert status == 200
        await partner.settled()
        assert store.get_peer(peer_id)['certificate_changed_at'] is None


@pytest.mark.asyncio
async def test_an_unpinned_self_signed_host_is_refused_by_normal_verification(store, tmp_path):
    cert, key, _ = _certificate(tmp_path, 'stranger')
    async with Partner(cert, key) as partner:
        _enroll(store, partner.port)  # Enrolled, but nothing pins its certificate.
        with pytest.raises(PartnerUnreachable) as refused:
            await _client(store).request('POST', f'https://127.0.0.1:{partner.port}/direct/deliveries',
                                         content=b'secret document bytes')
        assert not isinstance(refused.value, CertificateChanged)
        # The handshake failed, so the server never handed the connection over: no request byte was read.
        assert all(data == b'' for data in partner.received)


@pytest.mark.asyncio
async def test_a_revoked_partners_pin_no_longer_applies(store, tmp_path):
    cert, key, fingerprint = _certificate(tmp_path, 'partner')
    async with Partner(cert, key) as partner:
        peer_id = _enroll(store, partner.port)
        _pin(store, peer_id, fingerprint)
        store.revoke(peer_id)
        assert store.certificate_pin('127.0.0.1') is None
        with pytest.raises(PartnerUnreachable):
            await _client(store).request('GET', f'https://127.0.0.1:{partner.port}/direct/card')
        assert all(data == b'' for data in partner.received)


def test_without_the_pin_table_every_host_is_verified_normally(tmp_path):
    engine = create_database_engine('sqlite:///' + str(tmp_path / 'plain.db'))
    upgrade_schema(engine)
    if sa.inspect(engine).has_table('direct_certificate_pins'):
        pytest.skip('Builder AT\'s pin table is installed on this branch.')
    assert DirectStore(engine).certificate_pin('127.0.0.1') is None
