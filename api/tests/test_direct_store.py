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
    # A fence is never filed as a received fax.
    assert store.unfiled() == []


def test_acceptance_first_is_reported_as_received(store):
    row, created = accept(store, 'c' * 32)
    assert created is True and row['state'] == 'accepted' and row['kind'] is None
    assert store.answer_or_fence('c' * 32, {'id': 'peer-1'})['id'] == row['id']
    # Filing makes it a received fax with its email item (filing.py); until then it waits to be filed.
    assert store.intake.list_items() == []
    assert [item['message_id'] for item in store.unfiled()] == ['c' * 32]


def test_an_arrival_from_before_peer_fax_keeps_its_email_item_and_is_never_filed_again(store):
    row, _ = accept(store, 'd' * 32)
    with store.engine.begin() as connection:
        store.intake.add_direct(connection, direct_delivery_id=row['id'], received_at=row['accepted_at'], pages=1,
                                from_number='+15550100001', to_number='+15550100002', now=row['accepted_at'])
    assert store.unfiled() == []


def test_only_a_newer_signed_statement_changes_what_a_partner_accepts(store):
    early, late = datetime(2026, 10, 7, 9, 0), datetime(2026, 10, 7, 10, 0)
    assert store.note_capabilities('peer-1', fax_images=True, peer_calls=False, said_at=late) is True
    assert store.note_capabilities('peer-1', fax_images=False, peer_calls=False, said_at=early) is False
    peer = store.get_peer('peer-1')
    assert (peer['partner_receives_fax_images'], peer['partner_peer_calls'], peer['partner_said_at']) == (1, None, late)
    # Signed in the same second: the statement that arrives later is kept.
    assert store.note_capabilities('peer-1', fax_images=False, peer_calls=True, said_at=late) is True
    peer = store.get_peer('peer-1')
    assert (peer['partner_receives_fax_images'], peer['partner_peer_calls']) == (None, 1)


def test_fax_images_are_on_by_default_and_every_partner_is_told_until_it_has_the_current_choice(store):
    from api.app.direct.store import accepts_fax_images
    peer = store.get_peer('peer-1')
    # A partner enrolled before 0034 reads NULL: on, with no row rewritten, and not told yet.
    assert peer['receive_fax_images'] is None and accepts_fax_images(peer) is True
    assert [item['id'] for item in store.untold()] == ['peer-1']
    assert store.mark_told('peer-1', fax_images=True, peer_calls=False) is True and store.untold() == []
    off = store.set_receive_fax_images('peer-1', False)
    assert off['receive_fax_images'] == 0 and accepts_fax_images(off) is False
    assert [item['id'] for item in store.untold()] == ['peer-1']
    # A statement of the earlier choice does not count as telling the current one.
    assert store.mark_told('peer-1', fax_images=True, peer_calls=False) is False and len(store.untold()) == 1
    assert store.set_receive_fax_images('peer-1', True)['receive_fax_images'] == 1


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
