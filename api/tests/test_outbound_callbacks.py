"""Captured callback ownership, independent of HTTP route acceptance."""
import base64
import hashlib
import hmac

import pytest

from api.tests.test_outbound_store import installation, accept
from api.tests.test_schema import database
from api.app.config_profiles import ProviderConfiguration
from api.app.outbound_callbacks import CapturedCallbacks, CallbackRejected


def submitting(installation):
    configuration, store, snapshot = installation
    profile = ProviderConfiguration('phaxio', credentials={'callback_token': 'old-callback-token'},
        settings={'callback_url': 'https://fax.example.invalid/phaxio-callback?account=one', 'verify_signature': True})
    snapshot = configuration.apply(snapshot, snapshot.active.values, actor='test',
        restart_required=False, providers={'outbound': profile})
    job = accept((configuration, store, snapshot))
    claim = store.claim('worker-one')
    store.begin_submission(claim)
    return job, claim, snapshot


def test_callback_uses_original_token_and_url_after_rotation_before_ack(installation, monkeypatch):
    configuration, store, _ = installation
    job, claim, snapshot = submitting(installation)
    configuration.apply(snapshot, snapshot.active.values, actor='test', restart_required=False,
        providers={'outbound': ProviderConfiguration('phaxio', credentials={'callback_token': 'new-token'})})
    verified = []
    def verifier(token, url, fields, files, signature):
        verified.append((token, url, fields, files, signature))
        return True
    monkeypatch.setattr('api.app.outbound_callbacks.verify_phaxio_signature', verifier)
    fields = [('id', 'remote-one'), ('status', 'success')]
    owner = CapturedCallbacks(store)
    assert owner.receive('phaxio', job, claim.attempt_id, fields=fields, files=[], signature='synthetic-proof')
    assert verified == [('old-callback-token', 'https://fax.example.invalid/phaxio-callback?account=one&job_id='
                         + job + '&attempt_id=' + claim.attempt_id, fields, [], 'synthetic-proof')]
    assert store.get(job)['state'] == 'success'
    assert not owner.receive('phaxio', job, claim.attempt_id, fields=fields, files=[], signature='synthetic-proof')
    store.record_receipt(claim, provider_sid='remote-one', status='in_progress')
    assert store.get(job)['state'] == 'success'


def test_unauthenticated_or_wrong_attempt_callback_cannot_mutate_delivery(installation, monkeypatch):
    _, store, _ = installation
    job, claim, _ = submitting(installation)
    monkeypatch.setattr('api.app.outbound_callbacks.verify_phaxio_signature', lambda *args: False)
    owner = CapturedCallbacks(store)
    before = store.history(job)
    for attempt in (claim.attempt_id, 'unrelated-attempt'):
        with pytest.raises(CallbackRejected):
            owner.receive('phaxio', job, attempt, fields=[('id', 'remote-one'), ('status', 'success')],
                          files=[], signature='invalid')
    assert store.get(job)['state'] == 'submitting' and store.history(job) == before


def test_signed_conflicting_payload_is_refused_instead_of_choosing_an_alias(installation, monkeypatch):
    _, store, _ = installation
    job, claim, _ = submitting(installation)
    monkeypatch.setattr('api.app.outbound_callbacks.verify_phaxio_signature', lambda *args: True)
    with pytest.raises(CallbackRejected):
        CapturedCallbacks(store).receive('phaxio', job, claim.attempt_id,
            fields=[('id', 'one'), ('fax[id]', 'two'), ('status', 'success')], files=[], signature='signed')
    assert store.get(job)['state'] == 'submitting'


@pytest.mark.parametrize('fields', [
    [('fax', '{"id":123,"status":"success"}')],
    [('fax[id]', '123'), ('fax[status]', 'success')],
])
def test_authenticated_phaxio_payload_versions_share_guarded_transition(installation, monkeypatch, fields):
    _, store, _ = installation
    job, claim, _ = submitting(installation)
    monkeypatch.setattr('api.app.outbound_callbacks.verify_phaxio_signature', lambda *args: True)
    assert CapturedCallbacks(store).receive('phaxio', job, claim.attempt_id,
        fields=fields, files=[], signature='synthetic')
    assert store.get(job)['state'] == 'success'


