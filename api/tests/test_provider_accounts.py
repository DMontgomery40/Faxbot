"""Provider accounts (provider-rules design §3, WP-B): storage, the account list and its HTTP routes.

Synthetic credentials only; nothing contacts a provider.
"""
import json
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from api.tests.test_schema import database  # noqa: F401 (fixture)
from app import accounts
from app.config_activation import ConfigurationManager
from app.config_profiles import ConfigurationDocument
from app.config_store import ConfigurationStore
from app.schema import upgrade_schema


class Catalog:
    provider_ids = frozenset({'phaxio', 'sinch', 'sip', 'humblefax', 'efax'})

    def get(self, identity):
        if identity not in self.provider_ids:
            raise ValueError('unknown provider')
        return SimpleNamespace(id=identity, manifest=None, kind='cloud', traits=ConfigurationDocument({
            'requires_tiff': identity == 'sip', 'requires_ami': identity == 'sip', 'supports_inbound': True}))


UK = {'provider': 'sinch', 'label': 'Sinch (UK)', 'site': 'leeds', 'sends': True, 'receives': True, 'enabled': True,
      'numbers': ['+442071234567'], 'settings': {'project_id': 'synthetic-uk-project'},
      'credentials': {'api_key': 'synthetic-uk-key', 'api_secret': 'synthetic-uk-secret',
                      'inbound_basic_user': 'uk-user', 'inbound_basic_pass': 'synthetic-uk-pass'},
      'limits': {}}


def manager(database, tmp_path):
    upgrade_schema(database)
    store = ConfigurationStore(database, tmp_path / 'configuration.key')
    return ConfigurationManager(store, catalog_loader=lambda values: Catalog())


def environment(tmp_path, **extra):
    return {'FAXBOT_CONFIG_PATH': str(tmp_path / 'no-legacy.json'), 'FAX_BACKEND': 'sinch',
            'SINCH_PROJECT_ID': 'synthetic-project', 'SINCH_API_KEY': 'synthetic-key',
            'SINCH_API_SECRET': 'synthetic-secret', 'INBOUND_ENABLED': 'true', **extra}


def payload(store, revision_id):
    """The decrypted stored payload of one revision."""
    with store.engine.connect() as connection:
        row = connection.execute(sa.select(store.revisions).where(store.revisions.c.id == revision_id)).mappings().one()
        installation = store._head(connection)['installation_id']
    return store._cipher().open(row['envelope'], installation_id=installation, kind='revision', record_id=revision_id)


def with_accounts(control, snapshot, documents, changes=None):
    values = snapshot.desired.values.with_patch(changes or {})
    return control.store.apply(snapshot, values, restart_required=False, actor='trusted-fixture',
                               providers=control._prepare_apply(snapshot, values, snapshot.desired.plugins.as_dict(),
                                                                Catalog(), ConfigurationDocument(documents))[0],
                               accounts=ConfigurationDocument(documents))


# -- storage -------------------------------------------------------------------------------------------------

def test_a_revision_without_extra_accounts_is_stored_and_profiled_exactly_as_before(database, tmp_path):
    control = manager(database, tmp_path)
    first = control.initialize(environment(tmp_path))
    assert set(payload(control.store, first.active.id)) == {'environment', 'profiles', 'plugins'}
    profile = control.store.read_profile(first.active.profile_id('outbound'))
    changed = control.patch(first, {'inbound_retention_days': 45}, actor='admin')
    # No accounts key appears, and the provider profile (and so its digest) is the same one.
    assert set(payload(control.store, changed.active.id)) == {'environment', 'profiles', 'plugins'}
    assert changed.active.profile_id('outbound') == first.active.profile_id('outbound')
    assert control.store.read_profile(changed.active.profile_id('outbound')).configuration == profile.configuration
    assert first.active.accounts.as_dict() == {} and changed.active.values.provider_accounts == {}
    # The account list is derived from the provider's own settings: one primary account per provider in use.
    listed = accounts.all_accounts(changed.active.values)
    assert [(item.key, item.primary, item.default_sending) for item in listed] == [('sinch', True, True)]


