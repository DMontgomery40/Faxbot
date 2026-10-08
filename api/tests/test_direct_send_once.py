"""Send once to a partner's intake (D7), and reuse what a partner holds (D9, D10), between two installations.

Installation B is the running application (County Clinic, with a central intake for several numbers);
installation A (Valley Hospital) sends through the real delivery worker and route planner. Documents, numbers
and case ledger rows are synthetic.
"""
import asyncio
from datetime import datetime
import hashlib
import json
import random
from uuid import uuid4

import pytest
import sqlalchemy as sa

from app import main
from api.app.direct import reuse
from api.app.direct.crypto import canonical, seal, signed, timestamp
from api.app.direct.distribute import SendOnce
from api.app.direct.filing import received_text
from api.app.outbound_worker import OutboundWorker
from api.tests.test_direct_delivery import (  # noqa: F401 - fixtures
    A_NUMBER, ADMIN, B_NUMBER, b_client, direct_databases, pair, pdf_bytes, transport)


B2, B3, B4 = '+15550100012', '+15550100013', '+15550100014'
SIMILAR = '+15550100015'  # next to B's numbers, never granted


class ToA:
    """B's transport to A: code confirmations and signed send-once statements."""

    def __init__(self, service):
        self.service = service
        self.down = False

    async def request(self, method, url, **kwargs):
        if self.down:
            from app.direct.service import PartnerUnreachable  # B runs as the application package
            raise PartnerUnreachable()
        body = kwargs['json']
        if url == 'https://a.example/direct/verifications':
            return await asyncio.to_thread(self.service.confirm, body['statement'], body['signature'])
        if url == 'https://a.example/direct/capabilities':
            return await asyncio.to_thread(self.service.note, body['statement'], body['signature'])
        assert method == 'POST' and url == 'https://a.example/direct/distribution/statements'
        return await asyncio.to_thread(SendOnce(self.service).hear, body['statement'], body['signature'])


@pytest.fixture
def intake(pair):
    """B's receiving rules for its numbers, and B's transport back to A."""
    to_a = ToA(pair['a'])
    main.app.state.direct_http = to_a
    client = pair['b_client']
    billing, records = mailbox(client, 'Billing'), mailbox(client, 'Records')
    rule(client, to_number=B2, mailbox_id=billing)
    rule(client, to_number=B3, mailbox_id=records)
    paths = []
    real = pair['to_b'].request

    async def logged(method, url, **kwargs):
        paths.append((method, url[len('https://testserver'):]))
        return await real(method, url, **kwargs)
    pair['to_b'].request = logged
    return {**pair, 'to_a': to_a, 'paths': paths}


def version(client):
    return client.get('/auth/me', headers=ADMIN).json()['policy_version']


def mailbox(client, label):
    created = client.post('/access/mailboxes', headers=ADMIN, json={'label': label, 'enabled': True,
                                                                    'expected_policy_version': version(client)})
    assert created.status_code == 200, created.text
    return created.json()['mailbox']['id']


def rule(client, **body):
    created = client.post('/access/inbound-rules', headers=ADMIN,
                          json={**body, 'expected_policy_version': version(client)})
    assert created.status_code == 200, created.text
    return created.json()['rule']


def accept_to(pair, document, number):
    job = uuid4().hex
    (pair['data'] / (job + '.pdf')).write_bytes(document)
    now = datetime.utcnow()
    pair['configuration'].accept_outbound(pair['snapshot'].active, {'id': job, 'to_number': number,
        'file_name': 'referral.pdf', 'tiff_path': '', 'status': 'queued', 'pages': 1,
        'created_at': now, 'updated_at': now})
    return job


