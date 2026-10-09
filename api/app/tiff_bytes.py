"""The same pages always give the same TIFF bytes: zero what Pillow and libtiff leave unwritten.

libtiff starts each directory on an even offset and skips the pad byte before it without writing it, so that byte
is whatever the buffer held (0x00 one time, 0x0f another). Pillow's page appending also leaves each appended page's
own header in the file. Neither is part of the image, yet both change the file's SHA-256, so a fax image built
twice from the same pages could get two digests.

``settle`` keeps every byte the TIFF's structure points at (the header, each directory, every tag value stored
outside its directory, and every strip or tile) and sets every other byte to zero. Pixels and tags never change;
only bytes no reader looks at do. Every TIFF write whose bytes Faxbot hashes, signs or compares goes through it
(``direct/faximage.py``, ``direct/repair.py``, ``direct/relay_pages.py``).
"""
import struct

# Bytes per value of each TIFF field type (TIFF 6.0, plus the BigTIFF types a classic TIFF may still name).
_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 13: 4, 16: 8, 17: 8, 18: 8}
_DATA = ((273, 279), (324, 325))  # (offsets, byte counts) of strips and of tiles
# Tags that point at structures this module does not map (sub-directories, EXIF, GPS, embedded JPEG): such a file
# is refused rather than having bytes zeroed that something reads.
_POINTERS = frozenset({330, 513, 34665, 34853, 40965})
MAX_DIRECTORIES = 10_000


def _values(data, order, kind, count, where):
    form = {3: 'H', 4: 'I', 16: 'Q'}.get(kind)
    if form is None:
        raise ValueError('The TIFF names its strips with an unsupported type.')
    return struct.unpack_from(f'{order}{count}{form}', data, where)


def used(data):
    """The byte ranges the TIFF's structure points at, as sorted (start, end) pairs; ValueError if unmapped."""
    data = bytes(data)
    if data[:4] == b'II*\x00':
        order = '<'
    elif data[:4] == b'MM\x00*':
        order = '>'
    else:
        raise ValueError('This is not a classic TIFF.')
    ranges, seen = [(0, 8)], set()
    directory = struct.unpack_from(order + 'I', data, 4)[0]
    while directory:
        if directory in seen or len(seen) >= MAX_DIRECTORIES or directory + 2 > len(data):
            raise ValueError('The TIFF directories do not form a chain.')
        seen.add(directory)
        count = struct.unpack_from(order + 'H', data, directory)[0]
        end = directory + 2 + 12 * count + 4
        if end > len(data):
            raise ValueError('A TIFF directory runs past the end of the file.')
        ranges.append((directory, end))
        tags = {}
        for index in range(count):
            entry = directory + 2 + 12 * index
            tag, kind, number = struct.unpack_from(order + 'HHI', data, entry)
            if tag in _POINTERS or kind not in _SIZES:
                raise ValueError('The TIFF has a structure this cannot map.')
            size = _SIZES[kind] * number
            where = entry + 8 if size <= 4 else struct.unpack_from(order + 'I', data, entry + 8)[0]
            if size > 4:
                ranges.append((where, where + size))
            if where + size > len(data):
                raise ValueError('A TIFF tag value runs past the end of the file.')
            tags[tag] = (kind, number, where)
        for offsets_tag, counts_tag in _DATA:
            if (offsets_tag in tags) != (counts_tag in tags):
                raise ValueError('The TIFF names strips without their sizes.')
            if offsets_tag in tags:
                offsets, counts = (_values(data, order, *tags[name]) for name in (offsets_tag, counts_tag))
                if len(offsets) != len(counts):
                    raise ValueError('The TIFF names strips without their sizes.')
                for start, length in zip(offsets, counts):
                    if start + length > len(data):
                        raise ValueError('A TIFF strip runs past the end of the file.')
                    ranges.append((start, start + length))
        directory = struct.unpack_from(order + 'I', data, end - 4)[0]
    return sorted(ranges)


def settle(data):
    """``data`` with every byte its TIFF structure does not point at set to zero; ValueError if it cannot map it."""
    data = bytes(data)
    out = bytearray(len(data))
    for start, end in used(data):
        out[start:end] = data[start:end]
    return bytes(out)
