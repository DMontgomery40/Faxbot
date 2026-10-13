"""Which encoded pages a partner's Faxbot reads (``payload_layouts``, migration 0082), and the gate that keeps the key
away from partners that cannot parse it.

Two installations go through the real direct signing (``test_peer_fax.peer_pair``: B is the running application,
A a direct service posting to it), on SQLite and on PostgreSQL. The older partner is a fake that answers exactly as
``main`` 049c029e does: its ``parse_capabilities`` (copied below) refuses any key it does not know, and its
``note`` then answers 400. Every number and document is synthetic. Not yet run against a real partner.
"""
import asyncio
from datetime import datetime, timedelta
import json
import random

import pytest
import sqlalchemy as sa

from app import codec, main
from api.app import schema, schema_partner_layouts
from api.app.codec import decision, variants
from api.app.codec.send import partner_variants
from api.app.direct import http as direct_http
from api.app.direct.crypto import (DirectProtocolError, capabilities, parse_capabilities, parse_payload_layouts,
                                   parse_timestamp, timestamp)
from api.tests.test_access_schema import at_revision
from api.tests.test_codec_delivery import NUMBER, per_page
from api.tests.test_digital_schema import _downgrade, _table
from api.tests.test_direct_delivery import B_NUMBER
from api.tests.test_peer_fax import accept, peer_pair, send  # noqa: F401 - fixture
from api.tests.test_routing_http import ADMIN
from api.tests.test_schema import database, snapshot  # noqa: F401 - fixture

NOW = datetime(2026, 10, 10, 12)


def strict_parse_capabilities(value):
    """``parse_capabilities`` exactly as main 049c029e (and every release before ff0ecc2e) has it."""
    if (not isinstance(value, dict) or set(value) - {'own_engine'} != {'fax_images', 'peer_calls', 'said_at'}
            or type(value['fax_images']) is not bool or type(value['peer_calls']) is not bool
            or ('own_engine' in value and type(value['own_engine']) is not bool)):
        return None
    try:
        return value['fax_images'], value['peer_calls'], parse_timestamp(value['said_at'])
    except DirectProtocolError:
        return None


# The statement and its key -------------------------------------------------------------------------------------

def test_this_release_announces_every_variant_its_decoder_reads():
    from api.app.codec import capacity, enumerative, pages
    assert set(pages.LAYOUTS) == {'grid', 'runs', 'picture', 'enumerative', 'capacity'}
    assert variants.DECODED == {'grid', 'runs', 'picture', f'enumerative/{enumerative.PROFILE}'} | {
        f'capacity/{profile}' for profile in capacity.PROFILES}
    assert variants.BASELINE == {'grid', 'runs', 'picture'}
    said = capabilities(fax_images=True, peer_calls=False, payload_layouts=variants.DECODED)
    assert said['payload_layouts'] == sorted(variants.DECODED)
    assert parse_payload_layouts(said) == variants.DECODED
    assert variants.described(variants.DECODED) == [
        'grid', 'run-coded', 'picture', 'enumerative profile 1', 'capacity (shortest call)', 'capacity (balanced)',
        'capacity (fewest pages)']
    # Without the key the statement is exactly what every earlier release sent.
    assert 'payload_layouts' not in capabilities(fax_images=True, peer_calls=False)


def test_a_wrong_shape_reads_as_not_said_and_never_refuses_the_statement():
    said = capabilities(fax_images=True, peer_calls=True, said_at='2026-10-10T12:00:00Z')
    for shape in ({'grid': 1}, 'grid', None, 7, ['grid'] * 33):
        newer = {**said, 'payload_layouts': shape}
        assert parse_payload_layouts(newer) is None
        assert parse_capabilities(newer) is not None  # the known keys still count
    # Items this release cannot read are skipped; tokens it does not know are kept but chosen by nobody here.
    mixed = {**said, 'payload_layouts': ['grid', 'GRID', {'x': 1}, 'capacity/2', 'hexagon/4']}
    assert parse_payload_layouts(mixed) == {'grid', 'capacity/2', 'hexagon/4'}
    assert variants.allowed_for_partner(parse_payload_layouts(mixed)) == {'grid', 'capacity/2'}
    assert parse_payload_layouts(said) is None  # an older partner's statement: not said


