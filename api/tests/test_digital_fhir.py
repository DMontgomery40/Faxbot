"""A fax delivered as a FHIR DocumentReference: SMART Backend Services sign-in, conditional create, and how each
answer is classified, through the real delivery worker, planner, routed transport and delivery store.

The FHIR server is a stand-in that checks the signed sign-in token against the key set Faxbot publishes; nothing
contacts a real FHIR server or EHR. Synthetic names and 555 numbers only.
"""
import asyncio
import base64
from datetime import datetime
import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
import sqlalchemy as sa
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from api.app.config_profiles import ConfigurationDocument, ProviderConfiguration
from api.app.config_store import ConfigurationStore
from api.app.config_values import ConfigurationValues
from api.app.digital import accounts as digital_accounts
from api.app.digital import fhir
from api.app.digital.routes import DigitalRoute
from api.app.digital.store import DigitalStore
from api.app.digital.worker import DigitalWorker
from api.app.outbound_store import OutboundStore
from api.app.outbound_worker import OutboundWorker
from api.app.routing.transport import RoutedTransport
from api.app.schema import create_database_engine, upgrade_schema
from api.tests.digital_fixtures import synthetic_pdf
from api.tests.test_direct_delivery import Conventional
from api.tests.test_peer_fax import _namespaces


DEST = '+13035550170'
BASE = 'https://fhir.hospital.example.net/r4'
TOKEN_URL = 'https://fhir.hospital.example.net/auth/token'


def _b64decode(text):
    return base64.urlsafe_b64decode(text + '=' * (-len(text) % 4))


def key_from_jwk(jwk):
    if jwk['kty'] == 'RSA':
        return rsa.RSAPublicNumbers(int.from_bytes(_b64decode(jwk['e']), 'big'),
                                    int.from_bytes(_b64decode(jwk['n']), 'big')).public_key()
    return ec.EllipticCurvePublicNumbers(int.from_bytes(_b64decode(jwk['x']), 'big'),
                                         int.from_bytes(_b64decode(jwk['y']), 'big'), ec.SECP384R1()).public_key()


def verify_jwt(token, jwks):
    """What the FHIR server's authorization does: the header, the claims and the signature with the registered key."""
    header_text, claims_text, signature_text = token.split('.')
    header, claims = json.loads(_b64decode(header_text)), json.loads(_b64decode(claims_text))
    (jwk,) = [key for key in jwks['keys'] if key['kid'] == header['kid']]
    signed, signature = f'{header_text}.{claims_text}'.encode(), _b64decode(signature_text)
    key = key_from_jwk(jwk)
    if header['alg'] == 'RS384':
        key.verify(signature, signed, padding.PKCS1v15(), hashes.SHA384())
    else:
        assert len(signature) == 96
        der = encode_dss_signature(int.from_bytes(signature[:48], 'big'), int.from_bytes(signature[48:], 'big'))
        key.verify(der, signed, ec.ECDSA(hashes.SHA384()))
    return header, claims


class FakeFhir:
    """A FHIR R4 server with SMART Backend Services sign-in; ``mode`` says how it answers a document."""

    def __init__(self):
        self.jwks = None
        self.mode = 'created'
        self.documents = {}       # identifier -> resource
        self.posts, self.searches, self.tokens = [], [], []
        self.token_status = 200

    def send(self, method, url, headers, data):
        parts = urlsplit(url)
        path = parts.path
        if method == 'GET' and path == '/r4/.well-known/smart-configuration':
            return self._json(200, {'token_endpoint': TOKEN_URL, 'token_endpoint_auth_methods_supported':
                                    ['private_key_jwt'], 'grant_types_supported': ['client_credentials']})
        if method == 'POST' and url == TOKEN_URL:
            form = {name: values[0] for name, values in parse_qs(data.decode()).items()}
            assert form['grant_type'] == 'client_credentials'
            assert form['client_assertion_type'] == fhir.ASSERTION_TYPE
            header, claims = verify_jwt(form['client_assertion'], self.jwks)
            self.tokens.append((header, claims, form['scope']))
            if self.token_status != 200:
                return self._json(self.token_status, {'error': 'invalid_client'})
            return self._json(200, {'access_token': 'synthetic-access-' + uuid4().hex[:8], 'token_type': 'bearer',
                                    'expires_in': 300})
        assert headers.get('Authorization', '').startswith('Bearer synthetic-access-')
        if method == 'POST' and path == '/r4/DocumentReference':
            resource = json.loads(data)
            self.posts.append((headers, resource))
            identifier = resource['identifier'][0]['value']
            assert headers['If-None-Exist'] == f'identifier=urn:ietf:rfc:3986|{identifier}'
            if self.mode == 'lost_after_store':
                self.documents[identifier] = {**resource, 'id': 'doc-' + uuid4().hex[:6]}
                raise fhir.Lost('reset after sending')
            if self.mode == 'lost':
                raise fhir.Lost('reset after sending')
            if self.mode == 'refuse':
                return self._json(422, {'resourceType': 'OperationOutcome', 'issue': [
                    {'severity': 'error', 'code': 'required', 'diagnostics': 'DocumentReference.subject is required'}]})
            if self.mode in ('unavailable', 'server_error', 'multiple'):
                return self._json({'unavailable': 503, 'server_error': 500, 'multiple': 412}[self.mode], {})
            if identifier in self.documents:
                return self._json(200, self.documents[identifier])
            stored = {**resource, 'id': 'doc-' + uuid4().hex[:6]}
            self.documents[identifier] = stored
            return fhir.Response(201, {'location': f'{BASE}/DocumentReference/{stored["id"]}/_history/1'},
                                 json.dumps(stored).encode())
        if method == 'GET' and path == '/r4/DocumentReference':
            identifier = parse_qs(parts.query)['identifier'][0].split('|', 1)[1]
            self.searches.append(identifier)
            found = self.documents.get(identifier)
            return self._json(200, {'resourceType': 'Bundle', 'type': 'searchset', 'total': int(bool(found)),
                                    'entry': [{'resource': found}] if found else []})
        raise AssertionError((method, url))

    @staticmethod
    def _json(status, body):
        return fhir.Response(status, {'content-type': 'application/fhir+json'}, json.dumps(body).encode())


