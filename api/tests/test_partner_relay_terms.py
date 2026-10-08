"""Partner relay pieces with no partner: the terms a relay signs, its hours and places, the planner's order,
rules that name relays, outcome receipts and the true sender's line. Synthetic numbers only."""
from datetime import datetime, timedelta
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
import sqlalchemy as sa

from api.app.direct import relay
from api.app.direct.relay import (RelayConflict, check_terms, covers, hours_open, month_start, parse_outcome,
                                  parse_terms, rank, relay_candidates)
from api.app.direct.relay_pages import marketing_lines, stamp_tiff
from api.app.direct.relay_store import RelayStore
from api.app.direct.crypto import timestamp
from api.app.routing.costs import Money
from api.app.routing.predict import Shape
from api.app.rules import model
from api.app.rules.compile import compile_document, document_problems
from api.app.rules.evaluate import decide
from api.app.schema import create_database_engine, upgrade_schema


DEST = '+61755501234'
NOW = datetime(2026, 10, 7, 3, 0)  # 2 PM in Sydney, a Wednesday


def test_a_grant_becomes_the_terms_the_relay_signs_and_bad_ones_are_refused_in_a_sentence():
    terms = parse_terms({'countries': ['au'], 'regions': ['anz'], 'monthly_pages': 500,
                         'monthly_spend': {'amount': '80', 'currency': 'AUD'},
                         'hours': {'days': ['mon', 'tue', 'wed', 'thu', 'fri'], 'from': '08:00', 'until': '18:00'},
                         'together': True}, zone_name='Australia/Sydney',
                        regions={'anz': {'name': 'Australia and New Zealand', 'countries': ['NZ'], 'prefixes': ['+64']}})
    assert terms == {'countries': ['AU', 'NZ'], 'prefixes': ['+64'],
                     'regions': [{'key': 'anz', 'name': 'Australia and New Zealand', 'countries': ['NZ']}],
                     'monthly_pages': 500,
                     'monthly_spend': {'amount_micros': 80_000_000, 'currency': 'AUD'},
                     'hours': {'days': ['mon', 'tue', 'wed', 'thu', 'fri'], 'start_minute': 480, 'end_minute': 1080},
                     'together': True, 'same_organization': False, 'time_zone': 'Australia/Sydney'}
    assert check_terms(json.loads(json.dumps(terms))) == terms
    assert relay.limits_text(terms) == 'up to 500 pages and 80.00 AUD a month, weekdays 8:00 AM to 6:00 PM their time'
    assert relay.places_text(terms) == 'Australia and New Zealand and numbers in Australia'
    assert relay.places_text(parse_terms({'countries': ['AU']}, zone_name='')) == 'numbers in Australia'
    for raw, sentence in (({}, 'Choose at least one country this partner may send faxes to through you.'),
                          ({'countries': ['Australia']}, 'Name countries by their two-letter codes, such as AU.'),
                          ({'countries': ['AU'], 'monthly_pages': 0},
                           'A monthly page limit is a whole number from 1 to 1,000,000, or none.'),
                          ({'countries': ['AU'], 'regions': ['nowhere']}, 'There is no region “nowhere” in your rules.'),
                          ({'countries': ['AU'], 'hours': {'from': '09:00', 'until': '09:00'}},
                           'Hours need at least one day and two different times, such as 08:00 and 18:00.')):
        with pytest.raises(RelayConflict) as refused:
            parse_terms(raw, zone_name='')
        assert str(refused.value) == sentence
    # A partner's terms are read strictly: anything unexpected is not an offer.
    assert check_terms({**terms, 'extra': True}) is None
    assert check_terms({**terms, 'countries': [], 'prefixes': []}) is None


