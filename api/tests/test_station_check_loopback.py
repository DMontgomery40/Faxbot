"""Loopback proofs for asterisk patch 0007: the station check and the T0 cap, on two Faxbot Asterisks.

Native only (FAXBOT_NATIVE_PROOF=1, Docker and the Faxbot Asterisk image; ``make native-proof`` style). The
receiver answers as +1 720 555 0199 (its endpoint sets the fax station ID), while the sent call expects the number
dialled, so the sender's phase B handler sees a station that is none of the expected ones. Nothing here reaches a
carrier: both trunks are the two containers on an internal network.
"""
import json
import os

import pytest

from api.tests.test_t38_loopback import exchange
from app import sip_calls, sip_trunk

pytestmark = [
    pytest.mark.native,
    pytest.mark.skipif(os.environ.get('FAXBOT_NATIVE_PROOF') != '1',
                       reason='Set FAXBOT_NATIVE_PROOF=1 to run the station check loopback proof.'),
]
OTHER_STATION = '+17205550199'


def answers_as_another_station(text):
    """The receiver's endpoint gives its calls another fax station ID (pjsip set_var writes FAXOPT)."""
    marker = f'context={sip_trunk.INBOUND_CONTEXT}'
    assert marker in text
    return text.replace(marker, f'{marker}\nset_var=FAXOPT(localstationid)={OTHER_STATION}')


def answers_and_never_faxes(text):
    marker = f'context={sip_trunk.INBOUND_CONTEXT}'
    assert marker in text
    return text.replace(marker, 'context=faxbot-silent')


SILENT = ('dialplan add extension _.,1,Answer() into faxbot-silent',
          'dialplan add extension _.,2,Wait(75) into faxbot-silent',
          'dialplan add extension _.,3,Hangup() into faxbot-silent')


def test_a_refused_station_ends_the_call_before_any_page_and_reads_as_a_wrong_station(tmp_path):
    outcome = exchange(tmp_path, receiver_edit=answers_as_another_station, wait_for_receiver=20,
                       extra_variables={'FAXBOT_CSI_EXPECT': '15555550123', 'FAXBOT_CSI_REFUSE': 'yes'})
    result = outcome['result']
    assert result['Status'] == 'FAILED' and result['Pages'] == '0', result
    assert result['CsiCheck'] == 'refused', result
    assert 'answered as a station Faxbot did not expect; ending the call before any page' in outcome['sender_log']
    assert outcome['captured'] is None  # the receiver stored no fax
    assert sip_calls.verdict(result) == sip_calls.WRONG_STATION
    print(json.dumps({'station_refused': {'result': result, 'seconds': outcome['submit_to_result_seconds']}},
                     indent=2))


def test_a_station_that_differs_in_warn_mode_lets_the_fax_go_and_says_so(tmp_path):
    outcome = exchange(tmp_path, receiver_edit=answers_as_another_station,
                       extra_variables={'FAXBOT_CSI_EXPECT': '15555550123'})
    result = outcome['result']
    assert result['Status'] == 'SUCCESS' and result['Pages'] == '2', result
    assert result['CsiCheck'] == 'differs', result
    assert outcome['captured'] is not None


def test_a_matching_station_is_quiet(tmp_path):
    outcome = exchange(tmp_path, receiver_edit=answers_as_another_station,
                       extra_variables={'FAXBOT_CSI_EXPECT': '15555550123.17205550199', 'FAXBOT_CSI_REFUSE': 'yes'})
    assert outcome['result']['Status'] == 'SUCCESS' and outcome['result']['CsiCheck'] == 'matches', outcome['result']


def test_no_fax_answer_ends_the_call_at_the_cap_not_at_spandsps_sixty_seconds(tmp_path):
    outcome = exchange(tmp_path, receiver_edit=answers_and_never_faxes, receiver_commands=SILENT,
                       wait_for_receiver=5, extra_variables={'FAXBOT_T0_MS': '50000'})
    result = outcome['result']
    assert result['Status'] == 'FAILED' and result['T0Capped'] == '1', result
    held = int(result['Ended']) - int(result['Answered'])
    # Ended by the cap (50 s after SendFAX started), well before spandsp's own 60 s T0.
    assert 49 <= held <= 56, held
    assert 'no fax answered on' in outcome['sender_log'] and 'within 50000 ms; ending the call' in outcome['sender_log']
    assert sip_calls.verdict(result) in ('no_fax_answer', 'no_fax_data_back', 'no_t38_data_back'), result
    print(json.dumps({'t0_cap': {'answered_to_end_seconds': held, 'result': result}}, indent=2))
