"""The frozen sending-rules contract: Facts, Decision and Envelope round-trip as canonical JSON."""
import json

import pytest

from api.app.rules import model


def _facts(**changes):
    values = dict(
        destination='+442071234567', accepted_at='2026-10-07T15:04:05', country='GB', recipient_id='destination-1',
        preferred_route='humblefax', partner=False, own_number=False, sslfax_seen=True,
        alternate=model.Alternate('+18005550100', 'approval-1', 'Jane Smith', '2026-10-01T09:00:00',
                                  'Same intake, confirmed by phone'),
        sender=model.Sender('person-1', 'person', None, ('group-a', 'group-b')), mailbox_id='mailbox-1',
        workflow='referrals', labels=('clinical', 'legal'), pages=3, size_bytes=48_213, case_packet=False,
        urgent=True, by_call=False, time_zone='Europe/London',
        quotes=(model.Quote('sip', 31_000, 'USD', origin='any', pages=3), model.Quote('humblefax', None, None),
                model.Quote('sip', 0, 'USD', number='alternate')))
    values.update(changes)
    return model.Facts(**values)


def _decision(facts):
    rule = model.Source('rule', model.ORGANIZATION, '', 7, 'r-uk', 'UK numbers go through Sinch, then the trunk')
    envelope = model.Envelope(
        'ordered', ('sinch-uk', 'sip'), local=False, direct=True, require_encryption=False,
        caps=(model.Cap(500_000, 'USD', model.Source('rule', 'mailbox', 'mailbox-1', 2, 'l-cap', 'Under 50 cents')),),
        holds=(model.Hold('approval', rule, separate_approver=True),
               model.Hold('window', rule, release_at='2026-10-07T17:00:00')),
        when_busy='next', preferred=None, alternate='use', dial=facts.alternate, page_layout='as_receiver_allows',
        strict_fallback=True)
    return model.Decision(
        'held', envelope, rule, facts.digest(),
        revisions=(model.RevisionRef('organization', '', 'revision-7', 7),
                   model.RevisionRef('mailbox', 'mailbox-1', 'revision-m2', 2)),
        site='leeds', workflow='referrals', alternate_source=rule, layout_source=rule,
        excluded=(model.Excluded('humblefax', 'never', model.Source('rule', rule_id='l-hf-uk')),),
        trace=(model.Step('limit', 'matched', rule_id='l-hf-uk', revision=7),
               model.Step('route', 'not_matched', scope='mailbox', scope_id='mailbox-1', revision=2,
                          rule_id='r-leeds', field='sender.sites'),
               model.Step('preferred', 'not_applied', note='excluded'),
               model.Step('route', 'matched', revision=7, rule_id='r-uk')))


def test_facts_and_decisions_round_trip_as_canonical_json():
    facts = _facts()
    text = facts.to_json()
    assert text == json.dumps(json.loads(text), sort_keys=True, separators=(',', ':'), ensure_ascii=True)
    assert model.Facts.from_json(text) == facts
    decision = _decision(facts)
    assert model.Decision.from_json(decision.to_json()) == decision
    assert decision.revision_ids == {'organization': 'revision-7', 'mailbox:mailbox-1': 'revision-m2'}


def test_the_facts_digest_is_stable_across_releases():
    """The canonical encoding is part of the contract: stored digests must keep matching."""
    facts = model.Facts(destination='+15555550123', accepted_at='2026-10-07T15:00:00')
    assert facts.to_json() == (
        '{"accepted_at":"2026-10-07T15:00:00","alternate":null,"approximate":false,"by_call":false,'
        '"case_packet":false,"country":null,"destination":"+15555550123","format":1,"labels":[],"mailbox_id":null,'
        '"own_number":false,"pages":0,"partner":false,"preferred_route":null,"quotes":[],"recipient_id":null,'
        '"sender":{"groups":[],"key_id":null,"kind":null,"principal_id":null},"size_bytes":0,"sslfax_seen":false,'
        '"time_zone":"","urgent":false,"workflow":null}')
    assert facts.digest() == model.Facts.from_json(facts.to_json()).digest()
    assert _facts().digest() != _facts(pages=4).digest()


