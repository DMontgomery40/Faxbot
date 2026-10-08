"""Did a failed call end before any fax data? Each provider's documented codes, as recorded fixtures (no network).

The fixtures follow each provider's API reference as read on 2026-10-07
(``routing/predata.py`` names the pages). True lets a fax whose route a rule
chose go to its next account; False and None (unknown) never do.
"""
import json

import httpx
import pytest

from api.app.routing import predata


@pytest.mark.parametrize('error_type, expected', [
    ('lineError', True), ('documentConversionError', True), ('faxError', None), ('fatalError', None),
    ('generalError', None), (None, None)])
def test_phaxio_line_errors_never_reached_a_fax_machine(error_type, expected):
    assert predata.phaxio(error_type) is expected


@pytest.mark.parametrize('fax, expected', [
    ({'status': 'FAILURE', 'errorType': 'CALL_ERROR', 'errorCode': 17, 'pagesSentSuccessfully': 0}, True),
    ({'status': 'FAILURE', 'errorType': 'CALL_ERROR', 'errorCode': 30, 'pagesSentSuccessfully': 0}, True),
    ({'status': 'FAILURE', 'errorType': 'CALL_ERROR', 'errorCode': 11, 'pagesSentSuccessfully': 0}, None),
    ({'status': 'FAILURE', 'errorType': 'DOCUMENT_CONVERSION_ERROR', 'errorCode': 4}, True),
    ({'status': 'FAILURE', 'errorType': 'FAX_ERROR', 'errorCode': 13, 'pagesSentSuccessfully': 0}, None),
    ({'status': 'FAILURE', 'errorType': 'CALL_ERROR', 'errorCode': 17, 'pagesSentSuccessfully': 2}, False)])
def test_sinch_call_errors_with_no_page_sent(fax, expected):
    assert predata.sinch(fax) is expected


@pytest.mark.parametrize('status, message, expected', [
    ('busy', None, True), ('no-answer', None, True), ('failed', 'Connection Failed', True),
    ('failed', 'Fax transmission not established', True),
    ('failed', 'No response after sending a page', None), ('failed', 'Received no response to DCS or TCF', None),
    ('failed', None, None), ('delivered', None, None)])
def test_signalwire_statuses_and_messages(status, message, expected):
    assert predata.signalwire(status, message) is expected


def _humble(status, reason, pages):
    return {'status': status, 'recipients': [{'toNumber': '13035550100', 'status': status, 'failureReason': reason,
                                              'attempts': [{'status': 'failure', 'failureReason': reason,
                                                            'numPagesSent': pages}]}]}


@pytest.mark.parametrize('fax, expected', [
    (_humble('failure', 'Receiver did not pick up', 0), True),
    (_humble('failure', 'No fax machine detected at destination', 0), True),
    (_humble('failure', 'Receiver did not pick up', 1), False),
    (_humble('failure', 'Something else went wrong', 0), None),
    (_humble('partial success', 'Receiver did not pick up', 0), False),
    ({'status': 'image failure'}, True),
    ({'status': 'failure', 'recipients': [{'failureReason': 'Receiver did not pick up',
                                          'attempts': [{'status': 'failure'}]}]}, None)])
def test_humblefax_attempts_with_no_page_sent(fax, expected):
    assert predata.humblefax(fax) is expected


@pytest.mark.parametrize('code, expected', [('6100', True), (6000, True), ('5100', True), ('5200', True),
                                             ('4000', None), ('5000', None), (None, None)])
def test_documo_result_codes(code, expected):
    assert predata.documo(code) is expected


def test_efax_has_no_published_codes_so_nothing_falls_back():
    assert predata.efax({'status': 'failed', 'code': 1}) is None


@pytest.mark.parametrize('event, expected', [
    ({'Status': 'FAILED', 'Pages': '0'}, True),
    ({'Status': 'FAILED', 'Pages': '0', 'Station64': 'UkVNT1RF'}, False),
    ({'Status': 'FAILED', 'Pages': '2'}, False),
    ({'Status': 'FAILED'}, None),
    ({'Status': 'SUCCESS', 'Pages': '2'}, None)])
def test_the_trunk_without_the_engine(event, expected):
    assert predata.native_event(event) is expected


def test_signed_callbacks_carry_the_classification():
    assert predata.callback('signalwire', [('FaxStatus', 'busy'), ('FaxSid', 'FX1')]) is True
    assert predata.callback('phaxio', [('fax', json.dumps({'id': 1, 'status': 'failure', 'error_type': 'lineError'}))]) \
        is True
    assert predata.callback('phaxio', [('fax[error_type]', 'faxError')]) is None


@pytest.mark.asyncio
async def test_the_adapters_report_it_with_a_failed_status():
    """Recorded provider answers through the real adapters: the classification rides with the failed status."""
    from api.app.documo_service import DocumoFaxService
    from api.app.humblefax_service import HumbleFaxFaxService
    sid = '0f0e0d0c-0b0a-4908-8706-050403020100'

    def documo(request):
        return httpx.Response(200, json={'messageId': sid, 'status': 'failed', 'resultCode': '6100',
                                         'resultInfo': 'Fax Number Busy'})
    service = DocumoFaxService('synthetic-key', 'https://api.documo.com', False,
                               transport=httpx.MockTransport(documo))
    found = await service.get_fax_status(sid)
    assert (found['status'], found['before_fax_data']) == ('failed', True)

    def humble(request):
        return httpx.Response(200, json={'data': {'sentFax': {'id': 12345, **_humble(
            'failure', 'Receiver did not pick up', 0)}}})
    service = HumbleFaxFaxService('synthetic-access', 'synthetic-secret', transport=httpx.MockTransport(humble))
    found = await service.get_fax_status('12345')
    assert (found['status'], found['before_fax_data']) == ('failed', True)


# Recommendations draft rules ------------------------------------------------------------------------------------------

def test_recommendations_draft_a_rule_for_a_country_where_one_account_was_cheaper():
    from api.app.routing.recommendations import country_rules, public_items

    def item(number, saving):
        return {'number': number, 'kind': 'cheaper_route', 'suggested': {'route': 'sinch'},
                'saving_per_fax': {'currency': 'USD', 'amount': '0.03'}, '_delivered': 19, '_saving_micros': saving,
                'rule_suggestion': {'name': 'x', 'when': {}, 'then': {'use': 'sinch'}}}
    items = [item('+442071234567', 30_000), item('+441614960000', 32_000), item('+13035550100', 50_000)]
    found = country_rules(items)
    assert [(rule['country'], rule['route'], rule['numbers'], rule['delivered']) for rule in found] == [
        ('GB', 'sinch', 2, 38)]
    assert found[0]['sentence'] == ('Faxes to +44 numbers cost about $0.031 less each through Sinch over the last 30 '
                                    'days (38 delivered faxes to 2 numbers). Add as a rule?')
    assert found[0]['rule_suggestion'] == {'name': 'Numbers in the United Kingdom go by Sinch',
                                           'when': {'destination': {'countries': ['GB']}}, 'then': {'use': 'sinch'}}
    assert all(not key.startswith('_') for entry in public_items(items) for key in entry)
