"""Receiving rules placed on arrival (provider-rules design §4.9), on migrated SQLite and PostgreSQL.

The cases are the design's: order, no-match parity, any number, sender, site, time, email off and keep days,
plus the subaddress the sender stated (M17a), which routes and never grants access.
"""
from datetime import datetime, timedelta
import json

import pytest
import sqlalchemy as sa

from api.tests.test_schema import database  # noqa: F401 (fixture)
from api.tests.test_access_policy import NOW
from api.tests.test_inbound_access import InboundWorld
from api.app.access import receiving_rules
from api.app.access.fax_resources import FaxAccessError
from api.app.access.receiving_rules import ReceivedFacts
from api.app.routing.numbers import is_canonical, stored_number

DENVER = 'America/Denver'


class RulesWorld(InboundWorld):
    def __init__(self, engine):
        super().__init__(engine)
        self.receiving = receiving_rules.tables(engine)
        self.mailbox('cases', 'Cases', '+15550100003')
        self.count = 0

    def options(self, rule_id, place, **given):
        options = receiving_rules.clean_options(given)
        values = receiving_rules.stored_values(options)
        with self.engine.begin() as connection:
            connection.execute(self.receiving['options'].insert().values(
                id=rule_id, place=place, version=1, created_at=NOW, updated_at=NOW, **values))

    def rule(self, identity, number, mailbox, *, first=False, **given):
        """One more number rule for an existing mailbox, with options: read last, or first with ``first``."""
        self.insert('inbound_rules', id=identity, to_number=number, mailbox_label=mailbox)
        self.insert('access_mailbox_routes', id=identity, mailbox_id=mailbox)
        self.options(identity, 999, **given)
        if first:
            with self.engine.begin() as connection:
                order = [rule for rule in receiving_rules.rule_order(connection, self.tables, self.receiving)
                         if rule != identity]
                receiving_rules.renumber(connection, self.receiving, [identity] + order, NOW)

    def receive(self, to_number, *, sender='+15559990000', account=None, site=None, sub=None, at=None):
        self.count += 1
        identity = f'fax-{self.count}'
        moment = NOW - timedelta(minutes=30)
        facts = ReceivedFacts(to_number=None, account_key=account, site_key=site, subaddress=sub,
                              received_at=at or moment, time_source='provider' if at else 'import',
                              time_zone=DENVER)
        self.inbound.accept(dict(id=identity, from_number=sender, to_number=to_number, status='received',
                                 backend='sip', pages=1, created_at=moment, received_at=moment, updated_at=moment),
                            now=NOW, facts=facts)
        return identity

    def mailbox_of(self, inbound_id):
        parent = self.resource_of(inbound_id)['parent_id']
        return parent.removeprefix('mailbox-') if parent != 'legacy' else None

    def placed(self, inbound_id):
        with self.engine.connect() as connection:
            return receiving_rules.routing_for(connection, self.receiving, inbound_id)


@pytest.fixture
def rw(database):  # noqa: F811
    return RulesWorld(database)


def legacy_route(world, to_number, country='US'):
    """The placement before receiving rules existed, kept here as the yardstick (access/inbound.py at e0c8db45)."""
    number = str(to_number).strip()[:64] if to_number is not None else None
    if not number:
        return None
    canonical = stored_number(number, country=country)
    digits = ''.join(c for c in number if c.isdigit()) or number
    rules, routes = world.tables['inbound_rules'], world.tables['access_mailbox_routes']
    resources, mailboxes = world.tables['access_resources'], world.tables['mailboxes']
    with world.engine.connect() as connection:
        rows = connection.execute(sa.select(rules.c.to_number, resources.c.id)
            .select_from(rules.join(routes, routes.c.id == rules.c.id)
                .join(mailboxes, mailboxes.c.id == routes.c.mailbox_id)
                .join(resources, sa.and_(resources.c.kind == 'mailbox', resources.c.mailbox_id == mailboxes.c.id)))
            .where(resources.c.enabled == 1).order_by(rules.c.created_at, rules.c.id)).all()
    if is_canonical(canonical):
        match = next((row for row in rows if stored_number(row.to_number.strip(), country=country) == canonical), None)
        if match is not None:
            return match.id
    match = next((row for row in rows if (''.join(c for c in row.to_number if c.isdigit())
                                          or row.to_number.strip()) == digits), None)
    return match.id if match is not None else None


