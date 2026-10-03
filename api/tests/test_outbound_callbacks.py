"""Captured callback ownership, independent of HTTP route acceptance."""
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
