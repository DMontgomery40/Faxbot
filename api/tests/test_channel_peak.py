"""Channels at measured peak (N24) and the fax server renewal page (N20), from synthetic call records.

Each file follows its vendor's documented layout as read on 2026-10-10 (Asterisk's cdr_csv.c field order, Cisco's
two header lines and UTC epoch seconds, RightFax's DocTransport audit log levels 3 and 4, GFI FaxMaker's activity
export columns); the calls, numbers and amounts are invented. Not yet run against a real fax server's records.
"""
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

import pytest
import sqlalchemy as sa

from api.app.config_values import ConfigurationValues
from api.app.routing import channel_peak, renewal
from api.app.schema import upgrade_schema
from api.tests.test_schema import database  # noqa: F401 (fixture)


def asterisk_line(start, end, dst='+13035550110'):
    return (f'"","+13035550120","{dst}","fax-in","""Caller"" <+13035550120>","SIP/trunk-0001","","ReceiveFAX",'
            f'"/var/spool/fax/1.tif","{start}","{start}","{end}",120,118,"ANSWERED","DOCUMENTATION"')


ASTERISK = '\n'.join([
    asterisk_line('2026-09-01 09:00:00', '2026-09-01 09:02:00'),
    asterisk_line('2026-09-01 09:01:00', '2026-09-01 09:03:00'),
    asterisk_line('2026-09-01 09:02:00', '2026-09-01 09:04:00'),   # starts as the first ends: no overlap
    asterisk_line('2026-09-01 10:30:00', '2026-09-01 11:10:00', dst='+13035550111'),
    '"","short"',
    '',
])


def epoch(moment):
    return int(moment.replace(tzinfo=timezone.utc).timestamp())


CUCM = '\n'.join([
    '"cdrRecordType","globalCallID_callManagerId","dateTimeOrigination","callingPartyNumber",'
    '"originalCalledPartyNumber","finalCalledPartyNumber","dateTimeConnect","dateTimeDisconnect","duration"',
    'INTEGER,INTEGER,INTEGER,VARCHAR(50),VARCHAR(50),VARCHAR(50),INTEGER,INTEGER,INTEGER',
    f'1,1,{epoch(datetime(2026, 9, 1, 15))},"3035550120","3035550110","3035550110",'
    f'{epoch(datetime(2026, 9, 1, 15, 0, 5))},{epoch(datetime(2026, 9, 1, 15, 2))},115',
    f'1,1,{epoch(datetime(2026, 9, 1, 15, 1))},"3035550121","4155550100","4155550100",0,'
    f'{epoch(datetime(2026, 9, 1, 15, 1, 30))},0',
    '',
])
RIGHTFAX_3 = '\n'.join([
    'S,09/01/2026,09:00,0,120,3035550199,REMOTE,Successful,2,,,,user1,,',
    'R,09/01/2026,09:01,1,60,3035550110,SENDER,Successful,1,,,,user2,,',
    'R,09/01/2026,09:01,3,60,3035550110,SENDER,Successful,1,,,,user2,,',
    'S,13/45/2026,09:01,0,60,3035550199,REMOTE,Successful,1,,,,user1,,',
    '',
])
RIGHTFAX_4 = 'R\t20260901\t0901\t2\t90\t3035550110\tSENDER\t1\t1\n'
FAXMAKER = '\n'.join([
    'Type,Direction,Date,Day,Month,Year,Time,Hour,Minute,Host Name,Method Submitted,Billing Code,Sender Number,'
    'Sender,Recipient Number,Recipient Name,Recipient Company,Line,Fax Pages excluding coverpage,'
    'Fax Pages including coverpage,Total Pages/SMS,Resolution,Speed,Retries,Call duration (seconds),Status',
    'Fax,Outbound,9/1/2026,1,9,2026,09:00:00,9,0,FAX1,Email,,,Ada,+13035550199,,,Line 1,1,2,2,Fine,14400,0,90,'
    'Sent',
    'Fax,Inbound,9/1/2026,1,9,2026,09:01:00,9,1,FAX1,,,+13035550120,,,,,Line 2,1,1,1,Fine,14400,0,30,Received',
    '',
])
GENERIC = 'start,duration,direction,channel,number\n2026-09-01 09:00,600,received,A,+13035550110\n' \
          '2026-09-01T09:05:00,60,sent,B,+13035550199\nnot a time,60,sent,B,+1\n'


