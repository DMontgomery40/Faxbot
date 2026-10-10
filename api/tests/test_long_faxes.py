"""Long faxes: pages are kept packed while each attempt chooses how to send them, and a fax longer than the ceiling
goes exactly as it is, with one sentence in Sent saying why.

Pillow holds a mode "1" page at one byte a pixel (about 3.9 MB for Letter at fine resolution), so holding every page
of a long fax as images took gigabytes once documents of up to 500 pages were accepted (10 October 2026: 1,521 MB
peak for 100 pages). The peak-memory test runs one attempt's page preparation in a fresh process.
"""
from pathlib import Path
import json
import subprocess
import sys

from PIL import Image, ImageDraw
import pytest

from api.tests.test_dense_pages import JOB, PEER, _send, installation  # noqa: F401 - fixture
from api.tests.test_schema import database  # noqa: F401 - fixture
from app import conversion
from app.pages import views

API = Path(__file__).resolve().parents[1]


def _page(number, height=2156):
    image = Image.new('1', (1728, height), 1)
    draw = ImageDraw.Draw(image)
    for row in range(40):
        draw.text((80, 80 + row * 48), f'Synthetic record page {number}, line {row + 1}', fill=0)
    image.info['dpi'] = (204.0, 196.0)
    return image


def test_fax_frames_keep_pages_packed_and_make_each_page_as_it_was():
    pages = [_page(number) for number in range(3)]
    frames = conversion.FaxFrames(pages)
    assert len(frames) == 3 and frames.sizes() == [(1728, 2156)] * 3
    assert all(frames[i].tobytes() == pages[i].tobytes() and frames[i].info['dpi'] == (204.0, 196.0)
               for i in range(3))
    assert len(frames.packed(0)) == 1728 // 8 * 2156  # one bit a pixel, not one byte
    frames[1] = pages[2]
    assert frames[1].tobytes() == pages[2].tobytes() and len(frames[1:]) == 2
    changed = frames[0]
    changed.paste(0, (0, 0, 100, 100))
    assert frames[0].tobytes() == pages[0].tobytes()  # a page made from the store never changes the store
    with pytest.raises(ValueError):
        frames.append(pages[0].convert('L'))


PREPARE = r'''
import json, resource, sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, sys.argv[1])
from app import conversion, schema
from app.pages import capability, sending
folder, pdf, tiff = Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4])
engine = schema.create_database_engine('sqlite:///' + str(folder / 'schema.db'))
schema.upgrade_schema(engine)
# A machine that takes pages of unlimited length, so dense pages are drawn, measured and priced.
capability.records_for(engine).record_observation('+12025550123', source='d' * 32, engine='hylafax',
    values={'max_length': 'unlimited', 'ecm': 1, 'fine': 1}, now=datetime(2026, 10, 10))
before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
prepared = sending.prepare(engine, SimpleNamespace(sip_fax_fine=True, fax_friendly_documents='never'),
                           SimpleNamespace(provider_id='sip', manifest=None, traits={'requires_tiff': True}),
                           SimpleNamespace(job_id='a' * 32, attempt_id='b' * 32, members=()),
                           {'to_number': '+12025550123'}, pdf, tiff)
peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
scale = 1 if sys.platform == 'darwin' else 1024
print(json.dumps({'before': before * scale, 'peak': peak * scale,
                  'sent_pages': prepared.sent_pages if prepared else None}))
'''


@pytest.mark.skipif(sys.platform not in ('darwin', 'linux'), reason='peak memory is read from getrusage')
def test_a_100_page_fax_is_prepared_within_a_bounded_peak_memory(tmp_path):
    """One attempt's page preparation for 100 Letter pages, packed onto long pages, in a fresh process. Measured on
    10 October 2026 (macOS, the repository's virtual environment): it grew by 1,206 MB before the pages were kept
    packed and by 242 MB after. Holding the 100 pages as images again would alone add about 390 MB."""
    frames = conversion.FaxFrames(_page(number) for number in range(100))
    tiff, pdf = tmp_path / f'{"a" * 32}.tiff', tmp_path / f'{"a" * 32}.pdf'
    conversion.write_fax_tiff(frames, str(tiff))
    conversion.tiff_to_pdf(str(tiff), str(pdf))
    script = tmp_path / 'prepare.py'
    script.write_text(PREPARE)
    result = subprocess.run([sys.executable, str(script), str(API), str(tmp_path), str(pdf), str(tiff)],
                            capture_output=True, text=True, timeout=600, check=True)
    found = json.loads(result.stdout.strip().splitlines()[-1])
    assert found['sent_pages'] is not None and found['sent_pages'] < 100, found
    grown = found['peak'] - found['before']
    print(f'100-page preparation: peak {found["peak"] / 1e6:.0f} MB, grew {grown / 1e6:.0f} MB')
    assert grown < 400_000_000, found


def test_a_fax_longer_than_the_ceiling_goes_as_it_is_and_sent_says_why(installation, database, tmp_path,  # noqa: F811
                                                                       monkeypatch):
    monkeypatch.setattr(conversion, 'MAX_OPTIMIZED_PAGES', 4)
    installation.record_observation(PEER, source='d' * 32, engine='hylafax', values={
        'max_length': 'unlimited', 'ecm': 1, 'fine': 1}, now=None)
    assert _send(database, tmp_path, pages=[_page(number, height=600) for number in range(5)]) is None
    view = views.sent_view(database, JOB, str(tmp_path))
    assert view['sentences'] == [conversion.too_long_sentence(5)]
    assert view['sentences'][0] == ('The pages went as they are, because Faxbot changes pages only on faxes of up '
                                    'to 4 pages (this one has 5).')
    assert view['pages_saved'] == 0 and view['layout'] == 'normal'
    # A fax at the ceiling is still changed (packed here).
    monkeypatch.setattr(conversion, 'MAX_OPTIMIZED_PAGES', 5)
    changed = _send(database, tmp_path, pages=[_page(number, height=600) for number in range(5)],
                    attempt='c' * 32)
    assert changed is not None and changed.sent_pages < 5


def test_the_shading_drawing_is_read_one_page_at_a_time(tmp_path):
    from app.pages import friendly
    pages = [_page(number, height=300).convert('L') for number in range(3)]
    gray = tmp_path / 'gray.tiff'
    pages[0].save(gray, 'TIFF', compression='tiff_lzw', save_all=True, append_images=pages[1:])
    today = conversion.FaxFrames(page.convert('1') for page in pages)
    found = friendly._gray_pages(gray, today)
    assert isinstance(found, friendly.GrayPages) and len(found) == 3
    assert [page.tobytes() for page in found] == [page.tobytes() for page in pages]
    assert friendly._gray_pages(gray, conversion.FaxFrames(page.convert('1') for page in pages[:2])) is None
