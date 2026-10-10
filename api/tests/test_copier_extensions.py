"""Copiers as extensions of Faxbot (N22, migration 0085): the groundwork that is built so far.

Built and tested here: the extension's credentials (shown once, only pjsip's MD5 digest kept), the pjsip.conf
sections Faxbot writes for each copier (registration only from private addresses), the dialplan context that
receives a copier's fax and hands it to Faxbot with the number it dialled, and the hand-over script's two new
fields. Not built yet: turning that hand-over into an outbound fax from the copier's mailbox, forwarding received
faxes to the copier's extension, the console and command line, and the loopback proof with a second Asterisk
registering as the copier. Every name and number is synthetic; nothing has run against a real copier.
"""
import hashlib
import json
from pathlib import Path

import pytest
import sqlalchemy as sa

from api.app import copier_extensions, schema, schema_copier_extensions, sip_trunk
from api.tests.test_digital_schema import _downgrade, _tables
from api.tests.test_inbound_handover import _Faxbot, needs_shell, notify
from api.tests.test_schema import database  # noqa: F401 - fixture

ROOT = Path(__file__).resolve().parents[2]


def test_a_copier_gets_a_password_once_and_only_its_digest_is_kept(database):  # noqa: F811
    schema.upgrade_schema(database)
    copier, password = copier_extensions.add(database, name='Front desk Ricoh', extension='601',
                                             numbers=['+13035550160'], actor={'name': 'Ada'})
    assert copier.username == 'copier-601' and copier.endpoint == 'copier-601' and len(password) >= 20
    assert copier.md5_cred == hashlib.md5(f'copier-601:faxbot:{password}'.encode()).hexdigest()
    with database.connect() as connection:
        stored = json.dumps([dict(row) for row in connection.execute(
            sa.text('SELECT * FROM copier_extensions')).mappings()], default=str)
    assert password not in stored
    with pytest.raises(copier_extensions.CopierError, match='already a copier'):
        copier_extensions.add(database, name='Second', extension='601')
    with pytest.raises(copier_extensions.CopierError, match='3 to 6 digits'):
        copier_extensions.add(database, name='Bad', extension='06')
    copier_extensions.remove(database, copier.id)
    assert copier_extensions.copiers(database) == []


def test_pjsip_takes_a_copier_only_from_private_addresses_and_never_holds_its_password():
    copier = copier_extensions.Copier('c' * 32, 'Front desk Ricoh', '601', 'copier-601', 'a' * 32, None, (),
                                      'copier', 'active')
    text = copier_extensions.render([copier])
    assert text.count('[copier-601]') == 2 and text.count('[copier-601-auth]') == 1  # one AOR, one endpoint
    assert 'auth_type=md5' in text and 'md5_cred=' + 'a' * 32 in text and 'password=' not in text
    assert 'context=faxbot-copier' in text and f"set_var=FAXBOT_COPIER={'c' * 32}" in text
    assert 'deny=0.0.0.0/0.0.0.0' in text and 'permit=192.168.0.0/255.255.0.0' in text
    assert 'contact_deny=0.0.0.0/0.0.0.0' in text and 'contact_permit=10.0.0.0/255.0.0.0' in text
    assert 'max_contacts=1' in text and 't38_udptl=yes' in text
    assert copier_extensions.render([]) == ''
    # The file Faxbot writes for Asterisk carries the copier after the trunk and any partners.
    from api.app.config_values import ConfigurationValues
    values = ConfigurationValues.from_environment({'FAX_BACKEND': 'sip', 'SIP_TRUNK_PRESET': 'telnyx',
                                                   'SIP_TRUNK_USERNAME': 'faxbotuser',
                                                   'SIP_TRUNK_PASSWORD': 'Synthetic-Password-1'})
    rendered = sip_trunk.rendered_configuration(values, peers=[], copiers=[copier])
    assert rendered.index('[copier-601-auth]') > rendered.index('type=transport')
    assert rendered.count('[copier-601-auth]') == 1


def test_the_copier_dialplan_receives_and_hands_over_the_dialled_number_and_never_dials_out():
    text = (ROOT / 'asterisk' / 'etc' / 'asterisk' / 'extensions-copier.conf').read_text()
    main = (ROOT / 'asterisk' / 'etc' / 'asterisk' / 'extensions.conf').read_text()
    assert main.count('#include extensions-copier.conf') == 1
    context = text.split('[faxbot-copier]', 1)[1].split('\n[', 1)[0]
    assert 'ReceiveFAX(${FAXBOT_FILE},f)' in context and 'Dial(' not in context
    assert 'Set(FAXBOT_DIALED=${FILTER(0123456789+,${EXTEN})})' in context
    assert 'PJSIPHangup(403)' in context  # no copier ID: refused unanswered
    done = text.split('[faxbot-copier-done]', 1)[1].split('\n[', 1)[0]
    assert 'copier=${FILTER(0123456789abcdef,${FAXBOT_COPIER})}' in done
    assert 'dialed=${FILTER(0123456789+,${FAXBOT_DIALED})}' in done
    assert 'Dial(' not in text  # store and forward: a copier call is never bridged to the trunk


@needs_shell
def test_the_hand_over_script_passes_the_copier_and_the_dialled_number_and_nothing_else(tmp_path):
    faxbot = _Faxbot(200)
    try:
        sent = notify(tmp_path / 'one', faxbot.url, extra=['copier=' + 'c' * 32, 'dialed=+13035550199'])
        junk = notify(tmp_path / 'two', faxbot.url, extra=['copier=zz"},{"x', 'dialed=12;rm'])
        none = notify(tmp_path / 'three', faxbot.url)
    finally:
        faxbot.close()
    assert [result.stdout for result in (sent, junk, none)] == ['ok\n'] * 3
    bodies = [request['body'] for request in faxbot.requests]
    assert (bodies[0]['copier'], bodies[0]['dialed']) == ('c' * 32, '+13035550199')
    assert (bodies[1]['copier'], bodies[1]['dialed']) == (None, '12')
    assert (bodies[2]['copier'], bodies[2]['dialed']) == (None, None)


def test_0085_is_the_head_and_its_downgrade_keeps_copiers(database):  # noqa: F811
    schema.upgrade_schema(database)
    assert schema_copier_extensions.REVISION == '0085_copier_extensions' == schema.HEAD
    assert schema.PARTNER_ADMISSION == '0084_partner_admission'
    assert schema_copier_extensions.TABLES <= schema.STRICT_TABLES and schema_copier_extensions.TABLES <= _tables(database)
    _downgrade(database, '0084_partner_admission')
    assert not schema_copier_extensions.TABLES & _tables(database)
    schema.upgrade_schema(database)
    copier_extensions.add(database, name='Ricoh', extension='602')
    with pytest.raises(schema.SchemaUpgradeError, match='cannot be undone'):
        _downgrade(database, '0084_partner_admission')
