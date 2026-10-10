"""The recipient's decoder (migration 0071, brief 85 M2): capacity pages only for a decoder recorded as reading them.

The capacity layout (payload format 2) is read only by a Faxbot decoder from October 2026 or later, so the chooser
offers it only when you recorded that the recipient's decoder reads it; every other recipient keeps the format 1
layouts. The route's bill then picks the profile: the fewest pages on a per-page route, the shortest call on a
per-minute one.
"""
import random

from api.tests.test_codec_delivery import ADMIN, NUMBER, Prediction, Shape, client, per_page  # noqa: F401 - fixture
from app import codec
from app.codec import capacity, decision


def _document(size, seed=1):
    return codec.Document(random.Random(seed).randbytes(size), 'application/pdf', 'scan.pdf')


def _choose(tools, capacity_ok, size=150_000):
    return decision.choose(_document(size), route_key='sip', destination=NUMBER, pages_original=40,
                           page_bits_original=[40_000] * 40, exact_raster=True, ecm_and_fine_seen=True,
                           provider_renders=False, tools=tools, capacity=capacity_ok)


def test_capacity_pages_are_a_candidate_only_for_a_decoder_that_reads_them():
    tools, seen = per_page()
    older = _choose(tools, False)
    assert older.use and older.layout == 'runs' and 'capacity' not in {shape.layout for shape in seen} | {
        older.layout}
    newer = _choose(tools, True)
    # 150 kB fit on one page in either profile: at the same billed pages the shorter call wins.
    assert newer.use and newer.layout == 'capacity' and newer.profile == capacity.PROFILE_NAMES['time']
    back, report = codec.decode_images(newer.pages.pages)
    assert back == _document(150_000) and report['layout'] == 'capacity'
    # 290 kB need two pages as any run-coded page (limit 7 carries the most) or as time capacity pages, one as pages
    # capacity pages: billed by the page, they win.
    larger = _choose(tools, True, size=290_000)
    assert larger.layout == 'capacity' and larger.profile == capacity.PROFILE_NAMES['pages']
    assert larger.pages_encoded == 1 < _choose(tools, False, size=290_000).pages_encoded


def test_on_a_route_billed_by_the_minute_the_time_profile_wins():
    def predict(route_key, destination, shape, *, now=None):
        # Seconds from the measured MH bits (the coding the call would use), a cent a second.
        measured = dict(shape.measured) if shape.measured else {}
        bits = sum(measured['MH']) if 'MH' in measured else sum(shape.page_bits)
        seconds = bits / 14_400 + 3 * shape.pages
        return Prediction(0, seconds, int(seconds * 10_000), 'synthetic per-second card', False)
    choice = _choose((predict, Shape), True, size=60_000)
    assert choice.use and choice.layout == 'capacity' and choice.profile == capacity.PROFILE_NAMES['time']


def test_the_decoder_is_saved_shown_and_kept_in_the_history(client):  # noqa: F811
    path = f'/codec/numbers/{NUMBER}'
    off = client.get(path, headers=ADMIN).json()
    assert off['decoder'] == 'any' and off['decoder_text'] == 'Any Faxbot decoder'
    on = client.put(path, headers=ADMIN, json={'enabled': True, 'recipient_agreed': True, 'decoder': 'capacity',
                                              'version': 0})
    assert on.status_code == 200, on.text
    view = on.json()
    assert view['decoder'] == 'capacity'
    assert view['decoder_text'] == 'A Faxbot decoder from October 2026 or later, which also reads capacity pages'
    assert view['history'][0]['decoder'] == view['decoder_text']
    back = client.put(path, headers=ADMIN, json={'enabled': True, 'decoder': 'any', 'version': view['version']})
    assert back.status_code == 200 and back.json()['decoder'] == 'any'
    assert [change['action'] for change in back.json()['history']] == ['changed', 'on']
    refused = client.put(path, headers=ADMIN, json={'enabled': True, 'decoder': 'format9',
                                                    'version': back.json()['version']})
    assert refused.status_code == 422


def test_the_command_line_sets_the_decoder(monkeypatch, tmp_path):
    import httpx
    from typer.testing import CliRunner
    from app.cli.main import app
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('FAXBOT_CLI_CONFIG', str(tmp_path / 'absent-config.toml'))
    sent = []
    view = {'number': NUMBER, 'enabled': True, 'style': 'dense', 'fec': 'medium', 'decoder': 'capacity',
            'decoder_text': 'A Faxbot decoder from October 2026 or later, which also reads capacity pages',
            'has_key': False, 'key_fingerprint': None, 'version': 2, 'state_sentence': 'On.', 'agreement': None,
            'history': [], 'agreement_text': 'Agreed.', 'limits_text': 'Experimental.'}

    def handle(request):
        if request.method == 'PUT':
            import json
            sent.append(json.loads(request.content))
        return httpx.Response(200, json={**view, 'version': 1} if request.method == 'GET' else view)

    with httpx.Client(transport=httpx.MockTransport(handle), base_url='https://faxbot.example') as http:
        result = CliRunner().invoke(app, ['--url', 'https://faxbot.example', '--key', 'synthetic', 'recipients',
                                          'encoded', 'set', NUMBER, '--decoder', 'capacity'],
                                    obj={'client_factory': lambda *_: (http, False)})
    assert result.exit_code == 0, result.output
    assert sent and sent[0]['decoder'] == 'capacity'
    assert 'October 2026' in ' '.join(result.output.split())
    refused = CliRunner().invoke(app, ['--url', 'https://faxbot.example', '--key', 'synthetic', 'recipients',
                                       'encoded', 'set', NUMBER, '--decoder', 'newest'])
    assert refused.exit_code != 0 and 'Choose --decoder any or --decoder capacity.' in refused.output