def _rows(engine, query, **params):
    with engine.connect() as connection:
        return [dict(row) for row in connection.execute(sa.text(query), params).mappings()]


@pytest.fixture(params=['sqlite', 'postgresql'])
def world(request, tmp_path):
    scoped = _namespaces(request, 1)
    url = scoped[0].render_as_string(hide_password=False) if scoped else 'sqlite:///' + str(tmp_path / 'clinic.db')
    engine = create_database_engine(url)
    upgrade_schema(engine)
    data = tmp_path / 'data'
    data.mkdir()
    configuration = ConfigurationStore(engine, tmp_path / 'installation.key')
    values = ConfigurationValues.from_environment({
        'FAX_BACKEND': 'phaxio', 'FAX_DISABLED': 'false', 'FAX_DATA_DIR': str(data),
        'PUBLIC_API_URL': 'https://clinic.example.org', 'DIRECT_ORGANIZATION': 'County Clinic',
        'FAX_DEFAULT_COUNTRY': 'US'})
    phaxio = ProviderConfiguration('phaxio', credentials={'api_key': 'k', 'api_secret': 's'})
    snapshot = configuration.initialize(values, actor='test', providers={'outbound': phaxio})
    documents = digital_accounts.added(snapshot.desired.values, {
        'key': 'fhir-hospital', 'provider': 'fhir', 'settings': {'client_id': 'faxbot-county-clinic'}})
    documents = digital_accounts.with_signing_key(snapshot.desired.values.with_provider_accounts(
        ConfigurationDocument(documents)), 'fhir-hospital', algorithm='RS384')
    configuration.apply(snapshot, snapshot.desired.values, restart_required=False, actor='test',
                        accounts=ConfigurationDocument(documents))
    store = DigitalStore(engine)
    address = store.add_address(number=DEST, kind='fhir', address=BASE, source='entered', action='confirmed',
                                organization='Synthetic Hospital')
    server = FakeFhir()
    current = configuration.read().active.values
    server.jwks = fhir.jwks(digital_accounts.digital_account(current, 'fhir-hospital'))
    transport = fhir.Transport(send=server.send)
    delivery = OutboundStore(configuration)
    try:
        yield SimpleNamespace(engine=engine, configuration=configuration, data=data, store=store, server=server,
                              transport=transport, delivery=delivery, address=address,
                              values=lambda: configuration.read().active.values)
    finally:
        engine.dispose()


def queue(world, number=DEST):
    job = uuid4().hex
    (world.data / (job + '.pdf')).write_bytes(synthetic_pdf())
    now = datetime.utcnow()
    world.configuration.accept_outbound(world.configuration.read().active, {
        'id': job, 'to_number': number, 'file_name': 'referral.pdf', 'tiff_path': '', 'status': 'queued',
        'pages': 2, 'created_at': now, 'updated_at': now})
    return job


async def send(world):
    conventional = Conventional(world.delivery)
    transport = RoutedTransport(conventional, direct=None, relay=None, digital=DigitalRoute(
        world.engine, values=world.values, fhir_transport=world.transport))
    assert await OutboundWorker(world.delivery, transport).step() is True
    return conventional


def state_of(world, job):
    (row,) = _rows(world.engine, 'SELECT state, attempt_id FROM outbound_deliveries WHERE id = :id', id=job)
    (attempt,) = _rows(world.engine, 'SELECT phase FROM outbound_attempts WHERE id = :id', id=row['attempt_id'])
    return row['state'], attempt['phase']


