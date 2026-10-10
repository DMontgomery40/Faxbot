"""An analog line through a gateway (N8): gateway presets, Asterisk's file, one call per line, *70 and the local
calling area as $0 prices that the real pricing and planner rank (routing/analog.py), SQLite and PostgreSQL.

Everything is synthetic: documentation addresses (RFC 1918/5737), 555 numbers and made-up local prefix lists in
the Local Calling Guide's column layout as the vendor research read it on 2026-10-10. Not yet run against a real
gateway, line or carrier bill.
"""
from datetime import datetime
from pathlib import Path

import pytest

from app import ami, capacity, hylafax_engine, sip_trunk
from app.config_profiles import ConfigurationDocument
from app.config_values import ConfigurationValues
from app.routing import analog
from app.routing.costs import RateCard
from app.routing.store import RouteStore
from api.app.schema import upgrade_schema
from api.tests.test_schema import database  # noqa: F401 (fixture)

GATEWAYS = ('grandstream-ht813', 'grandstream-gxw410x', 'patton-smartnode-fxo', 'audiocodes-mp11x-fxo')
LINE = {'provider': 'sip', 'label': 'Denver office line', 'receives': True, 'numbers': ['+13034260100'],
        'settings': {'preset': 'grandstream-ht813', 'host': '192.168.1.50', 'auth': 'ip', 'transport': 'udp',
                     'caller_id': '+13034260100'}}
FIRST = {'FAX_BACKEND': 'sip', 'SIP_TRUNK_PRESET': 'telnyx', 'SIP_TRUNK_USERNAME': 'faxbotuser',
         'SIP_TRUNK_PASSWORD': 'synthetic-Trunk-Pass!42', 'SIP_TRUNK_CALLER_ID': '+15555550100',
         'SIP_TRUNK_DIDS': '+15555550100', 'FAX_DATA_DIR': '/faxdata', 'FAX_DEFAULT_COUNTRY': 'US',
         'FAX_OUTBOUND_ROUTES': 'sip-line'}
LOCAL, TOLL = '+13034265555', '+16502530000'
LCG_PAGE = """<html><body><h1>Local prefixes</h1><table>
<tr><th>NPA</th><th>NXX</th><th>Rate Centre</th><th>Region</th><th>Plan Type</th><th>Call Type</th>
<th>Monthly Limit</th><th>Note</th><th>Effective</th></tr>
<tr><td>303</td><td>426</td><td>Denver</td><td>CO</td><td>Basic</td><td>Flat</td><td></td><td></td><td></td></tr>
<tr><td>303</td><td>298</td><td>Denver</td><td>CO</td><td>Basic</td><td>Flat</td><td></td><td></td><td></td></tr>
<tr><td>720</td><td>555</td><td>Denver</td><td>CO</td><td>Basic</td><td>Flat</td><td></td><td></td><td></td></tr>
<tr><td>970</td><td>555</td><td>Greeley</td><td>CO</td><td>Extended</td><td>Measured</td><td>60</td><td></td><td></td></tr>
</table></body></html>"""


def values(documents=None, **environment):
    found = ConfigurationValues.from_environment({**FIRST, **environment})
    return found.with_provider_accounts(ConfigurationDocument({'sip-line': LINE} if documents is None
                                                              else documents))


# -- presets ---------------------------------------------------------------------------------------------------------

def test_the_gateway_family_is_on_the_local_network_one_call_per_line_and_sourced():
    for preset_id in GATEWAYS:
        preset = sip_trunk.PRESETS[preset_id]
        assert preset.kind == sip_trunk.ANALOG_LINE and preset.analog_line and preset.phone_system
        assert preset.auth_modes == ('ip',) and preset.lines == 1 and preset.t38
        assert preset.dial_formats == ('local', 'local_area') and preset.admin_steps
        assert all(source.read_on == '2026-10-10' for source in preset.sources)
        assert 'not yet run against a real' in ' '.join(preset.notes)
        assert any('*70' in step for step in preset.admin_steps)
        ConfigurationValues.from_environment({'SIP_TRUNK_PRESET': preset_id})  # the setting accepts it
    assert sip_trunk.PRESETS['grandstream-ht813'].port == 5062  # the HT813's FXO port
    catalog = {item['id']: item for item in sip_trunk.preset_catalog()}
    assert catalog['grandstream-ht813']['kind'] == 'analog_line' and catalog['grandstream-ht813']['lines'] == 1
    assert list(catalog)[-1] == 'custom'  # "Another carrier" stays last


