"""faxbot recipients partners notice-fax | notices | notice-faxes | pair | transfers | repairs."""
import json

from api.tests.test_cli import cli, server  # noqa: F401 - fixtures


def test_notice_fax_switch_and_the_lists(cli, tmp_path):  # noqa: F811
    from app.direct.crypto import Identity, card
    partner = tmp_path / 'valley.json'
    partner.write_text(json.dumps(card(Identity.generate(), organization='Valley Hospital',
                                       fax_number='+15550007777', endpoint='https://valley.example')))
    assert cli.json('recipients', 'partners', 'add', partner)['state'] == 'pending'
    on = cli('recipients', 'partners', 'notice-fax', 'Valley Hospital', 'on')
    assert on.exit_code == 0, on.stderr
    assert on.stdout.strip() == 'Each document to Valley Hospital now goes directly, with a one-page notice by fax.'
    (listed,) = cli.json('recipients', 'partners', 'list')
    assert listed['notice_fax'] is True
    assert cli('recipients', 'partners', 'notice-fax', 'Valley Hospital', 'maybe').exit_code != 0
    assert 'No notice faxes yet.' in cli('recipients', 'partners', 'notices').stdout
    assert 'No documents sent in pieces yet.' in cli('recipients', 'partners', 'transfers').stdout
    assert 'No broken calls with partners.' in cli('recipients', 'partners', 'repairs').stdout
    asked = cli('recipients', 'partners', 'pair', 'n-1')
    assert asked.exit_code != 0 and 'Give the received fax that is the notice' in asked.stderr
    missing = cli('recipients', 'partners', 'notice-faxes', 'n-1')
    assert missing.exit_code != 0 and 'There is no such document waiting for its notice.' in missing.stderr
