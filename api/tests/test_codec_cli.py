"""Received-original downloads choose a local suffix without changing document bytes."""
import httpx
import pytest
from typer.testing import CliRunner

from app.cli.main import app


@pytest.fixture
def download(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('FAXBOT_CLI_CONFIG', str(tmp_path / 'absent-config.toml'))
    monkeypatch.delenv('FAXBOT_PROFILE', raising=False)

    def invoke(content_type, data, *args):
        requests = []

        def handle(request):
            requests.append((request.method, request.url.path))
            headers = {'Content-Disposition': 'attachment; filename="../untrusted.pdf"'}
            if content_type is not None:
                headers['Content-Type'] = content_type
            return httpx.Response(200, content=data, headers=headers)

        with httpx.Client(transport=httpx.MockTransport(handle), base_url='https://faxbot.example') as client:
            result = CliRunner().invoke(app, [
                '--url', 'https://faxbot.example', '--key', 'synthetic',
                'received', 'decoded', 'fax-id', *args,
            ], obj={'client_factory': lambda *_: (client, False)})
        assert requests == [('GET', '/codec/received/fax-id/document')]
        return result

    return invoke


@pytest.mark.parametrize(('content_type', 'extension', 'data'), [
    ('text/plain', '.txt', b'Synthetic original\r\n'),
    ('text/plain; charset=utf-8', '.txt', 'Synthetic caf\u00e9\r\n'.encode()),
    (' Text/Plain ; charset=UTF-8', '.txt', b'Synthetic text\x00\r\n'),
    ('application/pdf; version=1.7', '.pdf', b'%PDF-1.7\n\x00\xff'),
    (None, '.pdf', b'%PDF-1.7\n'),
    ('application/unknown', '.pdf', b'Unknown original bytes\x00'),
])
def test_received_decoded_default_suffix_uses_mime_and_ignores_server_filename(download, tmp_path,
                                                                             content_type, extension, data):
    result = download(content_type, data)
    assert result.exit_code == 0, result.output
    expected = tmp_path / ('decoded_fax-id' + extension)
    assert expected.is_file(), result.output
    assert expected.read_bytes() == data
    assert list(tmp_path.iterdir()) == [expected]
    assert not (tmp_path.parent / 'untrusted.pdf').exists()


def test_received_decoded_keeps_an_explicit_output_name(download, tmp_path):
    data = b'Synthetic text\r\n'
    result = download('text/plain; charset=utf-8', data, '--output', 'chosen.original')
    assert result.exit_code == 0, result.output
    assert (tmp_path / 'chosen.original').read_bytes() == data
    assert list(tmp_path.iterdir()) == [tmp_path / 'chosen.original']


@pytest.mark.parametrize(('content_type', 'data'), [
    ('text/plain; charset=utf-8', 'Synthetic caf\u00e9\r\n'.encode()),
    ('application/pdf', b'%PDF-1.7\n\x00\xff'),
])
def test_received_decoded_stdout_preserves_bytes_without_a_file(download, tmp_path, content_type, data):
    result = download(content_type, data, '--output', '-')
    assert result.exit_code == 0, result.output
    assert result.stdout_bytes == data
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(('content_type', 'extension'), [('text/plain', '.txt'), ('application/pdf', '.pdf')])
def test_received_decoded_requires_force_to_replace_the_default_file(download, tmp_path,
                                                                    content_type, extension):
    target = tmp_path / ('decoded_fax-id' + extension)
    target.write_bytes(b'Existing original')
    result = download(content_type, b'Replacement original')
    assert result.exit_code != 0
    assert 'already exists' in result.output
    assert target.read_bytes() == b'Existing original'
    replaced = download(content_type, b'Replacement original', '--force')
    assert replaced.exit_code == 0, replaced.output
    assert target.read_bytes() == b'Replacement original'