async def agree(env, numbers=(B2, B3, B4)):
    """B grants A "send once" for ``numbers``; A's administrator accepts. Returns A's agreement row."""
    client = env['b_client']
    granted = client.post(f"/direct/peers/{env['a_on_b']['id']}/send-once", headers=ADMIN,
                          json={'numbers': list(numbers), 'intake': 'Central records'})
    assert granted.status_code == 200, granted.text
    assert granted.json()['state'] == 'offered' and granted.json()['told'] is True
    send_once = SendOnce(env['a'])
    (offer,) = send_once.store.agreements_for(role='sender')
    assert offer['state'] == 'offered' and json.loads(offer['numbers']) == sorted(numbers)
    accepted = send_once.accept(offer['id'], actor_name='Ada')
    assert await send_once.tell(accepted) == 'told'
    row = send_once.store.agreement(offer['id'])
    assert row['state'] == 'active'
    listed = client.get('/direct/send-once', headers=ADMIN).json()['agreements']
    assert listed[0]['state'] == 'active' and listed[0]['role'] == 'receiver'
    return row


def b_engine():
    return main.app.state.configuration_runtime.manager.store.engine


def received(numbers=None):
    """B's received faxes from direct delivery: [(to_number, mailbox_label, report, pdf bytes)]."""
    with b_engine().connect() as connection:
        rows = connection.execute(sa.text(
            "SELECT f.to_number, f.mailbox_label, i.report, d.document_path FROM inbound_faxes f "
            "JOIN inbound_imports i ON i.inbound_fax_id = f.id "
            "JOIN direct_deliveries d ON d.message_id = i.operation_id AND d.direction = 'inbound' "
            "WHERE i.source = 'local' ORDER BY f.to_number, d.created_at, d.id")).all()
    return [(row[0], row[1], json.loads(row[2]), open(row[3], 'rb').read()) for row in rows]


def receipt_of(pair, job):
    attempt = pair['delivery'].get(job)['attempt_id']
    row = pair['a'].store.find('outbound', attempt)
    return row, json.loads(json.loads(row['receipt'])['statement'])


async def run(pair, count):
    routed, conventional = transport(pair)
    for _ in range(count):
        assert await OutboundWorker(pair['delivery'], routed).step() is True
    return conventional


def synthetic_pdf(seed, size=120_000):
    rng = random.Random(seed)
    return b'%PDF-1.4\n' + bytes(rng.getrandbits(8) for _ in range(size)) + b'\n%%EOF\n'


# D7: the agreement ---------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_without_a_signed_agreement_nothing_goes_to_the_intake(intake):
    """Similar numbers or a shared name are never permission: only the numbers B signed."""
    document = pdf_bytes('No agreement yet')
    job = accept_to(intake, document, B2)
    conventional = await run(intake, 1)
    assert conventional.submissions == 1 and received() == []
    # A document sealed for one of B's other numbers is refused, signed, while B has granted nothing.
    a, client = intake['a'], intake['b_client']
    manifest, signature, ciphertext = seal(
        a.identity(), message_id=uuid4().hex, organization='Valley Hospital', fax_number=A_NUMBER,
        recipient_number=B2, recipient_signing_key=intake['b_on_a']['signing_key'],
        recipient_exchange_key=intake['b_on_a']['exchange_key'], document=document, pages=1)
    refused = client.post('/direct/deliveries', files={'manifest': (None, manifest), 'signature': (None, signature),
                                                       'document': ('d', ciphertext, 'application/octet-stream')})
    assert refused.status_code == 403 and '"reason":"wrong_recipient"' in refused.json()['statement']
    # An offer A did not sign is not recorded by B.
    body = {'type': 'distribution_offer', 'recipient': intake['b_on_a']['signing_key'], 'said_at': timestamp(),
            'signer': a.identity().signing_key, 'agreement': uuid4().hex, 'numbers': [B2], 'intake': 'Fake'}
    forged = client.post('/direct/distribution/statements', json={'statement': canonical(body).decode(),
                                                                   'signature': 'A' * 86})
    assert forged.status_code == 400 and SendOnce(a).store.agreements_for() == []
    # Granted for B2 only: a fax to the number beside it still goes by telephone.
    await agree(intake, numbers=(B2,))
    accept_to(intake, pdf_bytes('Next to a granted number'), SIMILAR)
    conventional = await run(intake, 1)
    assert conventional.submissions == 1 and intake['delivery'].get(job)['state'] == 'in_progress'
    assert received() == []