def test_places_and_hours_are_read_on_the_relays_own_clock():
    terms = parse_terms({'countries': ['AU'], 'hours': {'days': ['mon', 'tue', 'wed', 'thu', 'fri'],
                                                        'from': '08:00', 'until': '18:00'}},
                        zone_name='Australia/Sydney')
    assert covers(terms, DEST) and not covers(terms, '+441134960123') and not covers(terms, 'not a number')
    assert hours_open(terms, NOW)                                  # Wednesday 2 PM in Sydney
    assert not hours_open(terms, NOW + timedelta(hours=6))         # 8 PM
    assert not hours_open(terms, datetime(2026, 10, 10, 3, 0))     # Saturday 2 PM
    overnight = parse_terms({'countries': ['AU'], 'hours': {'days': ['fri'], 'from': '22:00', 'until': '06:00'}},
                            zone_name='Australia/Sydney')
    assert hours_open(overnight, datetime(2026, 10, 9, 12, 0))     # Friday 11 PM
    assert hours_open(overnight, datetime(2026, 10, 9, 18, 0))     # Saturday 5 AM, the window that began Friday
    assert not hours_open(overnight, datetime(2026, 10, 10, 12, 0))  # Saturday 11 PM
    # Months count on the relay's clock: Sydney's October began at 1 PM UTC on 30 September.
    assert month_start(NOW, 'Australia/Sydney') == datetime(2026, 9, 30, 14, 0)


# The planner's order --------------------------------------------------------------------------------------------

def _price(per_page_micros, *, home='AU', now=NOW):
    return {'priced_at': timestamp(now), 'valid_until': timestamp(now + timedelta(days=31)), 'home': home,
            'routes': [{'country': 'AU', 'kind': 'local', 'label': 'Telstra', 'link': {},
                        'terms': None if per_page_micros is None else {
                            'currency': 'USD', 'per_minute_micros': 0, 'per_page_micros': per_page_micros,
                            'per_call_micros': 0, 'billing_increment_seconds': 60, 'minimum_seconds': 0,
                            'monthly_fee_micros': None, 'destination_class': 'local', 'page_time_seconds': None,
                            'included_pages': None, 'overage_page_micros': None, 'max_pages_per_fax': None}}]}


@pytest.fixture
def sender(tmp_path):
    engine = create_database_engine('sqlite:///' + str(tmp_path / 'sender.db'))
    upgrade_schema(engine)
    peers = sa.Table('direct_peers', sa.MetaData(), autoload_with=engine)
    store = RelayStore(engine)

    def partner(name, number, per_page, *, state='active', monthly_pages=None, hours=None):
        peer_id = uuid4().hex
        with engine.begin() as connection:
            connection.execute(peers.insert().values(
                id=peer_id, organization=name, phone_number=number, endpoint_url=f'https://{peer_id[:6]}.example',
                signing_key=peer_id[:43].ljust(43, 'a'), exchange_key='e' * 43, state='verified',
                challenge_failures=0, version=1, created_at=NOW, updated_at=NOW))
        terms = parse_terms({'countries': ['AU'], 'monthly_pages': monthly_pages, 'hours': hours},
                            zone_name='Australia/Sydney')
        offer = {'statement': json.dumps({'type': 'relay_offer', 'price': _price(per_page)}), 'signature': 's'}
        store.create(agreement_id=uuid4().hex, peer_id=peer_id, role='sender', state=state, terms=terms,
                     statement=offer, direction='received', now=NOW)
        return peer_id
    yield SimpleNamespace(engine=engine, store=store, partner=partner)
    engine.dispose()


def test_relay_candidates_come_cheapest_first_with_unknown_prices_last(sender):
    shape = Shape(2, None, 'fine', 'normal')
    expensive = sender.partner('Perth office', '+61855501234', 90_000)
    cheap = sender.partner('Sydney office', '+61255501234', 20_000)
    unpriced = sender.partner('Hobart office', '+61355501234', None)
    sender.partner('Darwin office', '+61855501299', 10_000, state='offered')  # not accepted: never a candidate
    found = relay_candidates(DEST, shape, NOW, engine=sender.engine)
    assert [candidate.peer_id for candidate in found] == [cheap, expensive, unpriced]
    assert found[0].cost == Money(40_000, 'USD') and found[0].key == 'relay:' + cheap
    # US dollars read as "$" only on a US installation; anywhere else (or not said) the currency is named.
    assert found[0].sentence == 'Through Sydney office as a local call there, about 0.04 USD.'
    assert relay_candidates(DEST, shape, NOW, engine=sender.engine, home='US')[0].sentence.endswith('about $0.04.')
    assert found[2].cost is None and found[2].sentence == "Through Hobart office as a local call there; its price is not known."
    assert found[0].ledger_key == 'relay.' + cheap == relay.ledger_key(found[0].key)
    # A rule's "never relay", or one relay named under never, removes candidates.
    assert relay_candidates(DEST, shape, NOW, engine=sender.engine, never=('relay',)) == []
    assert [c.peer_id for c in relay_candidates(DEST, shape, NOW, engine=sender.engine,
                                                never=('relay:' + cheap,))] == [expensive, unpriced]
    # An expired price statement is an unknown price, never free.
    later = relay_candidates(DEST, shape, NOW + timedelta(days=40), engine=sender.engine)
    assert all(candidate.cost is None for candidate in later)


