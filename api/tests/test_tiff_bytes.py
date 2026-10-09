"""The same pages always give the same TIFF bytes (``tiff_bytes.settle``).

libtiff skips the pad byte before each directory without writing it, so a fax image could carry 0x00 there one
time and 0x0f another and get two SHA-256 digests for the same pages. Every TIFF Faxbot hashes, signs or compares
goes through ``settle``: these tests dirty every byte the structure does not point at, as an unwritten pad would,
and check that the result is the same bytes with the same pixels. All pages are synthetic.
"""
from datetime import datetime
import hashlib
import io
import random

import pytest

from api.app import tiff_bytes
from api.app.direct import faximage, relay_pages, repair
from api.tests.test_body_reuse import engine_image, pixels


MOMENT = datetime(2026, 10, 7, 20, 5)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def gaps(data):
    """The byte ranges the TIFF's structure does not point at."""
    found, position = [], 0
    for start, end in tiff_bytes.used(data):
        if start > position:
            found.append((position, start))
        position = max(position, end)
    if position < len(data):
        found.append((position, len(data)))
    return found


def dirtied(data, seed):
    """``data`` with every byte no reader looks at set to noise, as libtiff's unwritten pad byte can be."""
    noise, out = random.Random(seed), bytearray(data)
    for start, end in gaps(data):
        for position in range(start, end):
            out[position] = noise.randrange(1, 256)
    return bytes(out)


def raw_pages(data, dpi=(204, 196)):
    """The same pages written by Pillow alone, without settling: the bytes as libtiff leaves them."""
    from PIL import Image
    with Image.open(io.BytesIO(data)) as image:
        pages = []
        for page in range(image.n_frames):
            image.seek(page)
            pages.append(image.copy())
    output = io.BytesIO()
    pages[0].save(output, 'TIFF', compression='group4', save_all=True, append_images=pages[1:], dpi=dpi)
    return output.getvalue()


def test_the_same_pages_and_time_always_give_one_fax_image():
    tiff = engine_image(3, rows=1200)
    built = {sha(faximage.stamp(tiff, header='Valley Hospital', station='+15550100001', moment=MOMENT)[0])
             for _ in range(30)}
    assert len(built) == 1
    data, facts = faximage.stamp(tiff, header='Valley Hospital', station='+15550100001', moment=MOMENT)
    # Pillow's own output has bytes outside the structure: a pad before a directory and appended pages' headers.
    assert gaps(raw_pages(data)) and gaps(data)
    for seed in range(5):
        noisy = dirtied(data, seed)
        assert noisy != data and pixels(noisy) == pixels(data)
        assert tiff_bytes.settle(noisy) == data and tiff_bytes.settle(data) == data
    assert faximage.check(data, facts, 3) == 3 and faximage.layout(data) is not None


def test_a_held_image_with_other_pad_bytes_still_rebuilds_the_senders_exact_image():
    tiff = engine_image(3, rows=1200)
    earlier, _ = faximage.stamp(tiff, header='Valley Hospital', station='+15550100001', moment=MOMENT)
    later, _ = faximage.stamp(tiff, header='Valley Hospital', station='+15550100001', moment=datetime(2026, 10, 8, 9))
    held = dirtied(earlier, 7)  # kept by the partner as some earlier build or libtiff wrote it
    found, kept = faximage.layout(later), faximage.layout(held)
    assert kept.body_sha256 == found.body_sha256 == faximage.layout(earlier).body_sha256
    remaining, holes = faximage.cut(later, found)
    assert faximage.splice(remaining, holes, faximage.body_strips(held, kept), size=len(later)) == later


def test_repair_and_relay_images_are_settled_too():
    tiff = engine_image(3, rows=600)
    image, _ = faximage.stamp(tiff, header='Valley Hospital', station='+15550100001', moment=MOMENT)
    for _ in range(3):
        sliced, pages = repair.slice_pages(image, 1)
        assert pages == 2 and tiff_bytes.settle(sliced) == sliced
        assembled, total = repair.assemble(image, sliced, 1)
        assert total == 3 and tiff_bytes.settle(assembled) == assembled
        stamped, count, _ = relay_pages.stamp_tiff(tiff, header='Valley Hospital', station='+15550100001',
                                                   moment=MOMENT)
        assert count == 3 and tiff_bytes.settle(stamped) == stamped
    assert len({sha(repair.slice_pages(image, 1)[0]) for _ in range(10)}) == 1


def test_settle_refuses_what_it_cannot_map():
    with pytest.raises(ValueError):
        tiff_bytes.settle(b'%PDF-1.7')
    data, _ = faximage.stamp(engine_image(1), header='Valley Hospital', station='+15550100001', moment=MOMENT)
    with pytest.raises(ValueError):
        tiff_bytes.settle(data[:len(data) // 2])  # strips run past the end
    looped = bytearray(data)
    looped[4:8] = (8).to_bytes(4, 'little')  # the first directory "starts" at its own header
    with pytest.raises(ValueError):
        tiff_bytes.settle(bytes(looped))
