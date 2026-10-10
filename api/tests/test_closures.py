"""When a line's copper closes (N16) and country service rules (M4), from synthetic files and installations.

The closure file follows the government copy's exact layout (semicolons, a quoted outline column, the columns read
from the real export on 2026-10-10) with synthetic dates; the rules, accounts and numbers are synthetic. Not yet run
against Orange's own trajectory file, which sits behind a browser check.
"""
from datetime import date

import pytest

from api.app.config_values import ConfigurationValues
from api.app.routing import closures, country_rules
from api.tests.test_rules_delivery import BASE, installation, publish
from api.tests.test_schema import database  # noqa: F401 (fixture)


HEADER = ('code_insee;nom_commune;Code_postal;departement;region;point_geo;forme_geo;fermeture_technique;'
          'fermeture_commerciale;commune_et_cp;nom_departement;lot;output usager;annee_fermeture_technique')
GOUV = '﻿' + '\n'.join([
    HEADER,
    '75056;Paris;75001;Paris;Île-de-France;48.85, 2.35;"{""coordinates"": [[[2.2, 48.8]]], ""type"": ""Polygon""}";'
    '2027-01-31;2026-01-31;Paris - 75001;Paris;3;L\'arrêt des services interviendra au plus tard le 31/01/2027.;2027',
    '2A004;Ajaccio;20000;Corse-du-Sud;Corse;41.9, 8.7;;2029-10-31;2027-01-31;Ajaccio - 20000;Corse-du-Sud;5_3;;2029',
    '37185;Pocé-sur-Cisse;37530;Indre-et-Loire;Centre-Val de Loire;47.4, 0.98;;;2026-01-31;;Indre-et-Loire;'
    'PreselectionLot6;;',
    'XXXXX;Nowhere;;;;;;;;;;;;',
    '',
])
ORANGE = '\n'.join(['Code INSEE,Nom de la commune,Date de fermeture commerciale,Date de fermeture technique,Lot',
                    '75056,Paris,31/01/2026,31/01/2028,4', ''])
PARIS_LINE, TORONTO_LINE = '+33142000000', '+14165550100'
TODAY = date(2026, 10, 10)


def test_the_government_copy_is_read_with_both_dates_and_corsican_codes():
    found, skipped = closures.parse_file(GOUV, source='gouv')
    assert skipped == 1
    by = {item.code_insee: item for item in found}
    assert (by['75056'].technical, by['75056'].commercial, by['75056'].lot) == (date(2027, 1, 31), date(2026, 1, 31),
                                                                                '3')
    assert by['2A004'].commune == 'Ajaccio' and by['37185'].technical is None
    orange, _ = closures.parse_file(ORANGE, source='orange')
    assert orange[0].technical == date(2028, 1, 31) and orange[0].commercial == date(2026, 1, 31)
    with pytest.raises(closures.ClosureFileError, match='both closure dates'):
        closures.parse_file('code_insee;nom_commune\n75056;Paris\n')
    with pytest.raises(closures.ClosureFileError):
        closures.parse_file(GOUV, source='somewhere')


def test_a_site_commune_must_be_a_french_insee_code():
    from api.app.rules.compile import document_problems

    def commune_problems(site):
        return [problem for problem in document_problems('organization', {'sites': [site]})
                if 'French INSEE code' in problem.message]
    assert commune_problems({'key': 'paris', 'name': 'Paris', 'country': 'FR', 'commune': '75056'}) == []
    assert commune_problems({'key': 'ajaccio', 'name': 'Ajaccio', 'country': 'FR', 'commune': '2A004'}) == []
    assert commune_problems({'key': 'leeds', 'name': 'Leeds', 'country': 'GB', 'commune': '75056'})
    assert commune_problems({'key': 'bad', 'name': 'Bad', 'commune': 'Paris'})


ENVIRONMENT = {**BASE, 'FAX_BACKEND': 'sip', 'SIP_TRUNK_PRESET': 'telnyx', 'SIP_TRUNK_AUTH': 'ip',
               'SIP_TRUNK_HOST': 'sip.telnyx.com', 'SIP_TRUNK_CALLER_ID': PARIS_LINE, 'SIP_TRUNK_DIDS': PARIS_LINE,
               'FAX_DEFAULT_COUNTRY': 'FR', 'FAX_OUTBOUND_ROUTES': 'signalwire'}