def test_extra_accounts_round_trip_and_survive_every_other_write(database, tmp_path):
    control = manager(database, tmp_path)
    first = control.initialize(environment(tmp_path))
    added = with_accounts(control, first, {'sinch-uk': UK})
    assert set(payload(control.store, added.active.id)) == {'environment', 'profiles', 'plugins', 'accounts'}
    assert added.active.accounts.as_dict() == {'sinch-uk': UK}
    assert added.active.values.provider_accounts == {'sinch-uk': UK}
    # A settings save, a provider-page change, credentials from the environment and owner recovery each keep it.
    after = control.patch(added, {'fax_header': 'Leeds'}, actor='admin')
    assert after.active.accounts.as_dict() == {'sinch-uk': UK}
    after = control.patch_plugin(after, 'sinch', settings={'project_id': 'synthetic-project-2'}, actor='admin')
    assert after.active.accounts.as_dict() == {'sinch-uk': UK}
    after = control.apply_environment(after, {'sinch_api_secret': 'synthetic-rotated'})
    assert after.active.accounts.as_dict() == {'sinch-uk': UK}
    after = control.store.recover_bootstrap('r' * 40)
    assert after.active.accounts.as_dict() == {'sinch-uk': UK}
    reread = control.store.read()
    assert reread.active.accounts.as_dict() == {'sinch-uk': UK}
    assert [item.key for item in accounts.all_accounts(reread.active.values)] == ['sinch', 'sinch-uk']
    # A write that changes only the accounts is a new revision; repeating it changes nothing.
    removed = with_accounts(control, reread, {})
    assert removed.generation == reread.generation + 1
    assert set(payload(control.store, removed.active.id)) == {'environment', 'profiles', 'plugins'}
    assert with_accounts(control, removed, {}).generation == removed.generation


def test_an_extra_account_has_its_own_configuration_and_nothing_leaks_from_the_first(database, tmp_path):
    control = manager(database, tmp_path)
    first = control.initialize(environment(tmp_path, SINCH_INBOUND_BASIC_USER='first-user',
                                           SINCH_INBOUND_BASIC_PASS='first-pass', SINCH_BASE_URL='https://fax.example'))
    added = with_accounts(control, first, {'sinch-uk': {**UK, 'credentials': {
        'api_key': 'synthetic-uk-key', 'api_secret': 'synthetic-uk-secret'}}})
    values = added.active.values
    own = accounts.account_values(values, 'sinch-uk')
    assert (own.sinch_project_id, own.sinch_api_key, own.sinch_inbound_basic_user, own.sinch_base_url) == (
        'synthetic-uk-project', 'synthetic-uk-key', '', '')
    assert accounts.account_values(values, 'sinch') is values
    configuration = accounts.account_configuration(values, 'sinch-uk', catalog=Catalog(), plugin_state={})
    assert configuration.credentials['api_key'] == 'synthetic-uk-key'
    assert configuration.settings['project_id'] == 'synthetic-uk-project'
    primary = accounts.account_configuration(values, 'sinch', catalog=Catalog(), plugin_state={})
    assert primary == control.store.read_profile(added.active.profile_id('outbound')).configuration


def test_rules_see_every_sending_account_and_the_automatic_ones_are_todays(database, tmp_path):
    from app.rules.explain import _accounts_before_provider_accounts, accounts_from_values
    control = manager(database, tmp_path)
    first = control.initialize(environment(tmp_path, FAX_OUTBOUND_ROUTES='phaxio', PHAXIO_API_KEY='p-key',
                                           PHAXIO_API_SECRET='p-secret'))
    before = _accounts_before_provider_accounts(first.active.values)
    assert [account for account in accounts_from_values(first.active.values) if account.automatic] == list(before)
    added = with_accounts(control, first, {'sinch-uk': UK})
    listed = accounts_from_values(added.active.values)
    assert [(item.key, item.automatic, item.site) for item in listed] == [
        ('sinch', True, None), ('phaxio', True, None), ('sinch-uk', False, 'leeds')]
    assert accounts.account_permitted(added.active, 'sinch-uk')
    assert not accounts.account_permitted(added.active, 'sinch-uk', envelope=('sinch',))
    assert not accounts.account_permitted(added.active, 'nobody')