@pytest.mark.asyncio
async def test_one_copy_goes_to_the_intake_and_each_recipient_gets_its_own_receipt(intake):
    await agree(intake)
    document = pdf_bytes('One referral for three departments')
    jobs = [accept_to(intake, document, number) for number in (B2, B3, B4)]
    conventional = await run(intake, 3)
    assert conventional.submissions == 0
    digest = hashlib.sha256(document).hexdigest()
    # The document crossed once, with the signed list of recipients; the others sent references to it.
    posts = [path for method, path in intake['paths'] if method == 'POST']
    assert posts.count('/direct/distributions') == 1 and posts.count('/direct/references') == 2
    assert '/direct/deliveries' not in posts
    receipts = []
    for job, number in zip(jobs, (B2, B3, B4)):
        assert intake['delivery'].get(job)['state'] == 'success'
        assert intake['routes'].decision(intake['delivery'].get(job)['attempt_id'])['route'] == 'direct'
        row, statement = receipt_of(intake, job)
        assert row['state'] == 'accepted' and row['recipient_number'] == number
        assert statement['type'] == 'receipt' and statement['document_sha256'] == digest
        assert statement['recipient']['fax_number'] == number
        receipts.append(statement)
    assert len({statement['message_id'] for statement in receipts}) == 3
    # The first receipt lists where B's rules file every recipient; each says where its own copy went.
    assert receipts[0]['distribution']['recipients'] == [
        {'fax_number': B2, 'mailbox': 'Billing', 'held': None},
        {'fax_number': B3, 'mailbox': 'Records', 'held': None},
        {'fax_number': B4, 'mailbox': None,
         'held': f'No receiving rule files faxes to {B4}, so it waits in Received with no mailbox.'}]
    assert [statement['placement']['mailbox'] for statement in receipts] == ['Billing', 'Records', None]
    assert receipts[1]['carriage'] == receipts[2]['carriage'] == 'reference'
    # Each filing is its own received fax, filed by B's rules; B4 has no rule and waits with the reason.
    filed = received()
    assert [(to, label) for to, label, _, _ in filed] == [(B2, 'Billing'), (B3, 'Records'), (B4, None)]
    assert all(data == document for _, _, _, data in filed)
    texts = [received_text({'report': json.dumps(report)}) for _, _, report, _ in filed]
    assert texts[0] == ('Delivered directly by Valley Hospital to your intake, which files one copy for each of 3 '
                        'numbers; no telephone call.')
    assert texts[2].endswith(f'No receiving rule files faxes to {B4}, so it waits in Received with no mailbox.')
    from api.app.work.store import WorkStore
    assert WorkStore(b_engine()).feed(installation_hours=0) == 3
    # Sent says so for each fax, and the two references count as bytes saved, not money.
    from api.app.direct.distribute import sent_texts
    texts = sent_texts(intake['a'], intake['a'].store.recent())
    assert set(texts.values()) == {"Sent once to County Clinic's intake, which delivers to 3 recipients."}
    assert len(texts) == 3
    totals = reuse.ReuseStore(intake['a'].store.engine).totals(datetime(2000, 1, 1))
    assert totals == {'reference': {'documents': 2, 'full_bytes': 2 * len(document), 'sent_bytes': 0}}


@pytest.mark.asyncio
async def test_two_faxes_to_the_same_number_are_never_one_send(intake):
    await agree(intake)
    document = pdf_bytes('Sent twice on purpose')
    accept_to(intake, document, B2)
    accept_to(intake, document, B2)
    await run(intake, 2)
    posts = [path for method, path in intake['paths'] if method == 'POST']
    # Each went as its own delivery for B2 (the second as a reference to the first), never one send.
    assert '/direct/distributions' not in posts and posts.count('/direct/deliveries') == 1
    assert posts.count('/direct/references') == 1
    assert [to for to, _, _, _ in received()] == [B2, B2]


