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


# What receivers do to a page before anyone decodes it (api/tests/codec_receipts.py), and what the Python reader
# (api/app/codec/pages.py) makes of it: 'decodes', or the sentence it refuses with.
RECEIPT_TRANSFORMS = {
    'runs': ('rotated_180', 'inverted', 'rotated_inverted', 'moved_right', 'moved_left', 'padded_to_letter',
             'centred_on_letter', 'trailing_row_removed', 'trailing_row_added'),
    'capacity': ('rotated_180', 'inverted', 'moved_right', 'moved_left', 'padded_to_letter', 'centred_on_letter',
                 'trailing_row_removed'),
    'grid': ('rotated_180', 'inverted', 'centred_on_letter'),
}


def write_receipts(folder, document, salt, nonce):
    """Receipt fixtures: one encoded page per layout as a receiver changed it, plus a resized page of an exact layout
    and a small preview, which are refused, and a page kept as Faxbeep publishes it (a PDF of CCITT images, its last
    white row removed). Returns {file: 'decodes', 'resized' or 'preview'} for decoder.test.mjs and test_codec_decoder."""
    from app.codec import pages
    from tests import codec_receipts as receipts
    packed = container.pack(document, compression=container.DEFLATE, salt=salt, nonce=nonce)
    cases = []
    for layout, names in RECEIPT_TRANSFORMS.items():
        encoded = pages.encode(packed, layout=layout, fec='medium', profile=1 if layout == 'capacity' else None)
        for name in names:
            changed = [receipts.PAGE_TRANSFORMS[name](page) for page in encoded.pages]
            target = f'receipt-{layout}-{name}.tiff'
            (folder / target).write_bytes(receipts.tiff_bytes(changed))
            cases.append({'file': target, 'expect': 'decodes'})
        if layout == 'runs':
            resized = [receipts.resampled(page, 200, 200) for page in encoded.pages]
            (folder / 'receipt-runs-resized.tiff').write_bytes(receipts.tiff_bytes(resized))
            cases.append({'file': 'receipt-runs-resized.tiff', 'expect': 'resized'})
            preview = [receipts.thumbnail(page).convert('L').point(lambda v: 255 if v >= 128 else 0).convert('1')
                       for page in encoded.pages]
            (folder / 'receipt-runs-preview.tiff').write_bytes(receipts.tiff_bytes(preview))
            cases.append({'file': 'receipt-runs-preview.tiff', 'expect': 'preview'})
            (folder / 'receipt-faxbeep.pdf').write_bytes(receipts.faxbeep_pdf(list(encoded.pages)))
            cases.append({'file': 'receipt-faxbeep.pdf', 'expect': 'decodes'})
            # The receiver cut 3 dots off the left edge (a ladder that starts left of where it was drawn).
            (folder / 'receipt-cut.tiff').write_bytes(receipts.tiff_bytes([receipts.moved(page, -3)
                                                                           for page in encoded.pages]))
            cases.append({'file': 'receipt-cut.tiff', 'expect': 'decodes'})
    sentences = {'decodes': 'decodes', 'resized': pages.RESIZED, 'preview': pages.PREVIEW}
    for case in cases:
        # The fixture must say what the Python reader says, so the browser is tested against the reference.
        images = codec.read_images(folder / case['file'])
        try:
            decoded = pages.decode(images)
            outcome = 'decodes' if decoded is not None else 'nothing'
        except pages.PageError as error:
            outcome = str(error)
        if outcome != sentences[case['expect']]:
            raise SystemExit(f"{case['file']}: the Python reader says {outcome!r}, expected {case['expect']!r}")
    return {case['file']: case['expect'] for case in cases}


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