def test_without_options_placement_is_exactly_the_old_one(rw):
    # Older rows saved before E.164, a rule for the same digits saved later, a disabled mailbox, and no number.
    rw.insert('mailboxes', id='old', label='Old')
    rw.resource('mailbox-old', 'mailbox', 'installation', 'installation', mailbox_id='old')
    rw.insert('inbound_rules', id='rule-old', to_number='555 010 0004', mailbox_label='Old',
              created_at=NOW - timedelta(days=9))
    rw.insert('access_mailbox_routes', id='rule-old', mailbox_id='old')
    rw.insert('mailboxes', id='off', label='Off')
    rw.resource('mailbox-off', 'mailbox', 'installation', 'installation', mailbox_id='off', enabled=0)
    rw.insert('inbound_rules', id='rule-off', to_number='+15550100005', mailbox_label='Off')
    rw.insert('access_mailbox_routes', id='rule-off', mailbox_id='off')
    numbers = ['+15550100001', '+1 (555) 010-0002', '5550100003', '+15550100004', '5550100004', '+15550100005',
               '+15550109999', '0015550100001', None, '', 'not a number']
    for number in numbers:
        expected = legacy_route(rw, number)
        fax = rw.receive(number)
        assert rw.resource_of(fax)['parent_id'] == (expected or 'legacy'), number
        placed = rw.placed(fax)
        # Placed with no rule options: the record names the mailbox and no rule snapshot is needed to explain it.
        assert placed['rule_id'] == (None if expected is None else placed['rule_id'])
        assert placed['urgent'] in (0, None) and placed['keep_days'] is None


def test_the_first_matching_rule_in_place_order_places_the_fax(rw):
    # Places: an any-number rule for one sender before every number rule.
    rw.rule('rule-sender', '', 'cases', first=True, any_number=True, from_numbers=['+1303*'])
    rw.rule('rule-front-late', '+15550100001', 'billing', from_numbers=['+13035550100'])
    assert rw.mailbox_of(rw.receive('+15550100001', sender='+13035550100')) == 'cases'
    assert rw.mailbox_of(rw.receive('+15550100002', sender='+13035550199')) == 'cases'
    # Another sender is not caught by the any-number rule; the number rule for front still places it.
    assert rw.mailbox_of(rw.receive('+15550100001', sender='+14155550100')) == 'front'
    with rw.engine.begin() as connection:
        connection.execute(rw.receiving['options'].update().where(
            rw.receiving['options'].c.id == 'rule-sender').values(place=50))
    # Moved after the rule for front: front's plain rule (place 1 by creation order) now wins.
    assert rw.mailbox_of(rw.receive('+15550100001', sender='+13035550100')) == 'front'


def test_sender_numbers_match_exactly_or_by_their_beginning(rw):
    rw.rule('rule-exact', '+15550100001', 'billing', first=True, from_numbers=['+1 303 555 0100'])
    rw.rule('rule-prefix', '+15550100001', 'cases', first=True, from_numbers=['+1720*'])
    assert rw.mailbox_of(rw.receive('+15550100001', sender='+13035550100')) == 'billing'
    assert rw.mailbox_of(rw.receive('+15550100001', sender='+17208565062')) == 'cases'
    assert rw.mailbox_of(rw.receive('+15550100001', sender='+13035550101')) == 'front'
    assert rw.mailbox_of(rw.receive('+15550100001', sender=None)) == 'front'


def test_account_and_site_conditions(rw):
    # Read first: the account rule, then the site rule, then the plain rule for the number.
    rw.rule('rule-leeds', '+15550100001', 'cases', first=True, site_key='leeds')
    rw.rule('rule-uk', '+15550100001', 'billing', first=True, account_key='sinch-uk')
    assert rw.mailbox_of(rw.receive('+15550100001', account='sinch-uk', site='leeds')) == 'billing'
    assert rw.mailbox_of(rw.receive('+15550100001', account='sip-leeds', site='leeds')) == 'cases'
    assert rw.mailbox_of(rw.receive('+15550100001', account='sip', site=None)) == 'front'
    placed = rw.placed('fax-2')
    assert (placed['account_key'], placed['site_key'], placed['rule_id']) == ('sip-leeds', 'leeds', 'rule-leeds')


def test_times_follow_the_installation_clock_and_a_window_belongs_to_the_day_it_starts(rw):
    # Weekdays 09:00 to 17:00 in Denver, and Monday nights 18:00 to 07:00.
    rw.rule('rule-office', '+15550100001', 'billing', first=True, days=['mon', 'tue', 'wed', 'thu', 'fri'],
            start_minute=540, end_minute=1020)
    rw.rule('rule-night', '+15550100001', 'cases', first=True, days=['mon'], start_minute=1080, end_minute=420)
    denver = lambda *args: datetime(*args) + timedelta(hours=6)  # noqa: E731 - MDT is UTC-6 in October
    assert rw.mailbox_of(rw.receive('+15550100001', at=denver(2026, 10, 7, 10, 0))) == 'billing'   # Wednesday
    assert rw.mailbox_of(rw.receive('+15550100001', at=denver(2026, 10, 10, 10, 0))) == 'front'   # Saturday
    assert rw.mailbox_of(rw.receive('+15550100001', at=denver(2026, 10, 5, 23, 0))) == 'cases'    # Monday night
    assert rw.mailbox_of(rw.receive('+15550100001', at=denver(2026, 10, 6, 3, 0))) == 'cases'     # ...Tuesday 03:00
    assert rw.mailbox_of(rw.receive('+15550100001', at=denver(2026, 10, 5, 3, 0))) == 'front'     # Monday 03:00
    assert rw.placed('fax-1')['received_time_source'] == 'provider'