def test_an_older_partners_parser_refuses_the_key_which_is_why_it_is_gated():
    said = capabilities(fax_images=True, peer_calls=False, own_engine=False)
    assert strict_parse_capabilities(said) is not None
    assert strict_parse_capabilities({**said, 'payload_layouts': sorted(variants.DECODED)}) is None
    assert parse_capabilities({**said, 'payload_layouts': sorted(variants.DECODED)}) is not None


# The chooser ---------------------------------------------------------------------------------------------------

def _document(size, seed=3):
    return codec.Document(random.Random(seed).randbytes(size), 'application/pdf', 'scan.pdf')


def _choose(allowed, *, capacity=True, style='dense', size=150_000):
    tools, seen = per_page()
    choice = decision.choose(_document(size), route_key='sip', destination=NUMBER, pages_original=40,
                             page_bits_original=[40_000] * 40, exact_raster=True, ecm_and_fine_seen=True,
                             provider_renders=False, tools=tools, capacity=capacity, style=style, allowed=allowed)
    return choice, seen


def test_the_chooser_makes_and_prices_only_variants_the_partner_reads():
    everything, _ = _choose(None)
    assert everything.use and everything.layout == 'capacity'
    # A partner that announced only the format 1 layouts never gets capacity pages, even with your capacity setting.
    baseline, seen = _choose(variants.BASELINE)
    assert baseline.use and baseline.layout == 'runs' and all(shape.layout != 'capacity' for shape in seen)
    # A partner that reads only grid pages gets grid pages, never run-coded ones.
    grid, _ = _choose(frozenset({'grid'}))
    assert grid.use and grid.layout == 'grid'
    # The capacity profile it announced is the only one tried.
    pages_only, _ = _choose(frozenset({'capacity/3'}))
    assert pages_only.layout == 'capacity' and pages_only.profile == 3
    # A partner that reads none of what this route could carry keeps normal pages, with that reason.
    none, _ = _choose(frozenset({'grid', 'runs'}), style='picture')
    assert none.use is False and none.sentence == decision.NOT_READ


def test_a_partner_without_the_key_gets_the_baseline_plus_capacity_only_when_you_recorded_it():
    assert variants.allowed_for_partner(None) == variants.BASELINE
    assert variants.allowed_for_partner(None, decoder='capacity') == variants.BASELINE | {
        'capacity/1', 'capacity/2', 'capacity/3'}
    # A partner that announced decides, whatever you recorded.
    assert variants.allowed_for_partner(frozenset({'grid'}), decoder='capacity') == {'grid'}


# Two installations through the real direct signing ---------------------------------------------------------------

def _peer_row(service, peer_id):
    return service.store.get_peer(peer_id)


@pytest.mark.asyncio
async def test_two_upgraded_partners_learn_each_others_layouts_from_one_tell(peer_pair):  # noqa: F811
    pair = peer_pair
    a, b = pair['a'], direct_http.service_for(main.app)
    a_on_b, b_on_a = pair['a_on_b']['id'], pair['b_on_a']['id']
    # Before either has said anything: the real chooser gives B the variants every Faxbot reads.
    assert partner_variants(a.store.engine, B_NUMBER) == variants.BASELINE
    assert partner_variants(a.store.engine, B_NUMBER, decoder='capacity') >= {'capacity/3'}
    assert partner_variants(a.store.engine, '+12025550177') is None  # not a partner: today's rules
    # Neither statement carries the key yet: neither partner has shown it parses it.
    assert 'payload_layouts' not in a.offered(_peer_row(a, b_on_a))
    assert 'payload_layouts' not in b.offered(_peer_row(b, a_on_b))
    # Both are due to be told once by this release.
    assert [row['id'] for row in a.store.untold()] == [b_on_a]
    assert [row['id'] for row in b.store.untold()] == [a_on_b]
    # B tells A without the key; A's signed answer carries A's layouts; B re-tells A at once with its own.
    await b.tell_partners()
    assert _peer_row(b, a_on_b)['partner_payload_layouts'] == variants.stored(variants.DECODED)
    assert _peer_row(b, a_on_b)['told_payload_layouts'] == variants.stored(variants.DECODED)
    assert _peer_row(a, b_on_a)['partner_payload_layouts'] == variants.stored(variants.DECODED)
    assert b.store.untold() == []
    assert partner_variants(a.store.engine, B_NUMBER) == variants.DECODED
    # A still owes B its statement with the key, once; then neither side has anything left to tell.
    assert [row['id'] for row in a.store.untold()] == [b_on_a]
    posts = pair['to_b'].posts
    await a.tell_partners()
    assert pair['to_b'].posts == posts + 1
    assert _peer_row(a, b_on_a)['told_payload_layouts'] == variants.stored(variants.DECODED)
    assert a.store.untold() == [] and b.store.untold() == []
    await a.tell_partners()
    await b.tell_partners()
    assert pair['to_b'].posts == posts + 1  # no loop
    # Receipts now carry the key both ways; the Partners page and the API say what each reads.
    assert a.offered(_peer_row(a, b_on_a))['payload_layouts'] == sorted(variants.DECODED)
    view = pair['b_client'].get('/direct/peers', headers=ADMIN).json()['peers'][0]
    assert view['payload_layouts'][:3] == ['grid', 'run-coded', 'picture']
    assert view['payload_layouts_text'].startswith('Their Faxbot reads these encoded pages, and only these go to them')