def test_french_lines_show_their_communes_dates_and_carrier_notices_warn_before_the_date(database, tmp_path):  # noqa: F811
    from api.app.config_profiles import ProviderConfiguration
    env = installation(database, tmp_path, ENVIRONMENT, outbound=ProviderConfiguration('sip'))
    values = env.snapshot.active.values
    publish(env, {'format': 1, 'sites': [{'key': 'paris', 'name': 'Paris office', 'country': 'FR', 'commune': '75056',
                                          'accounts': ['sip']},
                                         {'key': 'lyon', 'name': 'Lyon office', 'country': 'FR', 'commune': '69123'}]})
    found, _ = closures.parse_file(GOUV, source='gouv')
    closures.import_file(database, found, source='gouv', file_date=date(2025, 10, 20),
                         source_url='https://data.economie.gouv.fr/example', actor={'name': 'Anne'})
    shown = closures.view(database, values, today=TODAY)
    paris = next(site for site in shown['sites'] if site['site'] == 'paris')
    assert (paris['closure']['technical'], paris['closure']['file_date'], paris['state']) == (
        '2027-01-31', '2025-10-20', 'soon')
    assert paris['sentence'].startswith('Copper in Paris closes on 31 January 2027. Before then, run a receipt test')
    lyon = next(site for site in shown['sites'] if site['site'] == 'lyon')
    assert lyon['state'] == 'unknown' and 'No imported closure file lists commune 69123' in lyon['sentence']
    [line] = shown['lines']
    assert (line['number'], line['site'], line['state']) == (PARIS_LINE, 'paris', 'soon')
    # Orange's own newer file wins over the government copy.
    orange, _ = closures.parse_file(ORANGE, source='orange')
    closures.import_file(database, orange, source='orange', file_date=date(2025, 12, 19))
    assert closures.closure_for(database, '75056').technical == date(2028, 1, 31)
    assert closures.view(database, values, today=TODAY)['lines'][0]['state'] == 'later'
    assert {item['source'] for item in closures.files(database)} == {'gouv', 'orange'}
    # A Canadian line whose carrier wrote: warned now, history kept when withdrawn.
    closures.record_notice(database, TORONTO_LINE, closes_on=date(2026, 11, 4), carrier='Bell',
                           received_on=date(2026, 9, 1), note='Letter about copper home phone service')
    toronto = next(item for item in closures.view(database, values, today=TODAY)['lines']
                   if item['number'] == TORONTO_LINE)
    assert toronto['state'] == 'soon' and toronto['sentences'][0].startswith('Bell says this line closes on 4 '
                                                                             'November 2026.')
    closures.remove_notice(database, TORONTO_LINE)
    assert TORONTO_LINE not in closures.notices(database)
    with pytest.raises(closures.NoticeError):
        closures.remove_notice(database, TORONTO_LINE)
    with pytest.raises(closures.NoticeError):
        closures.record_notice(database, 'not a number', closes_on=date(2026, 11, 4))
    with database.connect() as connection:
        assert connection.exec_driver_sql('SELECT count(*) FROM line_notices').scalar() == 2


def test_accounts_in_the_uae_show_tdras_rules_as_not_confirmed_until_you_confirm_them(database, tmp_path):  # noqa: F811
    env = installation(database, tmp_path, {**BASE, 'FAX_DEFAULT_COUNTRY': 'AE'})
    values = env.snapshot.active.values
    shown = country_rules.view(database, values)
    assert {item['country'] for item in shown['countries']} == {'AE', 'SA'}
    phaxio = next(item for item in shown['accounts'] if item['account'] == 'phaxio')
    assert (phaxio['country'], phaxio['confirmed']) == ('AE', False)
    assert phaxio['sentence'].startswith('Phaxio: not confirmed. Faxes still go; confirm')
    country_rules.confirm(database, 'phaxio', 'AE', evidence='Provider works with du; contract 2026-04', actor={
        'name': 'Anne'})
    assert next(item for item in country_rules.view(database, values)['accounts']
                if item['account'] == 'phaxio')['confirmed'] is True
    country_rules.withdraw(database, 'phaxio', 'AE')
    assert next(item for item in country_rules.view(database, values)['accounts']
                if item['account'] == 'phaxio')['confirmed'] is False
    with pytest.raises(country_rules.EligibilityError):
        country_rules.confirm(database, 'phaxio', 'FR', evidence='x')
    with pytest.raises(country_rules.EligibilityError):
        country_rules.withdraw(database, 'phaxio', 'AE')
    us = ConfigurationValues.from_environment({**BASE, 'FAX_DEFAULT_COUNTRY': 'US'})
    assert country_rules.view(database, us)['accounts'] == []


@pytest.fixture
def closures_cli(monkeypatch, tmp_path):
    from api.tests.test_cli import Cli, _serve
    for client in _serve(monkeypatch, tmp_path, FAX_DEFAULT_COUNTRY='AE'):
        yield Cli(client)


def test_the_command_line_imports_closures_records_notices_and_confirms_country_rules(closures_cli, tmp_path):
    file = tmp_path / 'fermeture-reseau-cuivre.csv'
    file.write_text(GOUV, encoding='utf-8')
    imported = closures_cli('numbers', 'move', 'import-closures', file, '--file-date', '2025-10-20')
    assert imported.exit_code == 0, imported.stdout + imported.stderr
    assert 'Imported 3 communes, 1 lines skipped.' in imported.stdout
    assert 'Government copy: 3 communes, file of 20 October 2025.' in imported.stdout
    noted = closures_cli('numbers', 'move', 'notice', TORONTO_LINE, '--closes', '2026-11-04', '--carrier', 'Bell')
    assert noted.exit_code == 0 and 'Bell says this line closes on 4 November 2026.' in noted.stdout
    assert closures_cli('numbers', 'move', 'notice', TORONTO_LINE).exit_code != 0  # no date
    assert closures_cli('numbers', 'move', 'notice', TORONTO_LINE, '--remove').exit_code == 0
    shown = closures_cli.json('numbers', 'move', 'closures')
    assert shown['lines'] == [] and shown['files'][0]['communes'] == 3
    rules = closures_cli('providers', 'accounts', 'country-rules')
    assert rules.exit_code == 0 and 'Phaxio: not confirmed' in rules.stdout and 'tdra.gov.ae' in rules.stdout
    confirmed = closures_cli('providers', 'accounts', 'country-rules', '--confirm', 'phaxio', '--country', 'AE',
                             '--evidence', 'Provider works with du')
    assert confirmed.exit_code == 0 and 'Phaxio: confirmed by' in confirmed.stdout
    assert closures_cli('providers', 'accounts', 'country-rules', '--confirm', 'phaxio').exit_code != 0
