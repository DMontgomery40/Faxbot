"""The pinned form renderer: the same form, values, renderer version and resolution give the same dots.

Byte-identical pages are the promise partners exchange (research M22), so these
tests compare hashes across runs and across processes started with different
hash seeds, and keep a golden hash for a form drawn without Ghostscript, which
CI checks again on Linux.
"""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from api.app.forms import importer, model, renderer
from api.app.forms.raster import Bitmap
from api.tests.forms_fixtures import POSITIONS, SVG, VALUES, positions_json, signature


ROOT = Path(__file__).resolve().parents[2]
# The SVG fixture filled with VALUES, at fine and standard resolution, by renderer faxbot-forms-1.
GOLDEN_FINE = 'a56553f5f20a4907092c51929c8ae29b5bb2baae2a056bc647716aa205706588'
GOLDEN_STANDARD = '0d42dcf07a1cf7ea0f612ad64487c940bb2be4e44e1b2a0db047f63908972fa6'
GOLDEN_ADDRESS = '7d225e0324b7454bce685c3d79e40467f2ef16b56ad494ba50e76c98a7f67145'


@pytest.fixture(scope='module')
def form():
    return importer.from_template(SVG, file_name='referral.svg', positions=positions_json())


def fill(form, values=VALUES, resolution='fine'):
    normalized = model.values(form.content, values, drawable=renderer.missing_characters)
    return renderer.render(form.content, form.backgrounds, normalized, resolution=resolution)


def test_the_font_is_pinned_and_licensed():
    assert hashlib.sha256(renderer.FONT_PATH.read_bytes()).hexdigest() == renderer.FONT_SHA256
    assert (renderer.FONT_PATH.parent / 'LICENSE-DejaVu.txt').read_text().startswith('Fonts are (c) Bitstream')
    assert renderer.RENDERER == 'faxbot-forms-1'


def test_the_golden_form_draws_the_same_pages_on_every_host(form):
    assert form.address == GOLDEN_ADDRESS
    assert fill(form).hashes == (GOLDEN_FINE,)
    assert fill(form, resolution='standard').hashes == (GOLDEN_STANDARD,)


def test_two_runs_draw_identical_pages(form):
    first, second = fill(form), fill(form)
    assert first.hashes == second.hashes
    assert first.pages[0].packed() == second.pages[0].packed()


SCRIPT = '''
import json, sys
sys.path.insert(0, {root!r})
from api.app.forms import importer, model, renderer
from api.tests.forms_fixtures import SVG, VALUES, positions_json
form = importer.from_template(SVG, file_name='referral.svg', positions=positions_json())
values = model.values(form.content, VALUES, drawable=renderer.missing_characters)
print(json.dumps([form.address, list(renderer.render(form.content, form.backgrounds, values).hashes),
                  list(renderer.render(form.content, form.backgrounds, values, resolution='standard').hashes)]))
'''


def test_two_processes_with_different_hash_seeds_draw_identical_pages(form):
    results = []
    for seed in ('1', '2024'):
        environment = {**os.environ, 'PYTHONHASHSEED': seed}
        output = subprocess.run([sys.executable, '-c', SCRIPT.format(root=str(ROOT))], env=environment, cwd=str(ROOT),
                                capture_output=True, text=True, timeout=120, check=True)
        results.append(output.stdout.strip())
    assert results[0] == results[1]
    assert results[0] == f'["{form.address}", ["{fill(form).hashes[0]}"], ["{fill(form, resolution="standard").hashes[0]}"]]'


def test_every_field_type_changes_only_its_own_dots(form):
    base = fill(form, {'patient': 'A'})
    for name, value in (('born', '2026-10-07'), ('urgent', True), ('clinic', 'North'), ('amount', '7'),
                        ('notes', 'Synthetic'), ('signature', signature())):
        drawn = fill(form, {'patient': 'A', name: value})
        assert drawn.hashes != base.hashes, name
        item = next(field for field in form.content['fields'] if field['name'] == name)
        x, y, width, height = item['box']
        for row in range(drawn.pages[0].height):
            if not y <= row < y + height:
                assert drawn.pages[0].rows[row] == base.pages[0].rows[row], (name, row)
            else:
                outside = drawn.pages[0].rows[row][:x] + drawn.pages[0].rows[row][x + width:]
                assert outside == base.pages[0].rows[row][:x] + base.pages[0].rows[row][x + width:], (name, row)