def test_the_gateway_renders_as_a_local_network_peer_beside_the_carrier():
    rendered = sip_trunk.render_pjsip(values())
    section = rendered.split('[trunk-sip-line-endpoint]', 1)[1].split('\n\n', 1)[0]
    assert 't38_udptl=yes' in section and 'outbound_auth' not in section and 'outbound_proxy' not in section
    assert 'set_var=FAXBOT_TRUNK=sip-line' in section and 'allow=ulaw,alaw' in section
    assert 'contact=sip:192.168.1.50:5062' in rendered and 'match=192.168.1.50' in rendered
    assert '[trunk-sip-line-reg]' not in rendered  # the gateway and Faxbot recognise each other by address
    udp = rendered.split('[transport-udp]', 1)[1].split('\n\n', 1)[0]
    assert f'external_signaling_address={sip_trunk.LAN_ADDRESS}' in udp and 'local_net' not in udp
    # A UDP carrier and the gateway cannot share one connection; the sentence names both kinds.
    clash = values(SIP_TRUNK_PRESET='flowroute', SIP_TRUNK_AUTH='ip', SIP_TRUNK_USERNAME='12345678',
                   SIP_TRUNK_TRANSPORT='udp')
    assert 'phone system or analog line gateway' in sip_trunk.trunk_problems(clash)['sip-line']


def test_one_call_per_line_unless_you_set_calls_at_once():
    line = sip_trunk.trunk_for(values(), 'sip-line')
    assert capacity.trunk_calls_at_once(line.values) == 1
    four = values({'sip-line': {**LINE, 'limits': {'at_once': 4}}})
    assert capacity.trunk_calls_at_once(sip_trunk.trunk_for(four, 'sip-line').values) == 4
    assert capacity.limited_accounts(values())['sip-line'].at_once == 1


def test_star_70_goes_in_front_only_on_an_analog_line_and_reaches_both_engines():
    line = values({'sip-line': {**LINE, 'settings': {**LINE['settings'], 'dial_prefix': '*70'}}})
    trunk = sip_trunk.effective_trunk(sip_trunk.trunk_for(line, 'sip-line').values, for_calls=True)
    assert sip_trunk.dial_number(trunk, LOCAL) == '*7013034265555'
    with pytest.raises(sip_trunk.TrunkConfigurationError):
        sip_trunk.effective_trunk(ConfigurationValues.from_environment({
            'SIP_TRUNK_PRESET': 'avaya-ipoffice', 'SIP_TRUNK_AUTH': 'ip', 'SIP_TRUNK_HOST': '192.168.10.5',
            'SIP_TRUNK_DIAL_FORMAT': 'local', 'SIP_TRUNK_DIAL_PREFIX': '*70'}))
    with pytest.raises(ValueError):
        ConfigurationValues.from_environment({'SIP_TRUNK_DIAL_PREFIX': '#70'})
    fields = ami.originate_fields_for(line, 'a' * 32, LOCAL, '/faxdata/x.tiff', attempt_id='b' * 32, trunk='sip-line')
    assert fields['Channel'] == 'PJSIP/*7013034265555@trunk-sip-line-endpoint'
    plan = hylafax_engine.call_plan(fields, 'a' * 32, 'b' * 32,
                                    endpoints=sip_trunk.rendered_endpoints(line))
    assert plan.startswith('*7013034265555/+13034260100/') and plan.endswith('/trunk-sip-line-endpoint')
    dialplan = (Path(__file__).resolve().parents[2] / 'asterisk/etc/asterisk/extensions.conf').read_text()
    assert 'FAXBOT_DIAL=${FILTER(0123456789+*,${CUT(FAXBOT_PLAN,/,1)})}' in dialplan  # * survives the engine path


