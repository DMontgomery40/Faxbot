"""`faxbot savings opportunities …`, `faxbot recipients toll-free …` and caller-name lookup in the command line.

The command line talks to a real Faxbot over HTTP (test_cli's server), with a
Phaxio install and no history: every section says it has nothing to advise yet.
"""
from api.tests.test_cli import cli, server  # noqa: F401  (fixtures)


SECTIONS = {'sending', 'receiving', 'plans', 'fax_marker', 'billing_steps', 'partners', 'toll_free', 'carriers',
            'pages', 'discovery', 'relays', 'trunks', 'numbers', 'sites'}


def test_recommendations_alone_shows_every_section_and_each_has_its_own_command(cli):  # noqa: F811
    result = cli.json('costs', 'recommendations')
    assert set(result) == SECTIONS
    assert result['fax_marker']['state'] == 'no_calls' and result['billing_steps']['state'] == 'no_trunk'
    assert result['partners']['state'] == 'none' and result['toll_free']['state'] == 'none'
    human = ' '.join(cli('costs', 'recommendations').stdout.split())
    for heading in ('Sending', 'Receiving', 'Plans', 'Fax marker', 'Billing steps', 'Partner candidates',
                    'Recipients that run Faxbot', 'Partner relays', 'Toll-free numbers', 'Other carriers',
                    'Shaded areas', 'Your trunks', 'Where each number should live',
                    'Which site calls cost less from'):
        assert heading in human
    assert ('Faxbot placed no calls over your carrier line in the last 90 days, so there is nothing to compare yet.'
            in human)
    for command, key in (('fax-marker', 'fax_marker'), ('billing-steps', 'billing_steps'), ('partners', 'partners'),
                         ('toll-free', 'toll_free'), ('sending', 'sending'), ('plans', 'plans'),
                         ('carriers', 'carriers'), ('trunks', 'trunks'), ('numbers', 'numbers'), ('sites', 'sites')):
        alone = cli('costs', 'recommendations', command)
        assert alone.exit_code == 0, alone.stdout
        expected = result[key].get('sentence') or result[key].get('empty_sentence')
        assert expected in ' '.join(alone.stdout.split())
        assert cli.json('costs', 'recommendations', command) == result[key]
    # These sections also have their own partner commands.
    for command, key, empty in (
            (('recipients', 'partners', 'discover', 'list'), 'discovery',
             'No recipient that runs Faxbot has been found yet.'),
            (('recipients', 'partners', 'relay', 'suggestions'), 'relays',
             'No partner would have sent your recent faxes for less.')):
        alone = cli(*command)
        assert alone.exit_code == 0, alone.stdout
        assert empty in human and empty in ' '.join(alone.stdout.split())
        assert cli.json(*command) == result[key]
    receiving = cli('costs', 'recommendations', 'receiving')
    assert receiving.exit_code == 0, receiving.stdout
    for part in ('pool', 'quiet_numbers', 'connections'):
        expected = result['receiving'][part]['sentence']
        assert expected in human and expected in ' '.join(receiving.stdout.split())
    shading = cli('costs', 'recommendations', 'shading')
    assert shading.exit_code == 0, shading.stdout
    assert ('Nothing to suggest: shaded areas are kept with a fax-friendly pattern where it saves time.'
            in ' '.join(shading.stdout.split()))
    assert cli.json('costs', 'recommendations', 'shading') == result['pages']
    services = ' '.join(cli('costs', 'recommendations', 'service-numbers').stdout.split())
    assert 'Faxbot knows no fax service number besides your carrier line.' in services


def test_a_toll_free_number_is_noted_approved_and_withdrawn_with_its_history(cli):  # noqa: F811
    noted = cli.json('recipients', 'toll-free', 'note', '+12025550123', '1-800-555-0100')
    assert noted['current']['action'] == 'noted' and noted['approved_alternate'] is None
    missing = cli('recipients', 'toll-free', 'approve', '+12025550123', '+18005550100', '--by', 'Dana', '--on',
                  '2026-10-03', '--evidence', ' ')
    assert missing.exit_code != 0 and 'Say where the agreement is recorded' in ' '.join(missing.stderr.split())
    approved = cli.json('recipients', 'toll-free', 'approve', '+12025550123', '+18005550100', '--by', 'Dana',
                        '--on', '2026-10-03', '--evidence', 'Email from Dana')
    assert approved['approved_alternate'] == '+18005550100'
    shown = ' '.join(cli('recipients', 'toll-free', 'show', '+12025550123').stdout.split())
    assert ('Dana agreed on 3 October 2026. With this approval, Faxbot sends faxes for the recipient to '
            '+1 800-555-0100, and the recipient pays for those calls.') in shown
    assert 'Approved' in shown and 'On file' in shown and '3 October 2026' in shown
    withdrawn = cli.json('recipients', 'toll-free', 'withdraw', '+12025550123')
    assert withdrawn['approved_alternate'] is None and len(withdrawn['history']) == 3
    advice = ' '.join(cli('costs', 'recommendations', 'toll-free').stdout.split())
    assert 'No recipient has a toll-free fax number on file.' in advice


def test_caller_name_lookup_needs_the_telnyx_trunk(cli):  # noqa: F811
    human = ' '.join(cli('providers', 'trunk', 'telnyx', 'names').stdout.split())
    assert 'This needs the Telnyx trunk with a Telnyx API key' in human
    refused = cli('providers', 'trunk', 'telnyx', 'name-lookup-off', '+15555550100')
    assert refused.exit_code != 0
    assert 'Add a Telnyx API key to the Telnyx trunk before changing Telnyx settings.' in ' '.join(refused.stderr.split())