@pytest.mark.asyncio
async def test_an_older_partners_newest_statement_without_the_key_puts_it_back_on_the_baseline(peer_pair):  # noqa: F811
    pair = peer_pair
    a, b = pair['a'], direct_http.service_for(main.app)
    b_on_a = pair['b_on_a']['id']
    await b.tell_partners()
    assert partner_variants(a.store.engine, B_NUMBER) == variants.DECODED
    # B is restored to an older release: its next signed statement says nothing about layouts.
    older = {'type': 'capabilities', 'recipient': a.identity().signing_key, 'signer': b.identity().signing_key,
             'capabilities': capabilities(fax_images=True, peer_calls=False,
                                          said_at=timestamp(datetime.utcnow() + timedelta(seconds=2)))}
    assert a.heard(_peer_row(a, b_on_a), older) is True
    assert _peer_row(a, b_on_a)['partner_payload_layouts'] is None
    assert partner_variants(a.store.engine, B_NUMBER) == variants.BASELINE
    assert 'payload_layouts' not in a.offered(_peer_row(a, b_on_a))


@pytest.mark.asyncio
async def test_a_receipt_carries_the_key_only_to_a_partner_that_announced_it(peer_pair):  # noqa: F811
    pair = peer_pair
    a, b = pair['a'], direct_http.service_for(main.app)
    a_on_b, b_on_a = pair['a_on_b']['id'], pair['b_on_a']['id']
    # Before any statement: B's receipt for A's fax carries B's capabilities without the key, which A keeps.
    job = accept(pair)
    await send(pair, job)
    assert _peer_row(a, b_on_a)['partner_said_at'] is not None
    assert _peer_row(a, b_on_a)['partner_payload_layouts'] is None
    # Once A announced its layouts to B, B's next receipt carries B's.
    a_said = {'type': 'capabilities', 'recipient': b.identity().signing_key, 'signer': a.identity().signing_key,
              'capabilities': a.offered(_peer_row(a, b_on_a), layouts=True)}
    assert b.heard(_peer_row(b, a_on_b), a_said) is True
    assert 'payload_layouts' in b.offered(_peer_row(b, a_on_b))
    await send(pair, accept(pair))
    assert _peer_row(a, b_on_a)['partner_payload_layouts'] == variants.stored(variants.DECODED)


class OlderB:
    """A's transport to a partner on an older release (main 049c029e): it records a statement its strict parser
    reads and answers 400 for any other, including one with a key it does not know."""

    def __init__(self):
        self.statements = []

    async def request(self, method, url, **kwargs):
        assert method == 'POST' and url.endswith('/direct/capabilities')
        body = json.loads(kwargs['json']['statement'])
        self.statements.append(body)
        found = strict_parse_capabilities(body['capabilities'])
        if found is None or set(body) != {'type', 'signer', 'recipient', 'capabilities'}:
            return 400, {'recorded': False, 'detail': 'This statement could not be checked.'}
        return 200, {'recorded': True}


@pytest.mark.asyncio
async def test_an_older_partner_never_gets_the_key_and_the_tell_loop_ends(peer_pair):  # noqa: F811
    pair = peer_pair
    a = pair['a']
    b_on_a = pair['b_on_a']['id']
    older = OlderB()
    a.http = older
    assert await a.tell_partner(_peer_row(a, b_on_a)) == 'told'
    assert len(older.statements) == 1 and 'payload_layouts' not in older.statements[0]['capabilities']
    assert _peer_row(a, b_on_a)['told_payload_layouts'] == ''
    assert a.store.untold() == []
    await a.tell_partners()
    assert len(older.statements) == 1  # told once; never again until something changes
    # Had the key gone out, this partner would have refused the whole statement.
    with_key = a.offered(_peer_row(a, b_on_a), layouts=True)
    assert strict_parse_capabilities(with_key) is None