# -- the local calling area ----------------------------------------------------------------------------------------

def test_the_saved_local_prefixes_page_is_read_by_its_columns_and_plans():
    found = analog.read_local_list(LCG_PAGE, 'lprefix.html')
    assert found.kind == 'html' and found.plans == {'Basic': 3, 'Extended': 1}
    with pytest.raises(analog.AnalogLineError, match='several calling plans: Basic \\(3 prefixes\\); Extended'):
        analog.choose_plan(found)
    assert analog.choose_plan(found, 'basic') == [('303', '298'), ('303', '426'), ('720', '555')]
    with pytest.raises(analog.AnalogLineError, match='no calling plan called Gold'):
        analog.choose_plan(found, 'Gold')


@pytest.mark.parametrize('text, kind', [
    ('303-426\n(303) 298\n1-720-555\n+1 303 426 0100\nnot a prefix\n', 'text'),
    ('NPA,NXX,Rate Centre\n303,426,Denver\n303,298,Denver\n720,555,Denver\n', 'csv'),
    ('<?xml version="1.0"?><root><prefix><npa>303</npa><nxx>426</nxx></prefix>'
     '<prefix><npa>303</npa><nxx>298</nxx></prefix><prefix><npa>720</npa><nxx>555</nxx></prefix></root>', 'xml'),
])
def test_a_carrier_list_a_csv_and_an_xml_answer_read_the_same(text, kind):
    found = analog.read_local_list(text)
    assert found.kind == kind
    assert analog.choose_plan(found) == [('303', '298'), ('303', '426'), ('720', '555')]


def test_unreadable_or_unsafe_files_are_refused_with_one_sentence():
    for text in ('<!DOCTYPE x [<!ENTITY a "b">]><x/>', '<html><table><tr><td>no</td></tr></table></html>',
                 'nothing here', '<?xml version="1.0"?><root><broken>'):
        with pytest.raises(analog.AnalogLineError):
            analog.choose_plan(analog.read_local_list(text))
    with pytest.raises(analog.AnalogLineError, match='larger than 4 MB'):
        analog.read_local_list(b'1' * (analog.MAX_BYTES + 1))
    assert analog.line_prefix('+1 303 426 0100') == ('303', '426') and analog.line_prefix('303-426') == ('303', '426')
    with pytest.raises(analog.AnalogLineError):
        analog.line_prefix('12')


def _install(database):  # noqa: F811
    upgrade_schema(database)
    store = RouteStore(database)
    store.replace_cards([RateCard(None, 'sip-telnyx', 'outbound', 'Telnyx', 'USD', 5000, 0, 0, 60, 60, None,
                                  datetime(2026, 10, 7))])
    return store


def _choices(store, settings, number):
    from app.routing.plan import RoutePlanner
    from app.routing.pricing import prices_for
    prices = prices_for(store, settings, number, 1, bound='sip')
    plan = RoutePlanner(store).plan(to_number=number, bound='sip', values=settings, pages=1, alternates=True,
                                    prices=prices)
    return [choice.route.key for choice in plan.choices], prices


