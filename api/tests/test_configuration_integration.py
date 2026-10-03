"""Real catalog, store and manifest HTTP calls retain accepted account frames."""
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

import pytest

from api.tests.test_schema import database
from api.app.schema import upgrade_schema
from api.app.config_activation import ConfigurationManager
from api.app.config_store import ConfigurationStore
from api.app.plugins.http_provider import HttpManifest, HttpProviderRuntime


@pytest.fixture
def provider_http():
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))))
            requests.append((self.path, self.headers.get('X-API-Key'), data))
            payload = json.dumps({'id': 'provider-job', 'status': 'queued'}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.05), daemon=True)
    thread.start()
    try:
        yield 'http://127.0.0.1:' + str(server.server_port), requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.asyncio
async def test_installed_manifest_changes_do_not_redirect_accepted_fax(database, tmp_path, provider_http):
    endpoint, requests = provider_http
    upgrade_schema(database)
    store = ConfigurationStore(database, tmp_path / 'configuration.key')
    manager = ConfigurationManager(store)
    providers = tmp_path / 'providers'
    first = manager.initialize({'FEATURE_V3_PLUGINS': 'true', 'FAXBOT_PROVIDERS_DIR': str(providers),
        'FAXBOT_CONFIG_PATH': str(tmp_path / 'absent.json')})
    path = providers / 'custom' / 'manifest.json'
    path.parent.mkdir(parents=True)
    manifest = {'id': 'custom', 'auth': {'scheme': 'api_key_header'}, 'allowed_domains': ['127.0.0.1'],
        'actions': {'send_fax': {'url': endpoint + '/original', 'method': 'POST',
            'body': {'kind': 'json', 'template': '{"to":"{{to}}","url":"{{file_url}}"}'}}}}
    path.write_text(json.dumps(manifest))
    selected = manager.patch_plugin(first, 'custom', settings={'api_key': 'original-key'}, enabled=True, actor='test')
    now = datetime.utcnow()
    original = store.accept_outbound(selected.active, dict(id='accepted-before-rotation', to_number='+15551230001',
        file_name='document.pdf', tiff_path='', status='queued', created_at=now, updated_at=now))
    manifest['actions']['send_fax']['url'] = endpoint + '/replacement'
    path.write_text(json.dumps(manifest))
    replacement = manager.patch_plugin(selected, 'custom', settings={'api_key': 'replacement-key'}, actor='test')
    for profile in (store.outbound_profile('accepted-before-rotation'), store.read_profile(replacement.active.profile_id('outbound'))):
        configuration = profile.configuration
        runtime = HttpProviderRuntime(HttpManifest.from_dict(configuration.manifest), configuration.credentials, configuration.settings)
        response = await runtime.send_fax(to='+15551230001', file_url='https://document.invalid/original.pdf')
        assert response['job_id'] == 'provider-job'
    assert [(path, key) for path, key, _ in requests] == [('/original', 'original-key'), ('/replacement', 'replacement-key')]
    assert store.outbound_profile('accepted-before-rotation') == original