@pytest.mark.asyncio
async def test_an_answer_that_does_not_check_is_not_kept(peer_pair, caplog):  # noqa: F811
    pair = peer_pair
    a = pair['a']
    b_on_a = pair['b_on_a']['id']

    class Forged:
        async def request(self, method, url, **kwargs):
            return 200, {'recorded': True, 'statement': {'statement': json.dumps({
                'type': 'capabilities', 'signer': 'x' * 43, 'recipient': 'y' * 43,
                'capabilities': capabilities(fax_images=True, peer_calls=True, payload_layouts=['grid'])}),
                'signature': 'z' * 86}}
    a.http = Forged()
    assert await a.tell_partner(_peer_row(a, b_on_a)) == 'told'
    assert _peer_row(a, b_on_a)['partner_payload_layouts'] is None
    assert "could not be checked" in caplog.text


# The migration -------------------------------------------------------------------------------------------------

def test_0082_follows_0079_and_adds_two_nullable_columns():
    assert schema_partner_layouts.REVISION == '0082_partner_layouts' == schema.PARTNER_LAYOUTS
    assert schema.POTS_QUOTE == '0079_pots_quote'
    assert schema_partner_layouts.TABLES == frozenset()


def test_0082_keeps_every_partner_tells_each_once_and_downgrades(database):  # noqa: F811
    at_revision(database, '0079_pots_quote')
    with database.begin() as connection:
        connection.execute(_table(database, 'direct_peers').insert().values(
            id='peer-1', organization='Valley Hospital', phone_number='+15550100001',
            endpoint_url='https://valley.example', signing_key='s' * 43, exchange_key='x' * 43, state='verified',
            challenge_failures=0, version=1, told_partner_at=NOW, created_at=NOW, updated_at=NOW))
    schema.upgrade_schema(database)
    after = snapshot(database)
    assert after['alembic_version'] == [{'version_num': schema.HEAD}]
    [row] = after['direct_peers']
    assert (row['partner_payload_layouts'], row['told_payload_layouts']) == (None, None) and row['told_partner_at']
    from api.app.direct.store import DirectStore
    # Told before this release: due once more, so the partner hears the layouts this release reads.
    assert [peer['id'] for peer in DirectStore(database).untold()] == ['peer-1']
    _downgrade(database, '0079_pots_quote')
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == '0079_pots_quote'
        columns = {column['name'] for column in sa.inspect(connection).get_columns('direct_peers')}
    assert not columns & {'partner_payload_layouts', 'told_payload_layouts'}
    schema.upgrade_schema(database)
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD


def test_the_command_line_shows_what_a_partner_reads(monkeypatch, tmp_path):
    import httpx
    from typer.testing import CliRunner
    from app.cli.main import app
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('FAXBOT_CLI_CONFIG', str(tmp_path / 'absent-config.toml'))
    peer = {'id': 'p' * 32, 'organization': 'County Clinic', 'fax_number': B_NUMBER, 'endpoint': 'https://b.example',
            'state': 'verified', 'status': 'Verified; faxes to this number are delivered directly.',
            'verified_at': '2026-10-10T12:00:00', 'expires_at': '2027-10-10T12:00:00', 'fax_images_text': None,
            'peer_calls_text': None, 'notice_fax_text': None, 'certificate_text': None,
            'payload_layouts': ['grid', 'run-coded'],
            'payload_layouts_text': 'Their Faxbot reads these encoded pages, and only these go to them: grid, '
                                    'run-coded.'}

    def handle(request):
        return httpx.Response(200, json={'peers': [peer]})

    with httpx.Client(transport=httpx.MockTransport(handle), base_url='https://faxbot.example') as http:
        result = CliRunner().invoke(app, ['--url', 'https://faxbot.example', '--key', 'synthetic', 'recipients',
                                          'partners', 'list', 'County Clinic'],
                                    obj={'client_factory': lambda *_: (http, False)})
    assert result.exit_code == 0, result.output
    assert 'only these go to them: grid, run-coded.' in ' '.join(result.output.split())