def test_local_numbers_go_out_on_the_line_at_no_cost_and_toll_numbers_by_the_cheapest_known_price(database):  # noqa: F811
    store = _install(database)
    settings = values()
    # Before an import the line has no price: unknown is never $0, so the carrier with a known price goes first.
    order, prices = _choices(store, settings, LOCAL)
    assert prices['sip-line'].micros is None and order[0] == 'sip'
    with pytest.raises(analog.AnalogLineError, match='charges a minute for calls outside the local area'):
        analog.import_local_calls(database, store, settings, 'sip-line', LCG_PAGE, plan='Basic')
    with pytest.raises(analog.AnalogLineError, match="own prefix 303-211 is not in this list"):
        analog.import_local_calls(database, store, settings, 'sip-line', LCG_PAGE, plan='Basic', line='303-211',
                                  toll_per_minute='0.10')
    with pytest.raises(analog.AnalogLineError, match='not an analog line'):
        analog.import_local_calls(database, store, settings, 'sip', LCG_PAGE, plan='Basic', toll_per_minute='0')
    view = analog.import_local_calls(database, store, settings, 'sip-line', LCG_PAGE, plan='Basic', line='303-426',
                                     toll_per_minute='0.10', monthly_fee='45',
                                     source_url='https://www.localcallingguide.com/lca_prefix.php?npa=303&nxx=426')
    assert view['local_prefixes'] == 3 and view['monthly_fee'] == '$45.00'
    assert view['sentence'].startswith('3 local prefixes go out on this line at no extra cost; other numbers cost')
    order, prices = _choices(store, settings, LOCAL)
    assert prices['sip-line'].micros == 0 and order[0] == 'sip-line'
    order, prices = _choices(store, settings, TOLL)
    assert prices['sip-line'].micros > prices['sip'].micros and order[0] == 'sip'
    # A later import replaces the local rows and keeps a row you saved yourself on the line's card.
    from app.routing.origin_rates import OriginRate, save_rows, saved
    card = next(item for item in store.current_cards() if item.provider_id == 'sip-line')
    mine = OriginRate('sip-line', 'any', '1650', 'USD', 1000, 0, 0, 60, 0, None, datetime(2026, 10, 10))
    save_rows(database, card.id, list(saved(database, ['sip-line'])) + [mine])
    analog.import_local_calls(database, store, settings, 'sip-line', '303-426\n', toll_per_minute=None)
    prefixes = sorted(row.destination_prefix for row in saved(database, ['sip-line']))
    assert prefixes == ['1303426', '1650']
    assert analog.is_local(database, 'sip-line', 'grandstream-ht813', LOCAL)
    assert not analog.is_local(database, 'sip-line', 'grandstream-ht813', '+13032980000')


def test_a_line_that_is_not_a_delivery_route_says_how_to_make_it_one(database):  # noqa: F811
    store = _install(database)
    unrouted = values(FAX_OUTBOUND_ROUTES='')
    analog.import_local_calls(database, store, unrouted, 'sip-line', '303-426\n', toll_per_minute='0.10')
    order, prices = _choices(store, unrouted, LOCAL)
    # The automatic choice uses the default sending account and the listed routes only: the line is not a candidate.
    assert 'sip-line' not in order and order[0] == 'sip'
    view = analog.line_view(database, store, unrouted, 'sip-line')
    assert view['routed'] is False
    assert view['route_sentence'] == ('Faxbot does not choose this line by itself yet: add it to your delivery routes '
                                      'with faxbot system settings set outbound_routes=sip-line, or name it in a '
                                      'sending rule under Providers → Rules.')
    assert analog.line_view(database, store, values(), 'sip-line')['routed'] is True


def test_calls_the_gateway_forwards_under_the_lines_number_are_filed_under_it():
    """The gateway forwards each call on the line to Faxbot under the line's own number (the admin steps); the
    line's endpoint names its trunk on every call, and the number in either written form is the stored one."""
    from app.inbound.http import received_number
    from app.inbound.sip_handover import receiving_trunk
    assert received_number('3034260100', 'US') == received_number('13034260100', 'US') == '+13034260100'
    assert received_number('200', 'US') == '200'  # a short forward target would not match: the steps say so
    for preset_id in ('grandstream-ht813', 'grandstream-gxw410x', 'patton-smartnode-fxo'):
        assert any("line's own number with its area code" in step for step in sip_trunk.PRESETS[preset_id].admin_steps)
    assert receiving_trunk(values(), {'trunk': 'sip-line', 'to_number': '3034260100'}) == 'sip-line'
    assert 'set_var=FAXBOT_TRUNK=sip-line' in sip_trunk.render_pjsip(values())
    assert sip_trunk.trunk_numbers(values())['sip-line'] == ('+13034260100',)