@pytest.mark.asyncio
async def test_a_withdrawn_agreement_sends_by_telephone_and_an_unheard_one_is_refused_signed(intake):
    row = await agree(intake)
    client = intake['b_client']
    # B ends it while A cannot be reached: A still thinks it is active.
    intake['to_a'].down = True
    (b_row,) = SendOnce(_b_service()).store.agreements_for()
    ended = client.post(f"/direct/send-once/{b_row['id']}/withdraw", headers=ADMIN)
    assert ended.status_code == 200 and ended.json()['state'] == 'withdrawn'
    assert SendOnce(intake['a']).store.agreement(row['id'])['state'] == 'active'
    job = accept_to(intake, pdf_bytes('After the end'), B3)
    conventional = await run(intake, 1)
    # B refused it, signed (nothing accepted), so it went by telephone in the same attempt.
    assert conventional.submissions == 1 and intake['delivery'].get(job)['state'] == 'in_progress'
    assert received() == []
    # Once A hears the withdrawal, faxes to those numbers go by telephone without asking B.
    intake['to_a'].down = False
    assert await SendOnce(_b_service()).tell_all() is False
    assert SendOnce(intake['a']).store.agreement(row['id'])['state'] == 'withdrawn'
    before = len(intake['paths'])
    accept_to(intake, pdf_bytes('Later'), B2)  # B3's fax is still on its call: a number takes one at a time
    conventional = await run(intake, 1)
    assert conventional.submissions == 1 and len(intake['paths']) == before


def accept_ruled(pair, document, number):
    """Accept a fax under the published sending rules, as POST /fax does: the decision is kept with the fax."""
    from types import SimpleNamespace
    from api.app.routing import rules_acceptance
    actor = SimpleNamespace(principal_id='person-anne', credential=None)
    configuration = pair['configuration']
    plan = rules_acceptance.prepare(configuration.engine, pair['snapshot'].active, actor=actor, destination=number,
                                    pages=1, document_sha256=hashlib.sha256(document).hexdigest())
    job, now = uuid4().hex, datetime.utcnow()
    (pair['data'] / f'{job}.pdf').write_bytes(document)
    with configuration._locked() as connection:
        configuration._accept_outbound_on(connection, pair['snapshot'].active, {
            'id': job, 'to_number': number, 'file_name': 'referral.pdf', 'tiff_path': '', 'status': 'queued',
            'pages': 1, 'created_at': now, 'updated_at': now})
        rules_acceptance.recorder(plan, job, actor)(connection, now)
    return job


@pytest.mark.asyncio
async def test_a_rule_that_requires_direct_delivery_accepts_a_number_the_intake_files(intake):
    from api.app.routing import envelope as envelopes
    from api.app.rules.store import RuleStore
    rules = RuleStore(intake['configuration'].engine)
    draft = rules.save_draft('organization', '', {'format': 1, 'limits': [
        {'id': 'l-direct', 'name': 'Only directly', 'on': True, 'when': {}, 'then': {'require_direct': True}}]},
        expected_version=0)
    rules.publish('organization', '', expected_active_revision=None, expected_draft_version=draft['version'])
    # Before the agreement, B2 has no partner: the rule holds the fax, and nothing is sent.
    held = accept_ruled(intake, pdf_bytes('Only directly, before'), B2)
    pinned = envelopes.load(intake['configuration'].engine, held)
    assert (pinned.decision.outcome, pinned.decision.reason) == ('blocked', 'needs_partner')
    # With the agreement, a fax to B2 is accepted under the same rule and goes to B's intake, never by telephone.
    await agree(intake)
    document = pdf_bytes('Only directly, after')
    job = accept_ruled(intake, document, B2)
    pinned = envelopes.load(intake['configuration'].engine, job)
    assert pinned.decision.outcome == 'route' and pinned.envelope.require_direct
    conventional = await run(intake, 1)
    assert conventional.submissions == 0 and intake['delivery'].get(job)['state'] == 'success'
    assert intake['routes'].decision(intake['delivery'].get(job)['attempt_id'])['route'] == 'direct'
    _, statement = receipt_of(intake, job)
    assert statement['placement'] == {'fax_number': B2, 'mailbox': 'Billing', 'held': None}
    assert [(to, label, data) for to, label, _, data in received()] == [(B2, 'Billing', document)]


def _b_service():
    from api.app.direct.http import service_for
    return service_for(main.app)