def test_the_order_the_planner_should_take_among_relays_and_its_own_routes(sender):
    """For WP-C: no-call routes first, then every priced route by cost (relays and own alike), unknown last."""
    cheap = sender.partner('Sydney office', '+61255501234', 20_000)
    unpriced = sender.partner('Hobart office', '+61355501234', None)
    relays = relay_candidates(DEST, Shape(2, None, 'fine', 'normal'), NOW, engine=sender.engine)
    direct = SimpleNamespace(key='direct', cost=Money(0, 'USD'), organization='')
    own = SimpleNamespace(key='phaxio', cost=Money(200_000, 'USD'), organization='')
    unknown_own = SimpleNamespace(key='sinch', cost=None, organization='')
    ordered = sorted([own, unknown_own, *relays, direct], key=rank)
    assert [item.key for item in ordered[:3]] == ['direct', 'relay:' + cheap, 'phaxio']
    assert {item.key for item in ordered[3:]} == {'relay:' + unpriced, 'sinch'}


def test_limits_and_hours_the_sender_knows_keep_a_relay_out_of_the_plan(sender):
    shape = Shape(3, None, 'fine', 'normal')
    small = sender.partner('Sydney office', '+61255501234', 20_000, monthly_pages=2)
    assert relay_candidates(DEST, shape, NOW, engine=sender.engine) == []
    assert [c.peer_id for c in relay_candidates(DEST, Shape(2, None, 'fine', 'normal'), NOW,
                                                engine=sender.engine)] == [small]
    sender.partner('Perth office', '+61855501234', 20_000,
                   hours={'days': ['mon', 'tue', 'wed', 'thu', 'fri'], 'from': '08:00', 'until': '18:00'})
    assert relay_candidates(DEST, shape, datetime(2026, 10, 10, 3, 0), engine=sender.engine) == []


def test_a_fax_relayed_for_a_partner_is_never_relayed_again(sender):
    sender.partner('Sydney office', '+61255501234', 20_000)
    job = uuid4().hex
    peer = sender.store.agreements_for(role='sender')[0]
    with sender.engine.begin() as connection:
        sender.store.add_fax_on(connection, role='relay', message_id=uuid4().hex, agreement_id=peer['id'],
                                peer_id=peer['peer_id'], job_id=job, destination=DEST, pages=1, state='accepted')
    assert relay_candidates(DEST, Shape(1, None, 'fine', 'normal'), NOW, engine=sender.engine, job_id=job) == []


# Rules ------------------------------------------------------------------------------------------------------

ACCOUNTS = (model.Account('sip', 'sip', 'Telnyx trunk', default=True, automatic=True),)
RELAY = 'relay:' + 'a' * 32


def _decide(limits=(), routes=()):
    document = {'format': 1, 'limits': list(limits), 'routes': list(routes)}
    compiled = compile_document(model.RevisionRef('organization', '', 'org-1', 1), document)
    facts = model.Facts(DEST, '2026-10-07T03:00:00', country='AU')
    return decide({'organization': compiled}, facts, ACCOUNTS)