def test_ten_digits_for_local_numbers_when_the_exchange_refuses_a_1(database, monkeypatch):  # noqa: F811
    store = _install(database)
    local_area = values({'sip-line': {**LINE, 'settings': {**LINE['settings'], 'dial_format': 'local_area'}}})
    analog.import_local_calls(database, store, local_area, 'sip-line', '303-426\n', toll_per_minute='0.10')
    monkeypatch.setattr(ami, '_database', lambda: database)
    near = ami.originate_fields_for(local_area, 'a' * 32, LOCAL, '/faxdata/x.tiff', trunk='sip-line')
    far = ami.originate_fields_for(local_area, 'a' * 32, TOLL, '/faxdata/x.tiff', trunk='sip-line')
    assert near['Channel'] == 'PJSIP/3034265555@trunk-sip-line-endpoint'
    assert far['Channel'] == 'PJSIP/16502530000@trunk-sip-line-endpoint'
    every = ami.originate_fields_for(values(), 'a' * 32, LOCAL, '/faxdata/x.tiff', trunk='sip-line')
    assert every['Channel'] == 'PJSIP/13034265555@trunk-sip-line-endpoint'  # 'local': 1 and ten digits always


# -- the console's and the command line's paths ----------------------------------------------------------------------

@pytest.fixture
def line_client(monkeypatch, tmp_path):
    """An installation whose first trunk is an HT813 on the local network, sending turned off."""
    from fastapi.testclient import TestClient
    from api.tests.test_access_management_http import ORIGIN, _environment
    from app.main import app
    _environment(monkeypatch, tmp_path)
    for name, value in {'FAX_BACKEND': 'sip', 'SIP_TRUNK_PRESET': 'grandstream-ht813', 'SIP_TRUNK_AUTH': 'ip',
                        'SIP_TRUNK_HOST': '192.168.1.50', 'SIP_TRUNK_CALLER_ID': '+13034260100',
                        'SIP_TRUNK_DIDS': '+13034260100', 'AMI_PASSWORD': 'synthetic-ami-pass'}.items():
        monkeypatch.setenv(name, value)
    with TestClient(app, base_url=ORIGIN, headers={'Origin': ORIGIN}) as test_client:
        yield test_client


def test_the_line_page_and_the_command_line_import_the_local_calling_area(line_client, tmp_path):
    from typer.testing import CliRunner
    from app.cli.main import app as cli_app
    from api.tests.test_access_management_http import B, BOOTSTRAP
    first = line_client.get('/routing/analog-lines/sip', headers=B)
    assert first.status_code == 200, first.text
    assert first.json()['analog'] is True and first.json()['local_prefixes'] == 0
    assert first.json()['calls_at_once'] == 1
    refused = line_client.put('/routing/analog-lines/sip/local-calls', headers=B, json={'text': LCG_PAGE})
    assert refused.status_code == 400 and 'several calling plans' in refused.json()['detail']
    saved = line_client.put('/routing/analog-lines/sip/local-calls', headers=B,
                            json={'text': LCG_PAGE, 'plan': 'Basic', 'toll_per_minute': '0.10', 'line': '303-426'})
    assert saved.status_code == 200, saved.text
    assert saved.json()['saved'].startswith('Saved. 3 local prefixes go out on this line at no extra cost')

    def run(*args):
        return CliRunner().invoke(cli_app, ['--url', 'https://testserver', '--key', BOOTSTRAP, *args],
                                  obj={'client_factory': lambda address, timeout: (line_client, False)},
                                  env={'COLUMNS': '220', 'TZ': 'UTC'})
    listed = tmp_path / 'local.txt'
    listed.write_text('303-426\n303-298\n')
    done = run('providers', 'trunk', 'analog-line', 'import', str(listed))
    assert done.exit_code == 0, (done.stdout, done.stderr)
    assert '2 local prefixes go out on this line at no extra cost' in ' '.join(done.stdout.split())
    shown = run('providers', 'trunk', 'analog-line', 'show')
    assert shown.exit_code == 0 and 'Grandstream HT813 (analog line)' in shown.stdout
    preset = run('providers', 'trunk', 'presets', 'grandstream-ht813')
    assert preset.exit_code == 0, (preset.stdout, preset.stderr)
    assert 'Analog line gateway' in preset.stdout and 'Unconditional Call Forward' in preset.stdout