@pytest.mark.asyncio
async def test_the_intake_refuses_a_list_naming_a_number_outside_the_agreement(intake):
    await agree(intake, numbers=(B2, B3))
    a, client = intake['a'], intake['b_client']
    document = pdf_bytes('Routing check')
    message_id = uuid4().hex
    manifest, signature, ciphertext = seal(
        a.identity(), message_id=message_id, organization='Valley Hospital', fax_number=A_NUMBER,
        recipient_number=B2, recipient_signing_key=intake['b_on_a']['signing_key'],
        recipient_exchange_key=intake['b_on_a']['exchange_key'], document=document, pages=1)
    (row,) = SendOnce(a).store.agreements_for()
    routing = signed(a.identity(), {'type': 'distribution', 'recipient': intake['b_on_a']['signing_key'],
                                    'said_at': timestamp(), 'agreement': row['id'], 'message_id': message_id,
                                    'document_sha256': hashlib.sha256(document).hexdigest(),
                                    'recipients': [B2, B4]})
    refused = client.post('/direct/distributions', files={
        'manifest': (None, manifest), 'signature': (None, signature), 'routing': (None, routing['statement']),
        'routing_signature': (None, routing['signature']), 'document': ('d', ciphertext, 'application/octet-stream')})
    assert refused.status_code == 409 and '"reason":"not_covered"' in refused.json()['statement']
    assert received() == []


# D9: reuse -----------------------------------------------------------------------------------------------------

def originals_only(env):
    """B turns fax images off for A, so A sends original documents (a fax image differs on every send)."""
    answer = env['b_client'].post(f"/direct/peers/{env['a_on_b']['id']}/fax-images", headers=ADMIN,
                                  json={'accept': False})
    assert answer.status_code == 200, answer.text


@pytest.mark.asyncio
async def test_a_document_the_partner_holds_goes_as_a_reference(intake):
    originals_only(intake)
    document = pdf_bytes('A form sent every week')
    accept_to(intake, document, B_NUMBER)
    await run(intake, 1)
    second = accept_to(intake, document, B_NUMBER)
    await run(intake, 1)
    posts = [path for method, path in intake['paths'] if method == 'POST']
    assert posts == ['/direct/deliveries', '/direct/holdings', '/direct/references']
    row, statement = receipt_of(intake, second)
    assert row['state'] == 'accepted' and statement['carriage'] == 'reference'
    assert [data for _, _, _, data in received()] == [document, document]
    saving = reuse.ReuseStore(intake['a'].store.engine).saving(row['message_id'])
    assert (saving['carriage'], saving['full_bytes'], saving['sent_bytes']) == ('reference', len(document), 0)
    _, _, report, _ = received()[1]
    assert received_text({'report': json.dumps(report)}) == (
        'Delivered directly by Valley Hospital as a reference to a copy of this document it sent you before; no '
        'telephone call.')


@pytest.mark.asyncio
async def test_a_copy_the_partner_no_longer_holds_sends_the_whole_document_at_once(intake, monkeypatch):
    originals_only(intake)
    document = pdf_bytes('Deleted by retention')
    accept_to(intake, document, B_NUMBER)
    await run(intake, 1)
    with b_engine().connect() as connection:
        (path,) = connection.execute(sa.text("SELECT document_path FROM direct_deliveries WHERE direction='inbound'")).scalars()
    import os
    os.remove(path)
    # The partner says, signed, that it does not hold it: the whole document goes.
    second = accept_to(intake, document, B_NUMBER)
    await run(intake, 1)
    posts = [path for method, path in intake['paths'] if method == 'POST']
    assert posts[1:] == ['/direct/holdings', '/direct/deliveries']
    assert intake['delivery'].get(second)['state'] == 'success'
    # It says it holds it, but the copy is gone when the reference arrives: a signed miss, the whole document at
    # once under the same message, and nothing counted as saved.
    import app.direct.reuse as b_reuse  # B runs as the application package
    monkeypatch.setattr(b_reuse, 'answer_holdings', lambda service, statement, signature: (200, signed(
        service.identity(), {'type': 'holdings', 'recipient': intake['a'].identity().signing_key,
                             'digests': [hashlib.sha256(document).hexdigest()], 'answered_at': timestamp()})))
    with b_engine().begin() as connection:
        connection.execute(sa.text("UPDATE direct_deliveries SET document_path = '/nonexistent' "
                                   "WHERE direction='inbound'"))
    third = accept_to(intake, document, B_NUMBER)
    await run(intake, 1)
    posts = [path for method, path in intake['paths'] if method == 'POST']
    assert posts[3:] == ['/direct/holdings', '/direct/references', '/direct/deliveries']
    row, statement = receipt_of(intake, third)
    assert intake['delivery'].get(third)['state'] == 'success' and 'carriage' not in statement
    assert reuse.ReuseStore(intake['a'].store.engine).saving(row['message_id']) is None