def test_turning_an_account_off_and_its_primary_overlay(database, tmp_path):
    control = manager(database, tmp_path)
    first = control.initialize(environment(tmp_path))
    values = first.active.values
    documents, changes = accounts.patched(values, 'sinch', {'site': None, 'label': 'Main Sinch'},
                                          provider_ids=Catalog.provider_ids)
    assert documents == {'sinch': {'label': 'Main Sinch'}} and changes == {}
    with pytest.raises(accounts.AccountsError, match='default sending account'):
        accounts.patched(values, 'sinch', {'enabled': False}, provider_ids=Catalog.provider_ids)
    with pytest.raises(accounts.AccountsError, match='own provider page'):
        accounts.patched(values, 'sinch', {'credentials': {'api_key': 'x'}}, provider_ids=Catalog.provider_ids)


def test_an_extra_default_for_sending_is_refused_while_the_first_account_of_its_provider_is_set_up(database,
                                                                                                    tmp_path):
    """Temporary (WP-C removes it): routes and cost records are keyed by provider id, so the two would mix."""
    control = manager(database, tmp_path)
    first = control.initialize(environment(tmp_path))
    added = with_accounts(control, first, {'sinch-uk': UK})
    with pytest.raises(accounts.AccountsError) as refused:
        accounts.patched(added.active.values, 'sinch-uk', {'default_sending': True}, provider_ids=Catalog.provider_ids)
    assert refused.value.status == 409
    assert str(refused.value) == ("Sinch (UK) can't be the default for sending while your first Sinch account is set "
                                  'up. Make the first Sinch account the default, or turn it off first.')
    # A provider whose first account is not set up: the extra account becomes the default and builds the profile.
    current = control.store.read()
    documents = {'humble-2': {'provider': 'humblefax', 'label': 'HumbleFax (second)', 'sends': True,
                              'receives': False, 'enabled': True,
                              'credentials': {'access_key': 'hf-access', 'secret_key': 'hf-secret'}}}
    with_extra = with_accounts(control, current, documents)
    documents, changes = accounts.patched(with_extra.active.values, 'humble-2', {'default_sending': True},
                                          provider_ids=Catalog.provider_ids)
    assert changes == {'outbound_backend': 'humblefax'} and documents['humble-2']['default_sending'] is True
    switched = with_accounts(control, with_extra, documents, changes)
    profile = control.store.read_profile(switched.active.profile_id('outbound')).configuration
    assert profile.provider_id == 'humblefax' and profile.credentials['access_key'] == 'hf-access'
    assert accounts.default_sending_key(switched.active.values) == 'humble-2'
    assert accounts.all_accounts(switched.active.values)[0].key == 'humble-2'


def test_numbers_belong_to_one_receiving_account(database, tmp_path):
    control = manager(database, tmp_path)
    first = control.initialize(environment(tmp_path))
    added = with_accounts(control, first, {'sinch-uk': UK})
    second = {'key': 'sinch-eu', 'provider': 'sinch', 'receives': True, 'numbers': ['+44 20 7123 4567'],
              'settings': {'project_id': 'eu'}, 'credentials': {'api_key': 'k', 'api_secret': 's'}}
    with pytest.raises(accounts.AccountsError, match='already receives on Sinch'):
        accounts.added(added.active.values, second, provider_ids=Catalog.provider_ids)
    with pytest.raises(accounts.AccountsError, match='kept for Faxbot'):
        accounts.added(added.active.values, {**second, 'key': 'phaxio'}, provider_ids=Catalog.provider_ids)
    with pytest.raises(accounts.AccountsError, match='masked value'):
        accounts.added(added.active.values, {**second, 'numbers': [], 'credentials': {'api_key': '****abcd'}},
                       provider_ids=Catalog.provider_ids)


def cost_records(tmp_path, rows):
    """A minimal attempt-cost table with the columns health reads; only the queries are under test here."""
    engine = sa.create_engine('sqlite:///' + str(tmp_path / 'costs.db'))
    costs = sa.Table('delivery_attempt_costs', sa.MetaData(), sa.Column('id', sa.String(40), primary_key=True),
                     sa.Column('route', sa.String(64)), sa.Column('outcome', sa.String(16)),
                     sa.Column('estimated_cost_micros', sa.Integer()), sa.Column('currency', sa.String(3)),
                     sa.Column('reported_cost_micros', sa.Integer()), sa.Column('reported_currency', sa.String(3)),
                     sa.Column('settled_cost_micros', sa.Integer()), sa.Column('created_at', sa.DateTime()))
    costs.metadata.create_all(engine)
    with engine.begin() as connection:
        for number, row in enumerate(rows):
            connection.execute(costs.insert().values(id=str(number), currency='USD', **row))
    return engine


