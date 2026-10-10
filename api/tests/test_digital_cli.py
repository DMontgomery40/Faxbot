"""faxbot delivery providers digital and faxbot recipients digital: Direct and FHIR on the command line."""
from api.app.digital import certificates
from api.tests.digital_fixtures import SENDER, hisp_settings, pem_cert, pem_key, pki
from api.tests.test_cli import Cli, _serve, cli, server  # noqa: F401 - fixtures


def test_a_hisp_account_is_added_with_secrets_from_files_and_never_shown(cli, tmp_path):
    settings, credentials = hisp_settings()
    certificate = tmp_path / 'clinic-cert.pem'
    certificate.write_text(pem_cert(pki().sender, pki().intermediate))
    private_key = tmp_path / 'clinic-key.pem'
    private_key.write_text(pem_key(pki().sender_key))
    arguments = ['providers', 'digital', 'add', '--kind', 'hisp', '--key', 'hisp', '--label', 'Synthetic HISP',
                 '--file', f'certificate={certificate}', '--file', f'private_key={private_key}',
                 '--secrets-from-stdin']
    for name in ('direct_address', 'smtp_host', 'imap_host'):
        arguments += ['--setting', f'{name}={settings[name]}']
    refused = cli('providers', 'digital', 'add', '--kind', 'hisp', '--key', 'hisp', '--setting', 'password=x')
    assert refused.exit_code != 0 and 'is a secret' in (refused.stdout + refused.stderr)
    added = cli(*arguments, input=f"password={credentials['password']}\n")
    assert added.exit_code == 0, added.stdout + added.stderr
    assert 'Synthetic HISP added.' in added.stdout and 'trust bundle' in added.stdout
    bundle = tmp_path / 'bundle.pem'
    bundle.write_bytes(certificates.pem([pki().anchor]))
    loaded = cli('providers', 'digital', 'trust-bundle', 'hisp', '--file', str(bundle))
    assert loaded.exit_code == 0 and 'Trust bundle loaded: 1 authorities. Ready to send.' in loaded.stdout
    listed = cli('providers', 'digital', 'list')
    assert 'Synthetic HISP' in listed.stdout and 'Ready' in listed.stdout
    shown = cli('providers', 'digital', 'show', 'hisp')
    assert SENDER in shown.stdout and 'Set' in shown.stdout
    assert credentials['password'] not in shown.stdout and 'PRIVATE KEY' not in shown.stdout
    off = cli('providers', 'digital', 'update', 'hisp', '--off')
    assert off.exit_code == 0 and 'It is off' in off.stdout


def test_a_fhir_client_makes_its_key_and_prints_its_public_key_set(cli):
    added = cli('providers', 'digital', 'add', '--kind', 'fhir', '--key', 'fhir-hospital', '--setting',
                'client_id=faxbot-county-clinic')
    assert added.exit_code == 0, added.stdout + added.stderr
    made = cli('providers', 'digital', 'signing-key', 'fhir-hospital', '--algorithm', 'ES384')
    assert made.exit_code == 0 and '/digital/jwks/fhir-hospital' in made.stdout
    keys = cli.json('providers', 'digital', 'public-keys', 'fhir-hospital')
    assert keys['keys'][0]['kty'] == 'EC' and 'd' not in keys['keys'][0]


def test_a_recipients_address_is_added_confirmed_and_withdrawn(cli):
    added = cli('recipients', 'digital', 'add', '+13035550142', '--direct', 'records@direct.hospital.example.net')
    assert added.exit_code == 0, added.stdout + added.stderr
    assert 'Suggested' in added.stdout and 'go only by fax' in added.stdout
    confirmed = cli('recipients', 'digital', 'confirm', '+13035550142', 'records@direct.hospital.example.net')
    assert 'Confirmed' in confirmed.stdout and 'may go as Direct message to' in confirmed.stdout
    withdrawn = cli('recipients', 'digital', 'withdraw', '+13035550142', 'records@direct.hospital.example.net',
                    '--note', 'They left the network.')
    assert 'Withdrawn' in withdrawn.stdout
    both = cli('recipients', 'digital', 'add', '+13035550142', '--direct', 'a@b.example', '--fhir', 'https://x.example')
    assert both.exit_code != 0 and 'either --direct or --fhir' in (both.stdout + both.stderr)
    messages = cli('recipients', 'digital', 'messages', '--sent')
    assert 'No Direct messages or FHIR documents yet.' in messages.stdout
    one = cli('recipients', 'digital', 'messages', '--fax', 'a' * 32)
    assert one.exit_code == 0, one.stdout + one.stderr
    assert 'This fax went as no Direct message or FHIR document.' in one.stdout