def test_201_is_delivered_with_the_signed_sign_in_and_the_document_as_sent(world):
    job = queue(world)
    conventional = asyncio.run(send(world))
    assert conventional.submissions == 0
    (header, claims, scope) = world.server.tokens[0]
    assert header['alg'] == 'RS384' and header['typ'] == 'JWT' and header['kid'].startswith('faxbot-')
    assert claims['iss'] == claims['sub'] == 'faxbot-county-clinic' and claims['aud'] == TOKEN_URL
    assert 0 < claims['exp'] - claims['iat'] <= 300 and len(claims['jti']) == 32
    assert scope == 'system/DocumentReference.c'
    (_, resource), = world.server.posts
    assert resource['resourceType'] == 'DocumentReference' and resource['status'] == 'current'
    assert 'subject' not in resource
    attachment = resource['content'][0]['attachment']
    document = (world.data / (job + '.pdf')).read_bytes()
    assert base64.b64decode(attachment['data']) == document and attachment['size'] == len(document)
    import hashlib
    assert attachment['hash'] == base64.b64encode(hashlib.sha1(document).digest()).decode()
    (message,) = world.store.for_job(job)
    assert message['state'] == 'delivered' and message['remote_id'].startswith('doc-')
    assert state_of(world, job)[0] == 'success'


def test_a_4xx_is_definite_and_the_fax_goes_by_its_own_route_in_the_same_attempt(world):
    world.server.mode = 'refuse'
    job = queue(world)
    conventional = asyncio.run(send(world))
    assert conventional.submissions == 1
    (message,) = world.store.for_job(job)
    assert message['state'] == 'refused' and 'subject is required' in message['detail']
    assert state_of(world, job)[0] == 'in_progress'


@pytest.mark.parametrize('mode', ['unavailable'])
def test_a_503_is_definite(world, mode):
    world.server.mode = mode
    job = queue(world)
    assert asyncio.run(send(world)).submissions == 1
    assert world.store.for_job(job)[0]['state'] == 'refused'


@pytest.mark.parametrize('mode', ['server_error', 'multiple', 'lost'])
def test_a_500_412_or_lost_answer_is_uncertain_and_never_sent_again(world, mode):
    world.server.mode = mode
    job = queue(world)
    conventional = asyncio.run(send(world))
    assert conventional.submissions == 0
    assert world.store.for_job(job)[0]['state'] == 'uncertain'
    assert state_of(world, job) == ('reconciliation_required', 'uncertain')
    DigitalWorker(world.engine, values=world.values, delivery=lambda: world.delivery,
                  fhir_transport=world.transport).step()
    assert len(world.server.posts) == 1 and world.server.searches
    assert world.store.for_job(job)[0]['state'] == 'uncertain'


def test_an_uncertain_document_the_server_stored_is_found_by_its_identifier(world):
    world.server.mode = 'lost_after_store'
    job = queue(world)
    asyncio.run(send(world))
    assert world.store.for_job(job)[0]['state'] == 'uncertain'
    DigitalWorker(world.engine, values=world.values, delivery=lambda: world.delivery,
                  fhir_transport=world.transport).step()
    (message,) = world.store.for_job(job)
    assert message['state'] == 'delivered' and len(world.server.posts) == 1
    assert state_of(world, job)[0] == 'success'


def test_a_refused_sign_in_sends_nothing(world):
    world.server.token_status = 401
    job = queue(world)
    assert asyncio.run(send(world)).submissions == 1
    assert world.server.posts == []
    assert "refused Faxbot's sign-in" in world.store.for_job(job)[0]['detail']


def test_es384_tokens_are_raw_r_and_s_and_verify_with_the_published_key():
    from api.app.digital.accounts import DigitalAccount
    from cryptography.hazmat.primitives import serialization
    key = ec.generate_private_key(ec.SECP384R1())
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    account = DigitalAccount('fhir-x', 'fhir', 'FHIR client', True,
                             {'client_id': 'faxbot-x', 'algorithm': 'ES384', 'key_id': 'faxbot-k1'},
                             {'signing_key': pem})
    token = fhir.client_assertion(account, TOKEN_URL)
    header, claims = verify_jwt(token, fhir.jwks(account))
    assert header == {'alg': 'ES384', 'kid': 'faxbot-k1', 'typ': 'JWT'} and claims['aud'] == TOKEN_URL
    tampered = token[:-4] + ('AAAA' if not token.endswith('AAAA') else 'BBBB')
    with pytest.raises(InvalidSignature):
        verify_jwt(tampered, fhir.jwks(account))


def test_private_and_plain_http_endpoints_are_refused():
    transport = fhir.Transport(resolver=lambda host, port: ['10.0.0.5'])
    with pytest.raises(fhir.NotSent, match='private or local'):
        transport.request('GET', 'https://fhir.internal.example/r4/metadata')
    with pytest.raises(fhir.NotSent, match='https'):
        fhir.Transport().request('GET', 'http://fhir.hospital.example.net/r4/metadata')
