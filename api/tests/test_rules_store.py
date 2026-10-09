"""The rules store on SQLite and PostgreSQL: drafts, publish, immutable revisions and a fax's decisions."""
from datetime import datetime
import json

import pytest
import sqlalchemy as sa

from api.app import schema
from api.app.rules import model
from api.app.rules.evaluate import decide
from api.app.rules.store import RuleStore, RulesConflict, RulesInputError
from api.tests.test_schema import database  # noqa: F401 - fixture


NOW = datetime(2026, 10, 7, 15, 0)
DOCUMENT = {'format': 1, 'limits': [], 'routes': [
    {'id': 'r-uk', 'name': 'UK numbers by HumbleFax', 'on': True, 'when': {'destination': {'countries': ['GB']}},
     'then': {'use': 'humblefax'}}]}
ACCOUNTS = (model.Account('sip', 'sip', default=True, automatic=True), model.Account('humblefax', 'humblefax'))


@pytest.fixture
def store(database):
    schema.upgrade_schema(database)
    return RuleStore(database)


def test_drafts_publish_and_heads_move_one_version_at_a_time(store):
    assert store.active('organization') is None and store.draft('organization') is None
    draft = store.save_draft('organization', '', DOCUMENT, expected_version=0, actor_name='Jane Smith')
    assert draft['version'] == 1 and draft['base_revision'] is None
    with pytest.raises(RulesConflict):
        store.save_draft('organization', '', DOCUMENT, expected_version=0)
    with pytest.raises(RulesConflict):
        store.publish('organization', '', expected_active_revision=None, expected_draft_version=2)
    first = store.publish('organization', '', expected_active_revision=None, expected_draft_version=1, note='First',
                          actor_name='Jane Smith')
    assert (first['number'], first['note'], first['actor_name'], first['parent_id']) == (1, 'First', 'Jane Smith', None)
    assert store.draft('organization') is None
    store.save_draft('organization', '', {**DOCUMENT, 'routes': []}, expected_version=0)
    with pytest.raises(RulesConflict):
        store.publish('organization', '', expected_active_revision=None, expected_draft_version=1)
    second = store.publish('organization', '', expected_active_revision=1, expected_draft_version=1)
    assert second['number'] == 2 and second['parent_id'] == first['id']
    assert store.active('organization')['number'] == 2
    assert [row['number'] for row in store.history('organization')] == [2, 1]
    # Revision 1 still reads exactly as published.
    assert store.revision('organization', '', 1) == first
    # Each scope numbers its own revisions.
    store.save_draft('mailbox', 'mailbox-1', DOCUMENT, expected_version=0)
    assert store.publish('mailbox', 'mailbox-1', expected_active_revision=None, expected_draft_version=1)['number'] == 1
    assert set(store.active_scopes()) == {'organization', 'mailbox:mailbox-1'}


def test_a_broken_draft_cannot_be_published_and_restore_makes_a_new_draft(store):
    store.save_draft('organization', '', {'format': 1, 'routes': [{'id': 'r', 'name': 'R', 'when': {}, 'then': {}}]},
                     expected_version=0)
    with pytest.raises(RulesInputError) as refused:
        store.publish('organization', '', expected_active_revision=None, expected_draft_version=1)
    assert refused.value.problems and store.active('organization') is None
    store.save_draft('organization', '', DOCUMENT, expected_version=1)
    store.publish('organization', '', expected_active_revision=None, expected_draft_version=2)
    restored = store.restore('organization', '', 1)
    assert json.loads(restored['document']) == DOCUMENT and restored['base_revision'] == 1
    with pytest.raises(RulesInputError):
        store.restore('organization', '', 7)


def test_a_faxs_decisions_are_inserted_compact_and_never_rewritten(store):
    store.save_draft('organization', '', DOCUMENT, expected_version=0)
    store.publish('organization', '', expected_active_revision=None, expected_draft_version=1)
    compiled = store.compiled_active()
    facts = model.Facts('+442071234567', '2026-10-07T15:00:00', country='GB')
    decision = decide(compiled, facts, ACCOUNTS)
    with store.engine.begin() as connection:
        connection.execute(sa.text(
            "INSERT INTO fax_jobs (id, to_number, file_name, tiff_path, status, pages, backend, created_at, updated_at) "
            "VALUES ('job-1', '+442071234567', 'note.pdf', '', 'queued', 1, 'sip', :now, :now)"), {'now': NOW})
        store.record_decision_on(connection, job_id='job-1', facts=facts, decision=decision, now=NOW)
        later = decide({}, facts, ACCOUNTS)
        store.record_decision_on(connection, job_id='job-1', facts=facts, decision=later, sequence=2,
                                 reason='The current rules were applied to waiting faxes.', now=NOW)
        with pytest.raises(ValueError):
            store.record_decision_on(connection, job_id='job-1', facts=model.Facts('+15555550100', NOW.isoformat()),
                                     decision=decision, sequence=3)
    current = store.current_decision('job-1')
    assert current['sequence'] == 2 and model.Decision.from_json(current['decision']).route == model.AUTOMATIC
    [first] = store.recent_decisions()
    stored = model.Decision.from_json(first['decision'])
    assert stored == decision.compact() and stored.route.rule_id == 'r-uk'
    assert json.loads(first['revisions']) == {'organization': decision.revisions[0].revision_id}
    # Replaying the stored facts under the stored revision gives the stored decision.
    assert decide(compiled, model.Facts.from_json(first['facts']), ACCOUNTS).compact() == stored
    with pytest.raises(sa.exc.IntegrityError):
        with store.engine.begin() as connection:
            store.record_decision_on(connection, job_id='job-1', facts=facts, decision=decision, sequence=1)