def test_daily_spending_limit_resets_at_local_midnight_and_failing_needs_most_of_the_last_faxes(database, tmp_path):
    from datetime import datetime
    control = manager(database, tmp_path)
    first = control.initialize(environment(tmp_path, FAX_TIME_ZONE='America/Denver'))
    added = with_accounts(control, first, {'sinch-uk': {**UK, 'limits': {'daily_spend_micros': 1_000_000,
                                                                         'currency': 'USD'}}})
    values = added.active.values
    account = accounts.account_named(values, 'sinch-uk')
    # 23:30 in Denver on October 6 is 05:30 UTC on October 7; local midnight was 06:00 UTC on October 6.
    now = datetime(2026, 10, 7, 5, 30)
    assert accounts.local_midnight(values, now) == datetime(2026, 10, 6, 6, 0)
    engine = cost_records(tmp_path, [
        # Before local midnight: yesterday's spending does not count.
        {'route': 'sinch-uk', 'outcome': 'success', 'estimated_cost_micros': 900_000,
         'created_at': datetime(2026, 10, 6, 5, 59)},
        # Today: the settled amount wins over the report, the report over the estimate.
        {'route': 'sinch-uk', 'outcome': 'success', 'estimated_cost_micros': 100, 'reported_cost_micros': 400_000,
         'reported_currency': 'USD', 'settled_cost_micros': 600_000, 'created_at': datetime(2026, 10, 6, 7, 0)},
        {'route': 'sinch-uk', 'outcome': 'success', 'estimated_cost_micros': 100, 'reported_cost_micros': 300_000,
         'reported_currency': 'USD', 'created_at': datetime(2026, 10, 6, 8, 0)},
        {'route': 'sinch', 'outcome': 'success', 'estimated_cost_micros': 5_000_000,
         'created_at': datetime(2026, 10, 6, 8, 0)},
    ])
    assert accounts.spent_today(engine, values, account, now=now) == 900_000
    assert accounts.health(values, account, engine, now=now)[0] == 'waiting'
    with engine.begin() as connection:
        connection.execute(sa.text("INSERT INTO delivery_attempt_costs (id, route, outcome, estimated_cost_micros, "
                                   "currency, created_at) VALUES ('x', 'sinch-uk', 'failed', 100000, 'USD', "
                                   "'2026-10-06 09:00:00')"))
    state, sentence, _ = accounts.health(values, account, engine, now=now)
    assert (state, sentence) == ('spending_limit', 'It has cost $1.00 today, its daily limit; Faxbot uses it again '
                                                   'after midnight.')
    # After the next local midnight the account is usable again.
    assert accounts.health(values, account, engine, now=datetime(2026, 10, 7, 6, 1))[0] == 'waiting'
    with engine.begin() as connection:
        for number in range(3):
            connection.execute(sa.text("INSERT INTO delivery_attempt_costs (id, route, outcome, currency, created_at) "
                                       f"VALUES ('f{number}', 'sinch-uk', 'failed', 'USD', '2026-10-07 07:0{number}:00')"))
    state, sentence, _ = accounts.health(values, account, engine, now=datetime(2026, 10, 7, 8, 0))
    assert (state, sentence) == ('failing', '4 of its last 7 faxes failed. Check its settings with Sinch.')


# -- over HTTP --------------------------------------------------------------------------------------------------

BOOTSTRAP = 'synthetic-accounts-bootstrap'
ADMIN = {'X-API-Key': BOOTSTRAP}