def test_a_partner_answers_only_about_documents_that_partner_delivered(intake):
    """Nobody learns what another sender sent: holdings are scoped to the asking partner."""
    a, client = intake['a'], intake['b_client']
    with b_engine().begin() as connection:
        connection.execute(sa.text(
            "INSERT INTO direct_deliveries (id, direction, message_id, peer_id, recipient_number, digest, size_bytes, "
            "manifest, state, document_path, accepted_at, created_at, updated_at) VALUES "
            "(:id, 'inbound', :m, NULL, :n, :d, 10, '{}', 'accepted', :p, :t, :t, :t)"),
            {'id': uuid4().hex, 'm': uuid4().hex, 'n': B_NUMBER, 'd': 'e' * 64, 'p': '/etc/hosts',
             't': datetime.utcnow()})
    body = {'type': 'holdings_query', 'recipient': intake['b_on_a']['signing_key'], 'said_at': timestamp(),
            'signer': a.identity().signing_key, 'digests': ['e' * 64]}
    statement = canonical(body)
    answer = client.post('/direct/holdings', json={'statement': statement.decode(),
                                                   'signature': a.identity().sign(statement)})
    assert answer.status_code == 200 and json.loads(answer.json()['statement'])['digests'] == []
    forged = client.post('/direct/holdings', json={'statement': statement.decode(), 'signature': 'A' * 86})
    assert forged.status_code == 400


# D10: patches --------------------------------------------------------------------------------------------------

def ledger(pair, job, version):
    """A's case ledger: ``job`` carries version ``version`` of one lab report (same case, source and purpose)."""
    engine = pair['configuration'].engine
    entries = sa.Table('case_entries', sa.MetaData(), autoload_with=engine)
    sends = sa.Table('case_entry_sends', sa.MetaData(), autoload_with=engine)
    entry = uuid4().hex
    with engine.begin() as connection:
        connection.execute(entries.insert().values(
            id=entry, case_id='CASE-7', recipient=B_NUMBER, digest=hashlib.sha256(
                (pair['data'] / f'{job}.pdf').read_bytes()).hexdigest(), source='Lab', version=version,
            purpose='Referral', title='Lab report', page_count=1, created_at=datetime.utcnow()))
        connection.execute(sends.insert().values(id=uuid4().hex, entry_id=entry, job_id=job,
                                                 created_at=datetime.utcnow()))


@pytest.mark.asyncio
async def test_a_newer_version_goes_as_changes_checked_before_filing(intake):
    first = synthetic_pdf(1)
    second = first[:60_000] + b'Corrected result: 4.2 mmol/L' + first[60_000:]
    one = accept_to(intake, first, B_NUMBER)
    ledger(intake, one, 'v1')
    await run(intake, 1)
    two = accept_to(intake, second, B_NUMBER)
    ledger(intake, two, 'v2')
    await run(intake, 1)
    posts = [path for method, path in intake['paths'] if method == 'POST']
    assert posts == ['/direct/deliveries', '/direct/holdings', '/direct/patches']
    row, statement = receipt_of(intake, two)
    assert statement['carriage'] == 'patch' and statement['base_sha256'] == hashlib.sha256(first).hexdigest()
    assert [data for _, _, _, data in received()] == [first, second]
    saving = reuse.ReuseStore(intake['a'].store.engine).saving(row['message_id'])
    assert saving['carriage'] == 'patch' and 0 < saving['sent_bytes'] < len(second) // 10
    assert saving['full_bytes'] == len(second)
    from api.app.direct.distribute import sent_texts
    text = sent_texts(intake['a'], intake['a'].store.recent())[row['message_id']]
    assert text.startswith('Delivered directly to County Clinic as the changes to a version it already held; ')


