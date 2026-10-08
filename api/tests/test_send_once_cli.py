"""faxbot recipients partners send-once list | offer | accept | end, and the bytes line of faxbot costs savings."""
import json

from api.tests.test_cli import cli, server  # noqa: F401 - fixtures


class Unreachable:
    """The partner cannot be reached: the offer waits to be told."""

    async def request(self, method, url, **kwargs):
        return 503, None


def test_offer_list_end_and_the_savings_line(cli, tmp_path):  # noqa: F811
    from app import main
    from app.direct.crypto import Identity, card
    main.app.state.direct_http = Unreachable()
    partner = tmp_path / 'valley.json'
    partner.write_text(json.dumps(card(Identity.generate(), organization='Valley Hospital',
                                       fax_number='+15550007777', endpoint='https://valley.example')))
    assert cli.json('recipients', 'partners', 'add', partner)['state'] == 'pending'
    assert 'No send-once agreements.' in cli('recipients', 'partners', 'send-once', 'list').stdout
    offered = cli('recipients', 'partners', 'send-once', 'offer', 'Valley Hospital', '+15550006667', '+15550006668',
                  '--intake', 'Central records')
    assert offered.exit_code == 0, offered.stderr
    assert offered.stdout.splitlines()[0] == 'Your intake "Central records" files Valley Hospital\'s faxes for 2 numbers.'
    assert 'Offered. Faxbot tells the partner as soon as it can reach them.' in offered.stdout
    listed = cli('recipients', 'partners', 'send-once', 'list')
    assert 'Your intake files theirs' in listed.stdout and '+15550006667, +15550006668' in listed.stdout
    # No receiving rule files these numbers yet, and the list says so for each.
    assert 'No receiving rule files faxes to +15550006667, so it waits in Received with no mailbox.' in listed.stdout
    assert 'No documents went to partners as references or changes in the last 30 days.' in listed.stdout
    again = cli('recipients', 'partners', 'send-once', 'offer', 'Valley Hospital', '+15550006669')
    assert again.exit_code != 0 and 'end that agreement first' in again.stderr
    nothing = cli('recipients', 'partners', 'send-once', 'accept', 'Valley Hospital')
    assert nothing.exit_code != 0 and 'There is no matching send-once agreement with Valley Hospital.' in nothing.stderr
    ended = cli('recipients', 'partners', 'send-once', 'end', 'Valley Hospital')
    assert ended.exit_code == 0 and ended.stdout.strip() == 'Ended. Faxes to these numbers go by your usual routes again.'
    (agreement,) = cli.json('recipients', 'partners', 'send-once', 'list')['agreements']
    assert agreement['state'] == 'withdrawn' and agreement['status'] == 'Ended by you.'
    savings = cli('costs', 'savings')
    assert savings.exit_code == 0, savings.stderr
    assert ('Bytes saved by reuse and patches: No documents went to partners as references or changes in the last '
            '30 days.') in savings.stdout