def test_a_rule_that_is_off_places_nothing_and_any_number_needs_its_conditions(rw):
    rw.rule('rule-off', '+15550100001', 'billing', first=True, enabled=False)
    rw.rule('rule-any', '', 'cases', any_number=True, account_key='humblefax')
    assert rw.mailbox_of(rw.receive('+15550100001')) == 'front'
    assert rw.mailbox_of(rw.receive('+15550109999', account='humblefax')) == 'cases'
    assert rw.mailbox_of(rw.receive('+15550109999', account='sip')) is None


def test_the_placement_record_keeps_the_rule_as_it_matched(rw):
    rw.rule('rule-urgent', '+15550100001', 'billing', first=True, urgent=True, keep_days=30, email_off=True)
    fax = rw.receive('+15550100001', account='sip', sub='20 01')
    placed = rw.placed(fax)
    snapshot = json.loads(placed['rule_snapshot'])
    assert (placed['rule_id'], placed['rule_version'], placed['mailbox_id']) == ('rule-urgent', 1, 'billing')
    assert (placed['urgent'], placed['keep_days'], placed['subaddress']) == (1, 30, '2001')
    assert snapshot['options']['email_off'] is True and snapshot['mailbox_label'] == 'Billing'
    assert receiving_rules.email_off_for(placed['rule_snapshot']) is True
    # Changing the rule later never rewrites how this fax was placed.
    with rw.engine.begin() as connection:
        connection.execute(rw.receiving['options'].update().values(urgent=0, version=2))
    assert rw.placed(fax)['urgent'] == 1


def test_a_subaddress_routes_and_never_grants_access(rw):
    """M17a: one number plus a subaddress per department. The subaddress is what the sending machine says."""
    rw.rule('rule-sub', '+15550100001', 'billing', first=True, subaddress='2001')
    alice = rw.user('alice')
    rw.assignment('alice', 'role_fax_viewer', 'mailbox-front')
    with rw.engine.connect() as connection:
        grants = connection.execute(sa.select(sa.func.count()).select_from(rw.tables['access_assignments'])).scalar()
    routed = rw.receive('+15550100001', sub='2001')
    plain = rw.receive('+15550100001', sub='2002')
    unstated = rw.receive('+15550100001')
    assert [rw.mailbox_of(fax) for fax in (routed, plain, unstated)] == ['billing', 'front', 'front']
    # Alice reads front's faxes; the fax whose sender stated billing's subaddress is not hers to see.
    assert {row['id'] for row in rw.queries.page(alice)} == {plain, unstated}
    with pytest.raises(FaxAccessError) as error:
        rw.queries.item(alice, routed)
    assert error.value.code == 'not_found'
    with rw.engine.connect() as connection:
        assert connection.execute(sa.select(sa.func.count()).select_from(
            rw.tables['access_assignments'])).scalar() == grants
    assert receiving_rules.normalize_subaddress(' 20 01 ') == '2001'
    assert receiving_rules.normalize_subaddress('ext. 2001') is None


def test_an_import_naming_a_mailbox_files_there_only_for_someone_who_can_read_it(rw):
    alice = rw.user('alice')
    rw.assignment('alice', 'role_fax_viewer', 'mailbox-cases')
    moment = NOW - timedelta(minutes=1)
    row = lambda identity: dict(id=identity, status='received', backend='import', created_at=moment,  # noqa: E731
                                received_at=moment, updated_at=moment)
    rw.inbound.accept(row('filed'), now=NOW, mailbox_id='cases', actor=alice)
    assert rw.mailbox_of('filed') == 'cases' and rw.placed('filed')['mailbox_id'] == 'cases'
    with pytest.raises(FaxAccessError):
        rw.inbound.accept(row('refused'), now=NOW, mailbox_id='billing', actor=alice)
    with pytest.raises(FaxAccessError):
        rw.inbound.accept(row('gone'), now=NOW, mailbox_id='nowhere', actor=alice)
    # Faxbot itself (a connector an administrator set up) files without a person.
    rw.inbound.accept(row('connector'), now=NOW, mailbox_id='billing')
    assert rw.mailbox_of('connector') == 'billing'


def test_option_values_are_checked_with_plain_sentences():
    clean = receiving_rules.clean_options
    with pytest.raises(receiving_rules.ReceivingRuleError, match='both a start and an end'):
        clean({'start_minute': 60})
    with pytest.raises(receiving_rules.ReceivingRuleError, match='not both'):
        clean({'email_off': True, 'email_connector_id': 'c-1'})
    with pytest.raises(receiving_rules.ReceivingRuleError, match='up to 20 digits'):
        clean({'subaddress': 'billing'})
    with pytest.raises(receiving_rules.ReceivingRuleError, match='End it with \\*'):
        clean({'from_numbers': ['303-not']})
    assert clean({'from_numbers': ['3035550100', '+1 720*'], 'days': ['fri', 'mon']})['from_numbers'] == [
        '+13035550100', '+1720*']
    assert clean({'days': ['fri', 'mon']})['days'] == ['mon', 'fri']