# -- formats ---------------------------------------------------------------------------------------------------------

def test_each_documented_format_is_recognised_and_read():
    assert channel_peak.detect(ASTERISK) == 'asterisk'
    assert channel_peak.detect(CUCM) == 'cucm'
    assert channel_peak.detect(RIGHTFAX_3) == 'rightfax' and channel_peak.detect(RIGHTFAX_4) == 'rightfax'
    assert channel_peak.detect(FAXMAKER) == 'faxmaker'
    assert channel_peak.detect(GENERIC) == 'csv'
    asterisk = channel_peak.parse_calls(ASTERISK.encode(), time_zone='UTC')
    assert len(asterisk.calls) == 4 and asterisk.skipped_count == 1
    assert asterisk.calls[0].started_at == datetime(2026, 9, 1, 9) and asterisk.calls[0].number == '+13035550110'
    # Local times are read in the zone you name and kept as UTC.
    denver = channel_peak.parse_calls(ASTERISK.encode(), time_zone='America/Denver')
    assert denver.calls[0].started_at == datetime(2026, 9, 1, 15)
    cucm = channel_peak.parse_calls(CUCM.encode())
    assert [call.started_at for call in cucm.calls] == [datetime(2026, 9, 1, 15), datetime(2026, 9, 1, 15, 1)]
    assert cucm.skipped_count == 0  # the line of field types is not a call
    # A phone system's records carry voice calls too: keep only the fax numbers.
    only_fax = channel_peak.parse_calls(CUCM.encode(), numbers=['+1 303-555-0110'])
    assert [call.number for call in only_fax.calls] == ['3035550110']
    rightfax = channel_peak.parse_calls(RIGHTFAX_3.encode(), time_zone='UTC')
    assert [(call.direction, call.channel) for call in rightfax.calls] == [('sent', '0'), ('received', '1'),
                                                                           ('received', '3')]
    assert rightfax.skipped == ['Line 4: its date, time or duration is not readable.']
    level4 = channel_peak.parse_calls(RIGHTFAX_4.encode(), time_zone='UTC')
    assert level4.calls[0].started_at == datetime(2026, 9, 1, 9, 1) and level4.calls[0].ended_at == datetime(
        2026, 9, 1, 9, 2, 30)
    faxmaker = channel_peak.parse_calls(FAXMAKER.encode(), time_zone='UTC')
    assert [(call.direction, call.channel) for call in faxmaker.calls] == [('sent', 'Line 1'), ('received', 'Line 2')]
    generic = channel_peak.parse_calls(GENERIC.encode(), time_zone='UTC')
    assert len(generic.calls) == 2 and generic.skipped_count == 1
    with pytest.raises(channel_peak.CallFileError, match='Choose its format'):
        channel_peak.parse_calls(b'hello,world\n')
    with pytest.raises(channel_peak.CallFileError, match='no call'):
        channel_peak.parse_calls(b'S,13/45/2026,09:01,0,60\n', source_format='rightfax')


# -- the measure ---------------------------------------------------------------------------------------------------

def test_hourly_peaks_carry_long_calls_and_back_to_back_calls_do_not_overlap():
    calls = channel_peak.parse_calls(ASTERISK.encode(), time_zone='UTC').calls
    peaks = channel_peak.hourly_peaks([(call.started_at, call.ended_at) for call in calls])
    assert peaks == {datetime(2026, 9, 1, 9): 2, datetime(2026, 9, 1, 10): 1, datetime(2026, 9, 1, 11): 1}
    assert channel_peak.percentile([0] * 99 + [5]) == 0 and channel_peak.percentile([0] * 98 + [5, 5]) == 5
    assert channel_peak.percentile([]) is None


def test_the_report_counts_channels_never_used_from_named_channels_or_from_the_peak():
    named = channel_peak.report(channel_peak.parse_calls(RIGHTFAX_3.encode(), time_zone='UTC').calls, licensed=8,
                                time_zone='UTC')
    assert (named['peak'], named['never_used'], named['never_used_basis']) == (3, 5, 'channels')
    assert named['sentence'].endswith('so 5 of your 8 licensed channels never carried a call.')
    by_peak = channel_peak.report(channel_peak.parse_calls(ASTERISK.encode(), time_zone='UTC').calls, licensed=4,
                                  time_zone='UTC')
    assert (by_peak['peak'], by_peak['sufficed'], by_peak['never_used']) == (2, 2, 2)
    assert by_peak['sentence'] == ('4 calls over 1 day: at most 2 at once, and in 99 hours out of 100 no more than 2. '
                                   '2 channels would have carried every call, so 2 of your 4 licensed channels were '
                                   'never needed at the same time as the others.')
    nine = next(item for item in by_peak['profile'] if item['hour'] == 9)
    assert (nine['peak'], nine['p99']) == (2, 2)


