"""Loopback proof for digits after answer (N7) on two Faxbot Asterisks.

Native only (FAXBOT_NATIVE_PROOF=1, Docker and the Faxbot Asterisk image). The receiver stands in for a phone
menu: it answers, waits up to ten seconds for one key, and only on 2 goes on to Faxbot's normal receive path; any
other key, or none, hangs up. The sender's call carries FAXBOT_DTMF=2 as ami.originate_fields_for sets it for a
recipient with keys after answer, so [faxbot-send] presses 2 (RFC 4733) before SendFAX. Nothing here reaches a
carrier: both trunks are the two containers on an internal network. Not yet run against a real phone menu.
"""
import json
import os

import pytest

from api.tests.test_t38_loopback import exchange
from app import sip_trunk

pytestmark = [
    pytest.mark.native,
    pytest.mark.skipif(os.environ.get('FAXBOT_NATIVE_PROOF') != '1',
                       reason='Set FAXBOT_NATIVE_PROOF=1 to run the digits-after-answer loopback proof.'),
]

# Asterisk's command line splits a command at spaces and drops double quotes, so these lines have neither.
MENU = ('dialplan add extension _.,1,Answer() into faxbot-menu',
        'dialplan add extension _.,2,Read(FAXBOT_KEYS,,1,,1,10) into faxbot-menu',
        'dialplan add extension _.,3,Log(NOTICE,Faxbot-proof-menu-heard-keys-[${FAXBOT_KEYS}]) into faxbot-menu',
        'dialplan add extension _.,4,GotoIf($[x${FAXBOT_KEYS}=x2]?faxbot-inbound,${EXTEN},1) into faxbot-menu',
        'dialplan add extension _.,5,Hangup() into faxbot-menu')


def behind_a_phone_menu(text):
    marker = f'context={sip_trunk.INBOUND_CONTEXT}'
    assert marker in text
    return text.replace(marker, 'context=faxbot-menu')


def test_the_keys_reach_the_menu_on_the_voice_call_and_the_fax_machine_behind_it_receives(tmp_path):
    outcome = exchange(tmp_path, receiver_edit=behind_a_phone_menu, receiver_commands=MENU,
                       extra_variables={'FAXBOT_DTMF': '2'})
    result = outcome['result']
    assert 'Faxbot-proof-menu-heard-keys-[2]' in outcome['receiver_log'], (result, outcome['receiver_log'][-3000:])
    assert result['Status'] == 'SUCCESS' and result['Pages'] == '2', result
    assert outcome['captured'] is not None
    print(json.dumps({'after_answer': {'result': result, 'seconds': outcome['submit_to_result_seconds'],
                                       'image': outcome['image'], 'asterisk': outcome['asterisk']}}, indent=2))
