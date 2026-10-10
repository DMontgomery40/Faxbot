"""Write the browser decoder's test fixtures from the Python encoder: python tools/fax-decoder/test/make_fixtures.py [folder].

Synthetic documents only. The same script runs in api/tests/test_codec_decoder.py, which checks that the
committed fixtures are exactly what the encoder makes today. The zstd fixture needs the zstandard package.
"""
import hashlib
import json
from pathlib import Path
import random
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'api'))

from app import codec  # noqa: E402
from app.codec import container, rs  # noqa: E402
from app.conversion import tiff_to_pdf  # noqa: E402

TEXT = ('Synthetic referral note for the Faxbot payload decoder test.\n'
        + ''.join(f'Line {index}: the quick brown fox faxes the lazy dog a dense page.\n' for index in range(40)))
SECRET = 'synthetic shared key'


def _document():
    return codec.Document(TEXT.encode(), 'text/plain', 'referral-note.txt')


def _rs_vectors():
    rng = random.Random(42)
    vectors = []
    for parity in (16, 32, 64):
        message = [rng.randrange(256) for _ in range(255 - parity)]
        word = rs.encode_message(message, parity)
        damaged = list(word)
        positions = rng.sample(range(255), parity // 2)
        for position in positions:
            damaged[position] ^= rng.randrange(1, 256)
        erasures = positions[: parity // 4]
        vectors.append({'parity': parity, 'codeword': word, 'damaged': damaged, 'erasures': erasures})
    return vectors


def make(folder):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    document = _document()
    salt, nonce = b'\x01' * 16, b'\x02' * 12
    cases = {
        'grid.tiff': dict(layout='grid', compression=container.DEFLATE),
        'runs.pdf': dict(layout='runs', compression=container.DEFLATE),
        'picture.tiff': dict(layout='picture', compression=container.DEFLATE),
        'encrypted.tiff': dict(layout='grid', compression=container.DEFLATE, secret=SECRET),
        'capacity.tiff': dict(layout='capacity', compression=container.DEFLATE, profile=1),
        'capacity-pages.pdf': dict(layout='capacity', compression=container.DEFLATE, profile=3),
    }
    if container.zstd_available():
        cases['zstd.tiff'] = dict(layout='grid', compression=container.ZSTD)
    expected = {'sha256': hashlib.sha256(document.data).hexdigest(), 'name': document.name, 'secret': SECRET,
                'files': sorted(cases), 'rs': _rs_vectors()}
    for name, options in cases.items():
        packed = container.pack(document, compression=options['compression'], secret=options.get('secret'),
                                salt=salt, nonce=nonce)
        from app.codec import pages
        encoded = pages.encode(packed, layout=options['layout'], fec='medium', profile=options.get('profile'))
        tiff = folder / (name.rsplit('.', 1)[0] + '.tiff')
        codec.write_tiff(encoded.pages, tiff)
        if name.endswith('.pdf'):
            tiff_to_pdf(str(tiff), str(folder / name))
            tiff.unlink()
    expected['receipts'] = write_receipts(folder, document, salt, nonce)
    (folder / 'expected.json').write_text(json.dumps(expected, indent=1) + '\n')
    write_capacity_tables()
    return folder


def write_receipts(folder, document, salt, nonce):
    """Encoded pages as real receivers keep them (api/tests/codec_receipts.py), as TIFF or PDF (the browser decoder
    reads PNG and JPEG only in a browser): {file: 'decodes', 'resized' or 'preview'}."""
    sys.path.insert(0, str(ROOT / 'api'))
    from tests import codec_receipts as receipts
    from app.codec import pages
    packed = container.pack(document, compression=container.DEFLATE, salt=salt, nonce=nonce)
    runs = list(pages.encode(packed, layout='runs', fec='medium').pages)
    capacity = list(pages.encode(packed, layout='capacity', fec='medium').pages)
    files = {
        'receipt-faxbeep.pdf': (receipts.faxbeep_pdf(runs), 'decodes'),
        'receipt-rotated.tiff': (receipts.tiff_bytes([receipts.rotated(page) for page in runs]), 'decodes'),
        'receipt-inverted.tiff': (receipts.tiff_bytes([receipts.inverted(page) for page in capacity]), 'decodes'),
        'receipt-moved.tiff': (receipts.tiff_bytes([receipts.moved(page, 3) for page in capacity]), 'decodes'),
        'receipt-padded.tiff': (receipts.tiff_bytes([receipts.centred(page) for page in runs]), 'decodes'),
        'receipt-resized.tiff': (receipts.tiff_bytes([receipts.resampled(page, 200, 200) for page in runs]),
                                 'resized'),
        'receipt-preview.tiff': (receipts.tiff_bytes([receipts.thumbnail(runs[0]).convert('1')]), 'preview'),
    }
    for name, (data, _) in files.items():
        (folder / name).write_bytes(data)
    return {name: outcome for name, (_, outcome) in files.items()}


def write_capacity_tables(target=Path(__file__).resolve().parents[1] / 'capacity-tables.js'):
    """The capacity layout's frozen tables for the browser, the exact bytes of api/app/codec/capacity_tables.json."""
    from app.codec import capacity
    text = capacity.TABLES_PATH.read_text().strip()
    target.write_text('// Generated by test/make_fixtures.py from api/app/codec/capacity_tables.json; never edit.\n'
                      f'// SHA-256 of that file: {capacity.TABLES_SHA256}\n'
                      f'export const CAPACITY_TABLES = JSON.parse({json.dumps(text)}).profiles;\n')
    return target


if __name__ == '__main__':
    print(make(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).parent / 'fixtures'))