def _values(**extra):
    return ConfigurationValues.from_environment({'FAX_BACKEND': 'sip', 'SIP_TRUNK_PRESET': 'telnyx',
                                                 'SIP_TRUNK_AUTH': 'ip', 'SIP_TRUNK_HOST': 'sip.telnyx.com',
                                                 'SIP_TRUNK_DIDS': '+13035550100', 'FAX_DEFAULT_COUNTRY': 'US',
                                                 'INBOUND_ENABLED': 'true', **extra})


def _sip_call(engine, start, minutes, direction='inbound'):
    table = sa.Table('sip_call_records', sa.MetaData(), autoload_with=engine)
    with engine.begin() as connection:
        connection.execute(table.insert().values(
            id=uuid4().hex, direction=direction, call_id=uuid4().hex, started_at=start, answered_at=start,
            ended_at=start + timedelta(minutes=minutes), disposition='answered', t38='yes', fax_preference=0,
            did='+13035550100', created_at=start, updated_at=start))


def test_imports_are_kept_overlapping_files_count_a_call_once_and_faxbot_measures_its_own(database):  # noqa: F811
    upgrade_schema(database)
    now = datetime(2026, 9, 2, 12)
    _sip_call(database, now - timedelta(hours=3), 5)
    _sip_call(database, now - timedelta(hours=3, minutes=-2), 5, direction='outbound')
    parsed = channel_peak.parse_calls(RIGHTFAX_3.encode(), time_zone='UTC')
    first = channel_peak.import_calls(database, parsed, system='RightFax at HQ', data=RIGHTFAX_3, licensed=8,
                                      file_name='audit.log', actor={'name': 'Ada'}, time_zone='UTC')
    channel_peak.import_calls(database, parsed, system='RightFax at HQ', data=RIGHTFAX_3, file_name='again.log')
    shown = channel_peak.view(database, _values(TIME_ZONE='UTC'), now=now)
    own = next(system for system in shown['systems'] if system['own'])
    assert own['report']['peak'] == 2 and own['report']['calls'] == 2
    # Faxbot's own limit is the calls at once its trunks allow, not a licence.
    assert 'Your trunks allow' in own['report']['sentence'] and 'licensed' not in own['report']['sentence']
    rightfax = next(system for system in shown['systems'] if system['system'] == 'RightFax at HQ')
    assert rightfax['report']['calls'] == 3 and rightfax['report']['never_used'] == 5
    assert len(rightfax['imports']) == 2 and rightfax['source'] == 'RightFax DocTransport audit log'
    channel_peak.remove_import(database, first)
    with pytest.raises(channel_peak.CallFileError):
        channel_peak.remove_import(database, first)
    rightfax = next(system for system in channel_peak.view(database, _values(), now=now)['systems']
                    if system['system'] == 'RightFax at HQ')
    assert len(rightfax['imports']) == 1 and rightfax['report'].get('licensed') is None
    with database.connect() as connection:
        assert connection.exec_driver_sql('SELECT count(*) FROM channel_calls').scalar() == 6
    with pytest.raises(channel_peak.CallFileError, match='Name the system'):
        channel_peak.import_calls(database, parsed, system='  ', data=RIGHTFAX_3)


# -- the renewal page ------------------------------------------------------------------------------------------------

def _received(engine, number, when, status='received'):
    table = sa.Table('inbound_faxes', sa.MetaData(), autoload_with=engine)
    with engine.begin() as connection:
        connection.execute(table.insert().values(
            id=uuid4().hex, from_number='+18015550100', to_number=number, status=status, backend='sip',
            inbound_backend='sip', pages=1, created_at=when, received_at=when, updated_at=when))


ROUTES = ('Number,User,Email,Cover sheet\n+13035550100,Ada,ada@example.com,Standard\n'
          '303-555-0110,Grace,grace@example.com,Legal\n303-555-0111,Grace,grace@example.com,\nnope,,,\n')


