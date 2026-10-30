"""Mailbox detail reads synthetic records without contacting any service."""
import httpx
import pytest
from typer.testing import CliRunner
from app.cli.main import app


@pytest.mark.parametrize('json_output', [True, False])
@pytest.mark.parametrize('available', [True, False])
def test_mailbox_detail_is_scoped_and_retains_unavailable_sources(json_output, available):
    requests = []

    def respond(request):
        requests.append(request.url.path)
        if request.url.path == '/auth/me':
            return httpx.Response(200, json={'permissions': [], 'policy_version': 1})
        if request.url.path == '/access/mailboxes':
            return httpx.Response(200, json={'items': [
                {'id': 'm1', 'label': 'Front desk', 'enabled': True, 'rule_count': 1, 'version': 1},
                {'id': 'm2', 'label': 'Other', 'enabled': True, 'rule_count': 0, 'version': 1}]})
        if request.url.path == '/access/inbound-rules':
            return httpx.Response(200, json={'items': [
                {'id': 'r1', 'to_number': '+15550100001', 'mailbox_id': 'm1'},
                {'id': 'r2', 'to_number': '+15550100002', 'mailbox_id': 'm2'}]})
        if available:
            data = {
                '/work/settings': {'acknowledge_hours': 24, 'mailboxes': [
                    {'mailbox_id': 'm1', 'acknowledge_hours': None, 'backup': {'name': 'Sam'}},
                    {'mailbox_id': 'm2', 'acknowledge_hours': 4, 'backup': None}]},
                '/routing/rules': {'active': None, 'organization': None},
                '/admin/settings': {},
                '/intake/connectors': {'connectors': []},
            }
            return httpx.Response(200, json=data[request.url.path])
        return httpx.Response(403, json={'detail': 'Permission denied'})

    client = httpx.Client(transport=httpx.MockTransport(respond), base_url='https://testserver')
    result = CliRunner().invoke(app, ['--url', 'https://testserver', '--key', 'synthetic', *(['--json'] if json_output else []),
                                    'delivery', 'mailboxes', 'show', 'Front desk'],
                                obj={'client_factory': lambda *_: (client, False)})
    assert result.exit_code == 0, result.output
    if json_output:
        import json
        detail = json.loads(result.stdout)
        assert detail['mailbox']['id'] == 'm1'
        assert [r['id'] for r in detail['numbers']] == ['r1']
        if available:
            assert detail['targets']['installation_wide_hours'] == 24
            assert len(detail['targets']['mailboxes']) == 1
            assert detail['numbers'][0]['email'] == 'Not emailed'
        else:
            assert detail['targets']['unavailable']
            assert detail['sending_rules']['unavailable']
    else:
        assert 'Front desk' in result.stdout and '+15550100001' in result.stdout
        assert '+15550100002' not in result.stdout
        assert ('Installation-wide target' if available else 'targets unavailable') in result.stdout
    assert set(requests) <= {'/auth/me', '/access/mailboxes', '/access/inbound-rules',
                             '/work/settings', '/routing/rules', '/admin/settings', '/intake/connectors'}
