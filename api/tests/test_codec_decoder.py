"""The browser decoder (tools/fax-decoder) reads what the Python encoder writes."""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess

import pytest
from PIL import ImageFont

from app import codec
from app.codec import container, pages

TOOL = Path(__file__).resolve().parents[2] / 'tools' / 'fax-decoder'


def _make(folder):
    spec = importlib.util.spec_from_file_location('make_fixtures', TOOL / 'test' / 'make_fixtures.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.make(folder)


@pytest.mark.parametrize('raqm_available', [False, True], ids=['basic-fonts', 'raqm-installed'])
def test_the_committed_fixtures_are_what_the_encoder_makes_today(tmp_path, monkeypatch, raqm_available):
    if raqm_available and not ImageFont.core.HAVE_RAQM:
        pytest.skip('Optional RAQM text shaping is not installed')
    # Pillow otherwise changes its default font layout when this optional library is present.
    monkeypatch.setattr(ImageFont.core, 'HAVE_RAQM', raqm_available)
    made = _make(tmp_path)
    committed = TOOL / 'test' / 'fixtures'
    expected = json.loads((committed / 'expected.json').read_text())
    for name in expected['files']:
        if name == 'zstd.tiff' and not container.zstd_available():
            continue
        fresh_images = codec.read_images(made / name)
        document, _ = codec.decode_images(fresh_images, secrets=[expected['secret']])
        assert document.sha256 == expected['sha256'], name
        assert document.name == expected['name'], name
        assert document.content_type == 'text/plain', name
        if name.endswith('.pdf'):
            # A PDF carries its creation time; its fax images must be identical.
            images = [image.convert('1').tobytes() for image in fresh_images]
            assert images == [image.convert('1').tobytes() for image in codec.read_images(committed / name)], name
        else:
            assert (made / name).read_bytes() == (committed / name).read_bytes(), name
    # Pages as receivers keep them (api/tests/codec_receipts.py): still what the encoder makes, and the Python reader
    # gives each the outcome the browser decoder's test expects, so the two readers agree on every one.
    refusals = {'resized': pages.RESIZED, 'preview': pages.PREVIEW}
    for name, outcome in expected['receipts'].items():
        fresh_images = codec.read_images(made / name)
        assert [image.convert('1').tobytes() for image in fresh_images] == [
            image.convert('1').tobytes() for image in codec.read_images(committed / name)], name
        if outcome == 'decodes':
            document, _ = codec.decode_images(fresh_images, secrets=[expected['secret']])
            assert document.sha256 == expected['sha256'], name
        else:
            with pytest.raises(codec.CodecError) as refused:
                codec.decode_images(fresh_images, secrets=[expected['secret']])
            assert str(refused.value) == refusals[outcome], name


@pytest.mark.skipif(shutil.which('node') is None, reason='Node.js is not installed')
def test_the_browser_decoder_decodes_every_fixture():
    result = subprocess.run(['node', '--test', str(TOOL / 'test' / 'decoder.test.mjs')], capture_output=True,
                            text=True, timeout=300)
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-2000:]


def test_the_browser_decoder_carries_the_frozen_capacity_tables_byte_for_byte():
    """The capacity layout's tables are data, never recomputed: the browser module embeds the exact JSON the Python
    codec reads, pinned by its SHA-256."""
    import hashlib
    from app.codec import capacity
    module = (TOOL / 'capacity-tables.js').read_text()
    assert capacity.TABLES_SHA256 in module
    embedded = json.loads(module.split('JSON.parse(', 1)[1].rsplit(').profiles', 1)[0])
    assert (embedded + '\n').encode() == capacity.TABLES_PATH.read_bytes()
    assert hashlib.sha256(capacity.TABLES_PATH.read_bytes()).hexdigest() == capacity.TABLES_SHA256


def test_the_browser_decoder_reads_exact_rows_at_the_widths_the_encoder_draws():
    """decoder.js EXACT_WIDTHS is pages._exact_widths(): the page width each exact layout's rows were drawn for, by
    ladder columns, and where the ladder starts on a page nobody moved."""
    import re
    from app.codec import pages
    module = (TOOL / 'decoder.js').read_text()
    table = re.search(r'const EXACT_WIDTHS = \{([^}]*)\};', module).group(1)
    embedded = {int(columns): (int(width), int(first))
                for columns, width, first in re.findall(r'(\d+): \[(\d+), (\d+)\]', table)}
    assert embedded == pages._exact_widths()