def test_the_renewal_page_reads_amount_channels_parallel_run_and_what_is_left(database):  # noqa: F811
    upgrade_schema(database)
    now = datetime(2027, 3, 10, 12)
    parsed = channel_peak.parse_calls(RIGHTFAX_3.encode(), time_zone='UTC')
    channel_peak.import_calls(database, parsed, system='RightFax at HQ', data=RIGHTFAX_3, time_zone='UTC')
    renewal.record_renewal(database, 'RightFax at HQ', renews_on=date(2027, 5, 31), amount='$26,756.71',
                           product='RightFax 22.2', licensed_channels=8, parallel_numbers=['303-555-0100'],
                           parallel_since=date(2027, 2, 1), actor={'name': 'Ada'}, now=now)
    _received(database, '+13035550100', datetime(2027, 2, 15))
    _received(database, '+13035550100', datetime(2027, 2, 16), status='failed')
    _received(database, '+13035550100', datetime(2027, 1, 15))  # before the run began
    routes, skipped = renewal.parse_routes(ROUTES.encode())
    assert len(routes) == 3 and skipped == ['Row 5: nope is not a fax number Faxbot can read.']
    renewal.import_routes(database, 'RightFax at HQ', routes, file_name='routing.csv')
    found = renewal.page(database, _values(), 'RightFax at HQ', now=now)
    assert found['renewal']['state'] == 'review' and found['renewal']['days_left'] == 82
    assert found['sentences'][0] == 'RightFax 22.2 renews on 31 May 2027 for $26,756.71: 82 days left to decide.'
    assert found['channels']['never_used'] == 5 and found['channels']['per_channel'] == '$3,344.59'
    assert found['sentences'][1].endswith('those 5 channels are $16,722.94 of it, if it is priced by channel.')
    assert (found['parallel']['received'], found['parallel']['received_ok']) == (2, 1)
    # Sent faxes are the whole installation's: the sentence says so.
    assert found['sentences'][2].endswith('(1 complete). Faxbot sent 0 faxes in all since 1 February 2027 '
                                          '(0 delivered).')
    renewal.record_renewal(database, 'RightFax at HQ', renews_on=date(2027, 5, 31), amount='26756.71',
                           licensed_channels=8, parallel_numbers=['303-555-0100', '303-555-0199'],
                           parallel_since=date(2027, 2, 1), now=now)
    two = renewal.page(database, _values(), 'RightFax at HQ', now=now)
    assert two['sentences'][2].endswith('1 of these numbers is on no Faxbot account yet, so faxes to it still reach '
                                        'only the old server.')
    assert [item['on_faxbot'] for item in two['parallel']['numbers']] == [True, False]
    assert renewal.view(database, _values(), now=now)['channels']['systems'][0]['report']['licensed'] == 8
    assert found['left']['count'] == 2 and found['left']['users'] == 1
    assert found['sentences'][3] == '2 of its 3 numbers are not on Faxbot yet, for 1 user.'
    assert {item['id'] for item in found['reference']} == {'cuyahoga-rightfax', 'rightfax-support', 'sr140-cdw'}
    early = renewal.page(database, _values(), 'RightFax at HQ', now=datetime(2026, 10, 10))
    assert early['renewal']['state'] == 'early' and 'This page is for 2 March 2027' in early['sentences'][0]
    renewal.remove_renewal(database, 'RightFax at HQ')
    assert renewal.renewals_by_system(database) == {}
    with pytest.raises(renewal.RenewalError):
        renewal.remove_renewal(database, 'RightFax at HQ')
    with pytest.raises(renewal.RenewalError, match='as a number'):
        renewal.record_renewal(database, 'X', renews_on=date(2027, 1, 1), amount='lots')
    with pytest.raises(renewal.RenewalError, match='not a fax number'):
        renewal.record_renewal(database, 'X', renews_on=date(2027, 1, 1), amount='1', parallel_numbers=['nope'])
    # Without its routing, the line inventory's fax lines stand in, and the page says so.
    renewal.import_routes(database, 'RightFax at HQ', [])
    page = renewal.page(database, _values(), 'RightFax at HQ', now=now)
    assert page['left'] is None and page['renewal'] is None
    assert page['sentences'][-1].startswith('Faxbot does not know its numbers yet')