def test_values_are_normalized_before_drawing(form):
    one = fill(form, {**VALUES, 'amount': '1234.50', 'urgent': 'yes'})
    other = fill(form, {**VALUES, 'amount': '1,234.5', 'urgent': True})
    assert one.hashes == other.hashes
    normalized = model.values(form.content, {**VALUES, 'urgent': 'no'}, drawable=renderer.missing_characters)
    assert 'urgent' not in normalized and normalized['amount'] == '1234.50' and normalized['born'] == '1980-02-29'


@pytest.mark.parametrize('values, problem', [
    ({}, 'Patient name is required.'),
    ({'patient': 'A', 'born': '02/29/1980'}, 'Date of birth must be a date written as year-month-day'),
    ({'patient': 'A', 'born': '1981-02-29'}, 'Date of birth must be a date'),
    ({'patient': 'A', 'clinic': 'East'}, 'Clinic must be one of: North, South.'),
    ({'patient': 'A', 'amount': '1.005'}, 'Amount can have at most 2 decimal places.'),
    ({'patient': 'A', 'amount': 'many'}, 'Amount must be a number.'),
    ({'patient': 'Line\nbreak'}, 'Patient name must be a single line.'),
    ({'patient': '日本'}, 'Patient name uses characters the form font cannot print'),
    ({'patient': 'A', 'nickname': 'B'}, 'This form has no field named nickname.'),
    ({'patient': 'A', 'signature': {'width': 2, 'height': 2, 'bits': 'AAAA'}}, 'The signature for Signature'),
])
def test_values_that_do_not_fit_name_the_field(form, values, problem):
    with pytest.raises(model.FormValueError) as raised:
        model.values(form.content, values, drawable=renderer.missing_characters)
    assert any(text.startswith(problem) for text in raised.value.problems), raised.value.problems


def test_standard_resolution_joins_each_pair_of_lines(form):
    fine, standard = fill(form), fill(form, resolution='standard')
    assert standard.pages[0].height == (fine.pages[0].height + 1) // 2
    assert standard.pages[0].rows[10] == bytearray(a | b for a, b in zip(fine.pages[0].rows[20], fine.pages[0].rows[21]))


def test_the_page_hash_names_the_renderer_and_resolution():
    page = Bitmap(model.PAGE_WIDTH, 200)
    page.rectangle(10, 10, 20, 20)
    hashes = {renderer.page_hash(page, renderer=r, resolution=s) for r in ('faxbot-forms-1', 'faxbot-forms-2')
              for s in ('fine', 'standard')}
    assert len(hashes) == 4
    with pytest.raises(renderer.RendererUnavailable):
        renderer.render({'fields': []}, [page], {}, renderer='faxbot-forms-2')


def test_packing_round_trips_and_pads_rows():
    page = Bitmap(13, 3)
    page.span(1, 2, 13)
    packed = page.packed()
    assert packed == bytes([0, 0, 0x3F, 0xF8, 0, 0])
    assert Bitmap.from_packed(13, 3, packed).rows == page.rows


@pytest.mark.skipif(shutil.which('gs') is None, reason='Ghostscript is not installed')
def test_the_pdf_faxed_on_the_trunk_path_carries_the_same_dots(form, tmp_path):
    from PIL import Image
    from api.app.conversion import pdf_to_tiff
    rendered = fill(form)
    pdf = tmp_path / 'form.pdf'
    pdf.write_bytes(renderer.to_pdf(rendered))
    pages, tiff = pdf_to_tiff(str(pdf), str(tmp_path / 'form.tiff'))
    assert pages == 1
    with Image.open(tiff) as image:
        assert image.size == (rendered.pages[0].width, rendered.pages[0].height)
        inverted = bytes(255 - value for value in range(256))
        faxed = image.convert('1').tobytes().translate(inverted)
    assert hashlib.sha256(faxed).hexdigest() == hashlib.sha256(rendered.pages[0].packed()).hexdigest()


def test_positions_place_fields_in_dots_from_the_top_left(form):
    patient = next(field for field in form.content['fields'] if field['name'] == 'patient')
    # 160 pt across a 612 pt page is 160 * 1728 / 612 dots; 100 pt down is 100 * 196 / 72 dots.
    assert patient['box'] == [451, 272, 848, 66]
    assert len(POSITIONS['fields']) == len(form.content['fields'])