@pytest.mark.asyncio
async def test_changes_that_do_not_rebuild_the_document_send_it_whole(intake, monkeypatch):
    first = synthetic_pdf(2)
    second = first[:30_000] + b'Amended' + first[30_000:]
    one = accept_to(intake, first, B_NUMBER)
    ledger(intake, one, 'v1')
    await run(intake, 1)
    real = reuse.make_delta
    # Changes that rebuild something else: the partner checks the result's SHA-256 and refuses, signed.
    monkeypatch.setattr(reuse, 'make_delta', lambda base, target, **kw: real(base, target[:-1] + b'X', **kw))
    two = accept_to(intake, second, B_NUMBER)
    ledger(intake, two, 'v2')
    await run(intake, 1)
    posts = [path for method, path in intake['paths'] if method == 'POST']
    assert posts == ['/direct/deliveries', '/direct/holdings', '/direct/patches', '/direct/deliveries']
    row, statement = receipt_of(intake, two)
    assert intake['delivery'].get(two)['state'] == 'success' and 'carriage' not in statement
    assert [data for _, _, _, data in received()] == [first, second]
    assert reuse.ReuseStore(intake['a'].store.engine).saving(row['message_id']) is None


def test_the_delta_rebuilds_exactly_and_refuses_another_version():
    base = synthetic_pdf(3, 50_000)
    target = base[:10_000] + b'inserted' + base[10_000:40_000] + base[45_000:]
    delta = reuse.make_delta(base, target)
    assert delta is not None and len(delta) < len(target) // 10
    assert reuse.apply_delta(base, delta, max_size=len(target)) == target
    with pytest.raises(reuse.DeltaError):
        reuse.apply_delta(base[:-1], delta, max_size=len(target))
    with pytest.raises(reuse.DeltaError):
        reuse.apply_delta(base, delta, max_size=len(target) - 1)
    # Unrelated bytes are not worth a delta.
    assert reuse.make_delta(base, synthetic_pdf(4, 50_000)) is None


def test_bytes_saved_are_counted_on_savings_and_never_as_money(intake):
    client = intake['b_client']
    before = client.get('/routing/savings', headers=ADMIN).json()
    assert before['direct_bytes'] == {'bytes_saved': 0, 'documents': 0, 'references': 0, 'patches': 0,
                                      'sentence': 'No documents went to partners as references or changes in the '
                                                  'last 30 days.'}
    savings = sa.Table('direct_byte_savings', sa.MetaData(), autoload_with=b_engine())
    now = datetime.utcnow()
    with b_engine().begin() as connection:
        connection.execute(savings.insert(), [
            {'id': uuid4().hex, 'message_id': uuid4().hex, 'peer_id': intake['a_on_b']['id'], 'carriage': 'reference',
             'document_sha256': 'a' * 64, 'base_sha256': None, 'full_bytes': 300_000, 'sent_bytes': 0,
             'created_at': now},
            {'id': uuid4().hex, 'message_id': uuid4().hex, 'peer_id': intake['a_on_b']['id'], 'carriage': 'patch',
             'document_sha256': 'b' * 64, 'base_sha256': 'a' * 64, 'full_bytes': 200_000, 'sent_bytes': 4_000,
             'created_at': now}])
    after = client.get('/routing/savings', headers=ADMIN).json()
    assert after['direct_bytes']['bytes_saved'] == 496_000 and after['direct_bytes']['documents'] == 2
    assert after['direct_bytes']['sentence'] == (
        '484 KB not sent in the last 30 days: 1 as a reference to a copy the partner held and 1 as only the changes '
        'to an earlier version. These are bytes over the internet, not money; the calls were already saved.')
    # Never money: the money total and every money part are exactly what they were.
    assert after['total_saved'] == before['total_saved'] and after['total_sentence'] == before['total_sentence']
    assert {key: value for key, value in after.items() if key not in ('direct_bytes', 'since')} == {
        key: value for key, value in before.items() if key not in ('direct_bytes', 'since')}