def test_older_json_reads_with_defaults_and_unknown_keys_are_ignored():
    stored = {'destination': '+15555550123', 'accepted_at': '2026-10-07T15:00:00', 'added_later': {'x': 1}}
    assert model.Facts.from_json(json.dumps(stored)) == model.Facts('+15555550123', '2026-10-07T15:00:00')


@pytest.mark.parametrize('broken', [
    {'destination': '+15555550123', 'accepted_at': '2026-10-07T15:00:00', 'pages': '3'},
    {'destination': '+15555550123', 'accepted_at': '2026-10-07T15:00:00', 'urgent': 1},
    {'destination': '+15555550123', 'accepted_at': '2026-10-07T15:00:00+00:00'},
    {'destination': '+15555550123', 'accepted_at': '2026-10-07T15:00:00', 'labels': ['b', 'a']},
    {'destination': '+15555550123', 'accepted_at': '2026-10-07T15:00:00', 'sender': {'kind': 'robot'}},
    {'destination': '', 'accepted_at': '2026-10-07T15:00:00'},
])
def test_stored_facts_with_the_wrong_shape_are_refused(broken):
    with pytest.raises(ValueError):
        model.Facts.from_json(json.dumps(broken))


def test_envelopes_and_decisions_refuse_unknown_values():
    rule = model.Source('rule', rule_id='r-1')
    with pytest.raises(ValueError):
        model.Envelope('fastest')
    with pytest.raises(ValueError):
        model.Envelope('ordered', ('sip', 'sip'))
    with pytest.raises(ValueError):
        model.Envelope('ordered', ('direct',))
    with pytest.raises(ValueError):
        model.Envelope('ordered', page_layout='tiny')
    with pytest.raises(ValueError):
        model.Envelope('ordered', caps=(model.Cap(1, 'USD', rule), model.Cap(2, 'USD', rule)))
    with pytest.raises(ValueError):
        model.Decision('blocked', model.Envelope('automatic'), model.AUTOMATIC, 'f' * 64)
    with pytest.raises(ValueError):
        model.Decision('route', model.Envelope('automatic'), model.AUTOMATIC, 'f' * 64, reason='needs_partner')
    with pytest.raises(ValueError):
        model.Hold('no_route', rule)
    with pytest.raises(ValueError):
        model.Quote('sip', 100, None)
    with pytest.raises(ValueError):
        model.Account('direct', 'sip')
    with pytest.raises(ValueError):
        model.Account('Sinch UK', 'sinch')
    blocked = model.Decision('blocked', model.Envelope('automatic'), model.AUTOMATIC, 'f' * 64,
                             reason='no_allowed_account')
    assert model.Decision.from_json(blocked.to_json()) == blocked


def test_scope_names():
    assert model.scope_name('organization') == 'organization'
    assert model.scope_name('mailbox', 'mailbox-1') == 'mailbox:mailbox-1'
    assert model.parse_scope('workflow:referrals') == ('workflow', 'referrals')
    assert model.parse_scope('organization') == ('organization', '')
    for bad in ('team:x', 'mailbox:', 'workflow'):
        with pytest.raises(ValueError):
            model.parse_scope(bad)
    with pytest.raises(ValueError):
        model.scope_name('organization', 'x')


def test_rate_rows_are_digits_and_non_negative_amounts():
    row = model.RateRow('country:GB', '44', 9_000, 0, 0, 6, 6, source_url='https://example.test/rates')
    assert row.destination_prefix == '44'
    with pytest.raises(ValueError):
        model.RateRow('any', '+44', 1, 0, 0, 1, 0)
    with pytest.raises(ValueError):
        model.RateRow('any', '44', -1, 0, 0, 1, 0)


def test_the_document_vocabulary_names_every_action_once():
    assert not set(model.ROUTE_ACTIONS) & set(model.LIMIT_ACTIONS)
    assert set(model.ROUTE_SETTINGS) & set(model.LIMIT_ACTIONS) == {'alternate_number'}
    assert set(model.CONDITIONS) == {'destination', 'sender', 'workflows', 'labels', 'document', 'urgent',
                                    'real_call', 'time'}
