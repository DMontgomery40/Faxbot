"""Measure payload page capacity and line time: ``python -m app.codec.measure [--quick]``.

For each resolution and layout this encodes a two-page random payload (the
worst case: compressed or encrypted data looks random), takes the full first
page and reports:

- container bytes one page carries at each error-correction level;
- the page's coded size under MH, MR and MMR (``t4``), and JBIG (T.85) when
  ``pbmtojbg85`` from jbigkit is on PATH or named by FAXBOT_JBIG_TOOL;
- the seconds those bits take at 9,600 and 14,400 bit/s. Line time only: the
  T.30 handshake and the per-page exchange (a few seconds each) come on top.

Every layout's page is the same whatever the error-correction level (the data
is random either way); only how much of it is document changes.
"""
import argparse
import json
import os
import shutil
import subprocess
import tempfile

from . import capacity, pages, stream as streams
from .channel import lines_of
from . import t4

RATES = (9600, 14400)


def _jbig_bits(image):
    tool = os.environ.get('FAXBOT_JBIG_TOOL') or shutil.which('pbmtojbg85')
    if not tool:
        return None
    with tempfile.TemporaryDirectory() as folder:
        source, target = os.path.join(folder, 'page.pbm'), os.path.join(folder, 'page.jbg')
        image.convert('1').save(source)
        subprocess.run([tool, source, target], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return os.path.getsize(target) * 8


def _variants(quick):
    resolutions = ('fine',) if quick else tuple(pages.RESOLUTIONS)
    for name in resolutions:
        yield name, 'grid', {}
        if name in ('fine', 'standard'):
            yield name, 'grid (sturdy)', {'sturdy': True}
        for limit in ((15,) if quick else (7, 15, 63)):
            yield name, f'runs (limit {limit})', {'run_limit': limit}
        for profile, (label, _) in capacity.PROFILES.items():
            yield name, f'capacity ({label})', {'profile': profile}
        yield name, 'picture', {}


def measure(quick=False):
    rows = []
    for resolution, label, options in _variants(quick):
        layout = label.split(' ')[0]
        geo = pages.geometry(resolution, layout, sturdy=options.get('sturdy', False),
                             run_limit=options.get('run_limit', pages.DEFAULT_RUN_LIMIT))
        if layout in ('runs', 'capacity'):
            probe = os.urandom(int(geo.max_data_lines * geo.resolution.width * (0.25 if layout == 'runs' else 0.12)))
        else:
            probe = os.urandom(int(geo.row_bytes * geo.rows_per_page * 1.2))
        encoded = pages.encode(probe, resolution=resolution, layout=layout, fec=0 or 'low', **options)
        page = encoded.pages[0]
        stream_bytes = encoded.page_bits[0] // 8
        width = page.size[0]
        lines = lines_of(page)
        k = 2 if geo.resolution.ydpi < 150 else 4
        coded = {scheme: t4.coded_bits(lines, width, scheme, k) for scheme in ('MH', 'MR', 'MMR')}
        jbig = _jbig_bits(page)
        if jbig is not None:
            coded['JBIG'] = jbig
        capacity = {level: stream_bytes * (255 - parity) // 255 for level, parity in streams.FEC_LEVELS.items()}
        rows.append({
            'resolution': resolution, 'layout': label, 'page_lines': page.size[1], 'stream_bytes': stream_bytes,
            'capacity_bytes': capacity, 'coded_bits': coded,
            'seconds': {scheme: {rate: round(bits / rate, 1) for rate in RATES} for scheme, bits in coded.items()},
        })
    return rows


def table(rows):
    out = ['| Resolution | Layout | Bytes/page low | medium | high | Coded kbit MH / MR / MMR / JBIG '
           '| s @9600 (MH) | s @14400 (MH) | s @14400 (MMR) |',
           '| --- | --- | ---: | ---: | ---: | --- | ---: | ---: | ---: |']
    for row in rows:
        coded = row['coded_bits']
        kbit = ' / '.join(str(round(coded[s] / 1000)) if s in coded else '-' for s in ('MH', 'MR', 'MMR', 'JBIG'))
        cap = row['capacity_bytes']
        out.append(f"| {row['resolution']} | {row['layout']} | {cap['low']:,} | {cap['medium']:,} | {cap['high']:,} "
                   f"| {kbit} | {row['seconds']['MH'][9600]} | {row['seconds']['MH'][14400]} "
                   f"| {row['seconds']['MMR'][14400]} |")
    return '\n'.join(out)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--quick', action='store_true', help='fine resolution only')
    parser.add_argument('--json', action='store_true')
    arguments = parser.parse_args()
    rows = measure(arguments.quick)
    print(json.dumps(rows, indent=1) if arguments.json else table(rows))


if __name__ == '__main__':
    main()