def test_rules_name_a_relay_like_an_account_and_never_relay_is_a_limit():
    route = {'id': 'au', 'name': 'Australia', 'on': True, 'when': {'destination': {'countries': ['AU']}},
             'then': {'try_in_order': [RELAY, 'sip']}}
    decision = _decide(routes=[route])
    assert decision.envelope.accounts == (RELAY, 'sip')
    never = {'id': 'no-relay', 'name': 'No relays', 'on': True, 'when': {}, 'then': {'never': ['relay']}}
    decision = _decide(limits=[never], routes=[route])
    assert decision.envelope.accounts == ('sip',)
    assert ('relay', 'never') in {(item.account, item.why) for item in decision.excluded}
    assert (RELAY, 'never') in {(item.account, item.why) for item in decision.excluded}
    # Direct-only and encrypted-only faxes never go through a relay: its call is ordinary and it sees the pages.
    direct_only = {'id': 'direct', 'name': 'Direct only', 'on': True, 'when': {}, 'then': {'require_direct': True}}
    assert (RELAY, 'direct_required') in {(i.account, i.why) for i in _decide([direct_only], [route]).excluded}
    # A malformed relay key is not an account.
    bad = {**route, 'then': {'use': 'relay:sydney'}}
    assert document_problems('organization', {'format': 1, 'limits': [], 'routes': [bad]})
    assert model.is_relay(RELAY) and not model.is_relay('relay') and model.RELAY in model.RESERVED_KEYS


def test_the_rules_check_knows_which_relays_are_in_force():
    from api.app.rules.check import CheckContext, check
    route = {'id': 'au', 'name': 'Australia', 'on': True, 'when': {}, 'then': {'use': RELAY}}
    document = {'format': 1, 'limits': [], 'routes': [route]}
    problems = check('organization', '', document, CheckContext(accounts=ACCOUNTS, relays=frozenset({'b' * 32})))
    assert any(problem.code == 'unknown_account' for problem in problems)
    problems = check('organization', '', document, CheckContext(accounts=ACCOUNTS, relays=frozenset({'a' * 32})))
    assert not any(problem.code == 'unknown_account' for problem in problems)


# Receipts and the true sender's line -----------------------------------------------------------------------------

def test_an_outcome_receipt_is_read_strictly():
    good = {'type': 'relay_outcome', 'status': 'delivered', 'pages': 2, 'seconds': 41, 'shared': False,
            'detail': 'Sydney office delivered it as a local call.',
            'charge': {'amount_micros': 70_000, 'currency': 'AUD', 'basis': 'reported'}}
    assert parse_outcome(good)['status'] == 'delivered'
    for bad in ({'status': 'resent'}, {'pages': -1}, {'charge': {'amount_micros': 1, 'currency': 'AUD'}},
                {'shared': 'no'}, {'type': 'relay_price'}):
        assert parse_outcome({**good, **bad}) is None


def test_marketing_details_go_on_the_first_page_only():
    import io
    from PIL import Image
    frames = [Image.new('1', (1728, 300), 1) for _ in range(2)]
    output = io.BytesIO()
    frames[0].save(output, 'TIFF', compression='group4', save_all=True, append_images=frames[1:], dpi=(204, 196))
    lines = marketing_lines({'business_number': 'ABN 00 000 000 000', 'contact': '+441134960123',
                             'opt_out': 'stop@example.com'}, DEST)
    # ACMA's list for a marketing fax: business number, contact details, the number it is sent to, and opting out.
    assert lines == ('Business number: ABN 00 000 000 000', 'Contact: +441134960123', 'Sent to: +61755501234',
                     'To stop these faxes: stop@example.com')
    stamped, pages, line = stamp_tiff(output.getvalue(), header='Leeds HQ', station='+441134960123', moment=NOW,
                                      zone_name='Australia/Sydney', first_page=lines)
    assert pages == 2 and line == ' 7-Oct-2026   14:00   Leeds HQ   +441134960123   p.1'
    with Image.open(io.BytesIO(stamped)) as image:
        image.seek(0)
        first = image.size[1]
        image.seek(1)
        second = image.size[1]
    # The header line's 32 rows above every page; on the first page the four marketing lines in larger type
    # (at least 10 point) add 40 rows each.
    assert (first - 300, second - 300) == (32 + 4 * 40, 32)
    assert marketing_lines(None) == () and marketing_lines({'contact': '  '}, DEST) == ()