@pytest.fixture
def http(isolated_installation, monkeypatch):
    for name, value in {'INBOUND_ENABLED': 'true', 'REQUIRE_API_KEY': 'true', 'API_KEY': BOOTSTRAP,
                        'PUBLIC_API_URL': 'https://testserver', 'FAX_BACKEND': 'sinch',
                        'SINCH_PROJECT_ID': 'synthetic-project', 'SINCH_API_KEY': 'synthetic-key',
                        'SINCH_API_SECRET': 'synthetic-secret', 'FAXBOT_CONSOLE_ORIGINS': 'https://testserver',
                        'MAX_REQUESTS_PER_MINUTE': '0'}.items():
        monkeypatch.setenv(name, value)
    from app import main
    with TestClient(main.app, base_url='https://testserver', headers={'Origin': 'https://testserver'}) as client:
        yield client


def add_body(generation, **changes):
    return {'key': 'sinch-uk', 'provider': 'sinch', 'label': 'Sinch (UK)', 'site': None, 'sends': True,
            'receives': True, 'numbers': ['+442071234567'],
            'limits': {'at_once': None, 'calls_per_second': None, 'daily_limit': {'currency': 'USD', 'amount': '25.00'}},
            'settings': {'project_id': 'synthetic-uk-project'},
            'credentials': {'api_key': 'synthetic-uk-key', 'api_secret': 'synthetic-uk-secret',
                            'inbound_basic_user': 'uk-user', 'inbound_basic_pass': 'synthetic-uk-pass'},
            'expected_generation': generation, **changes}


def test_the_account_list_add_change_and_health_over_http(http):
    state = http.get('/admin/providers/accounts', headers=ADMIN).json()
    assert [account['key'] for account in state['accounts']] == ['sinch']
    assert state['default_sending'] == 'sinch' and state['accounts'][0]['primary'] is True
    assert {kind['id'] for kind in state['providers']} >= {'sinch', 'phaxio', 'humblefax', 'efax', 'sip'}
    sinch = next(kind for kind in state['providers'] if kind['id'] == 'sinch')
    assert {field['name']: field['secret'] for field in sinch['fields']}['api_secret'] is True
    # A stale generation is refused before anything is written.
    stale = http.post('/admin/providers/accounts', headers=ADMIN, json=add_body(state['generation'] - 1))
    assert stale.status_code == 409
    added = http.post('/admin/providers/accounts', headers=ADMIN, json=add_body(state['generation']))
    assert added.status_code == 200, added.text
    result = added.json()
    uk = next(account for account in result['accounts'] if account['key'] == 'sinch-uk')
    assert uk['webhook_address'] == 'https://testserver/sinch-inbound/sinch-uk'
    assert uk['health'] == {'state': 'waiting', 'sentence': 'No fax has arrived on it yet.'}
    assert uk['limits']['daily_limit'] == {'currency': 'USD', 'amount': '25.00'}
    assert sorted(uk['secrets_set']) == ['api_key', 'api_secret', 'inbound_basic_pass']
    assert 'synthetic-uk' not in json.dumps(result).replace('synthetic-uk-project', '')
    assert result['generation'] == state['generation'] + 1
    health = http.get('/admin/providers/accounts/sinch-uk/health', headers=ADMIN).json()
    assert health['state'] == 'waiting'
    assert 'add /sinch-inbound/sinch-uk to that list' in ' '.join(health['details'])
    off = http.patch('/admin/providers/accounts/sinch-uk', headers=ADMIN,
                     json={'enabled': False, 'expected_generation': result['generation']})
    assert off.status_code == 200, off.text
    uk = next(account for account in off.json()['accounts'] if account['key'] == 'sinch-uk')
    assert uk['enabled'] is False and uk['health']['state'] == 'off'
    refused = http.patch('/admin/providers/accounts/sinch', headers=ADMIN,
                         json={'enabled': False, 'expected_generation': off.json()['generation']})
    assert refused.status_code == 409
    assert refused.json()['detail'].endswith('Make another account the default first.')
    # A rejected body never echoes what was sent.
    bad = http.post('/admin/providers/accounts', headers=ADMIN, json={**add_body(1), 'credentials': {'api_key': 5}})
    assert bad.status_code == 422 and '5' not in json.dumps(bad.json()['detail'][0].get('msg'))


def test_the_account_routes_need_provider_permissions(http):
    assert http.get('/admin/providers/accounts').status_code == 401
    assert http.post('/admin/providers/accounts', json=add_body(1)).status_code in (401, 403)
