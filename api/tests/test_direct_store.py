"""Direct delivery ledger: a fence and an acceptance for one message id are exclusive."""
from datetime import datetime

import pytest

from api.tests.test_schema import database
from api.app.schema import upgrade_schema
from api.app.direct.store import DirectStore, code_hash


@pytest.fixture
def store(database):
    upgrade_schema(database)
    direct = DirectStore(database)
    now = datetime.utcnow()
    with database.begin() as connection:
        connection.execute(direct.peers.insert().values(
            id='peer-1', organization='Valley Hospital', phone_number='+15550100001', endpoint_url='https://valley.example',
            signing_key='s' * 43, exchange_key='x' * 43, state='verified', challenge_failures=0, version=1,
            created_at=now, updated_at=now))
    return direct


def manifest(message_id):
    return ('{"document":{"pages":1,"sha256":"' + 'a' * 64 + '","size":10},"message_id":"' + message_id
            + '","recipient":{"fax_number":"+15550100002"},"sender":{"fax_number":"+15550100001"}}').encode('ascii')


def accept(store, message_id):
    return store.accept_inbound(message_id=message_id, peer={'id': 'peer-1'}, manifest=manifest(message_id),
                                receipt_for=lambda _: {'statement': '{}', 'signature': 'x'}, document_path='/tmp/x.pdf')


def test_not_received_answer_fences_a_later_acceptance(store):
    assert store.answer_or_fence('b' * 32, {'id': 'peer-1'}) is None
    row, created = accept(store, 'b' * 32)
    assert created is False and row['state'] == 'refused'
    assert store.intake.list_items() == []
    # Asking again keeps answering from the same fence.
    assert store.answer_or_fence('b' * 32, {'id': 'peer-1'})['state'] == 'refused'
    assert store.recent() == []


def test_acceptance_first_is_reported_as_received(store):
    row, created = accept(store, 'c' * 32)
    assert created is True and row['state'] == 'accepted'
    assert store.answer_or_fence('c' * 32, {'id': 'peer-1'})['id'] == row['id']
    assert len(store.intake.list_items()) == 1


def test_codes_lock_after_five_wrong_tries(store):
    now = datetime.utcnow()
    with store.engine.begin() as connection:
        connection.execute(store.peers.update().where(store.peers.c.id == 'peer-1').values(state='pending'))
    store.start_challenge('peer-1', code='12345678', job_id=None, now=now)
    for _ in range(5):
        assert store.confirm_code('s' * 43, '00000000', now=now)[0] == 'mismatch'
    assert store.confirm_code('s' * 43, '12345678', now=now)[0] == 'unavailable'
    store.start_challenge('peer-1', code='87654321', job_id=None, now=now)
    assert store.get_peer('peer-1')['challenge_hash'] == code_hash('peer-1', '87654321')
    assert store.confirm_code('s' * 43, '8765 4321', now=now)[0] == 'verified'
    assert store.get_peer('peer-1')['state'] == 'verified'
