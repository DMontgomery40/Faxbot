"""faxbot forms: import, list, show, render and send registered forms from the command line."""
import base64
import io
import json

from api.tests.forms_fixtures import SVG, POSITIONS
from api.tests.test_cli import Cli, cli, server  # noqa: F401 - fixtures


def files(tmp_path):
    template, positions = tmp_path / 'referral.svg', tmp_path / 'positions.json'
    template.write_bytes(SVG)
    positions.write_text(json.dumps(POSITIONS), encoding='utf-8')
    return template, positions


def signature_png(path):
    from PIL import Image, ImageDraw
    image = Image.new('RGB', (200, 60), 'white')
    ImageDraw.Draw(image).line((10, 40, 190, 20), fill='black', width=4)
    image.save(path)
    return path


def test_import_list_show_render_and_send_a_form(cli, tmp_path):
    template, positions = files(tmp_path)
    imported = cli('forms', 'import', template, '--name', 'Referral', '--positions', positions)
    assert imported.exit_code == 0, imported.stdout
    assert imported.stdout.strip() == 'Imported Referral version 1. 1 page, 7 fields.'
    again = cli('forms', 'import', template, '--name', 'Copy', '--positions', positions)
    assert 'already registered as Referral version 1' in again.stdout
    assert cli('forms', 'import', template).exit_code != 0
    listed = cli('forms', 'list')
    assert 'Referral' in listed.stdout and 'Imported from an SVG drawing' in listed.stdout
    shown = cli('forms', 'show', 'referral')
    assert shown.exit_code == 0
    assert 'Patient name' in shown.stdout and 'North, South' in shown.stdout and 'MM/DD/YYYY' in shown.stdout
    values = ['--value', 'patient=Ann Example', '--value', 'born=1980-02-29', '--value', 'urgent=yes',
              '--value', 'clinic=North', '--signature', f"signature={signature_png(tmp_path / 'sig.png')}"]
    output = tmp_path / 'filled.pdf'
    rendered = cli.json('forms', 'render', 'Referral', *values, '--output', output)
    assert output.read_bytes().startswith(b'%PDF') and rendered['pages'] == 1 and len(rendered['page_hashes'][0]) == 64
    again = cli.json('forms', 'render', 'Referral', *values, '--output', tmp_path / 'again.pdf')
    assert again['page_hashes'] == rendered['page_hashes']
    picture = cli('forms', 'render', 'Referral', *values, '--png', '--output', tmp_path / 'filled.png')
    assert picture.stdout.strip().endswith('Nothing was sent.')
    assert (tmp_path / 'filled.png').read_bytes().startswith(b'\x89PNG')
    missing = cli('forms', 'render', 'Referral', '--output', tmp_path / 'x.pdf')
    assert missing.exit_code != 0 and 'Patient name is required.' in (missing.stdout + missing.stderr)
    sent = cli.json('forms', 'send', 'Referral', '+15551230009', *values)
    assert sent['route'] == 'fax' and sent['state'] == 'faxed' and sent['fax_id']
    human = cli('forms', 'send', 'Referral', '+15551230009', *values)
    assert 'Sent as a fax (fax ID' in human.stdout
    (first, second) = cli.json('forms', 'sent')
    assert first['form'] == 'Referral' and first['state'] == 'faxed'
    assert 'as a fax' in cli('forms', 'sent').stdout
    assert cli('forms', 'received').stdout.strip() == 'No forms received from partners yet.'


def test_a_new_version_leaves_the_first_one_as_it_was(cli, tmp_path):
    template, positions = files(tmp_path)
    cli('forms', 'import', template, '--name', 'Referral', '--positions', positions)
    changed = tmp_path / 'changed.svg'
    changed.write_bytes(SVG.replace(b'Synthetic referral form', b'Synthetic referral form, revised'))
    result = cli('forms', 'import', changed, '--form', 'Referral', '--positions', positions)
    assert result.stdout.strip().startswith('Imported Referral version 2.')
    (form,) = cli.json('forms', 'list')
    assert [version['number'] for version in form['versions']] == [1, 2]
    assert cli.json('forms', 'show', 'Referral', '--version', '1')['number'] == 1
    assert 'has no version 3' in cli('forms', 'show', 'Referral', '--version', '3').stdout + \
        cli('forms', 'show', 'Referral', '--version', '3').stderr