def test_signalwire_uses_separate_captured_signing_key_and_rejects_files(installation, monkeypatch):
    configuration, store, snapshot = installation
    profile = ProviderConfiguration('signalwire', credentials={'webhook_signing_key': 'signing-only', 'api_token': 'send-only'})
    snapshot = configuration.apply(snapshot, snapshot.active.values, actor='test',
        restart_required=False, providers={'outbound': profile})
    job = accept((configuration, store, snapshot))
    claim = store.claim('worker-one')
    store.begin_submission(claim)
    seen = []
    def verifier(key, url, fields, signature):
        seen.append((key, url))
        return True
    monkeypatch.setattr('api.app.outbound_callbacks.verify_signalwire_signature', verifier)
    fields = [('FaxSid', 'remote-one'), ('FaxStatus', 'delivered')]
    owner = CapturedCallbacks(store)
    with pytest.raises(CallbackRejected):
        owner.receive('signalwire', job, claim.attempt_id, fields=fields,
                      files=[('file', b'not part of form signature')], signature='synthetic')
    assert not seen
    assert owner.receive('signalwire', job, claim.attempt_id, fields=fields, files=[], signature='synthetic')
    assert seen == [('signing-only', snapshot.active.values.public_api_url
                     + '/signalwire-callback?job_id=' + job + '&attempt_id=' + claim.attempt_id)]
    assert store.get(job)['state'] == 'success'


@pytest.mark.parametrize('provider', ['phaxio', 'signalwire'])
def test_callback_deduplication_preserves_signed_repeated_name_order(installation, provider):
    configuration, store, snapshot = installation
    token = 'synthetic-callback-key'
    credential = 'callback_token' if provider == 'phaxio' else 'webhook_signing_key'
    snapshot = configuration.apply(snapshot, snapshot.active.values, actor='test',
        restart_required=False, providers={'outbound': ProviderConfiguration(provider,
            credentials={credential: token}, settings={'callback_url': 'https://fax.example.invalid/callback'})})
    job = accept((configuration, store, snapshot))
    claim = store.claim('worker-one')
    assert store.begin_submission(claim)
    url = 'https://fax.example.invalid/callback?job_id=' + job + '&attempt_id=' + claim.attempt_id
    sid_name, status_name = ('id', 'status') if provider == 'phaxio' else ('FaxSid', 'FaxStatus')
    fields = [('Tag', 'one'), (sid_name, 'remote-one'), (status_name, 'sending'),
              ('Blank', ''), ('Tag', 'two')]
    files = [('zfile', b'one'), ('afile', b'abc'), ('zfile', b'two')] if provider == 'phaxio' else []

    def signature(form, attachments):
        # Independently frame the documented HMAC; do not call the verifier or
        # callback identity helpers to derive these synthetic signed requests.
        message = url + ''.join(name + value for name, value in sorted(form, key=lambda part: part[0]))
        message += ''.join(name + hashlib.sha1(data).hexdigest()
                           for name, data in sorted(attachments, key=lambda part: part[0]))
        digest = hmac.new(token.encode(), message.encode(), hashlib.sha1)
        return digest.hexdigest() if provider == 'phaxio' else base64.b64encode(digest.digest()).decode()

    signed = signature(fields, files)
    owner = CapturedCallbacks(store)
    assert owner.receive(provider, job, claim.attempt_id, fields=fields, files=files, signature=signed)
    before, history = store.get(job), store.history(job)
    reordered = [fields[3], fields[1], fields[2], fields[0], fields[4]]
    reordered_files = [files[1], files[0], files[2]] if files else []
    assert not owner.receive(provider, job, claim.attempt_id,
        fields=reordered, files=reordered_files, signature=signed)
    assert store.get(job) == before and store.history(job) == history

    changed = [fields[4], fields[1], fields[2], fields[3], fields[0]]
    with pytest.raises(CallbackRejected):
        owner.receive(provider, job, claim.attempt_id, fields=changed, files=files, signature=signed)
    assert store.get(job) == before and store.history(job) == history
    # A freshly signed repeated-name ordering is a different event. Sorting
    # values as well as names would incorrectly collapse this observation.
    assert owner.receive(provider, job, claim.attempt_id,
        fields=changed, files=files, signature=signature(changed, files))
    assert store.get(job)['version'] == before['version'] + 1
    assert len(store.history(job)) == len(history) + 1
    if files:
        before, history = store.get(job), store.history(job)
        changed_files = [files[2], files[1], files[0]]
        with pytest.raises(CallbackRejected):
            owner.receive(provider, job, claim.attempt_id,
                fields=changed, files=changed_files, signature=signature(changed, files))
        assert store.get(job) == before and store.history(job) == history
        assert owner.receive(provider, job, claim.attempt_id,
            fields=changed, files=changed_files, signature=signature(changed, changed_files))
        assert store.get(job)['version'] == before['version'] + 1
        assert len(store.history(job)) == len(history) + 1
