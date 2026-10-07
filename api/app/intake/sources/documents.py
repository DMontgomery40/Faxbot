"""Turn an attachment or a file into a validated PDF in the data folder; join several into one fax.

A PDF is kept byte for byte. A TIFF is converted with Faxbot's own converter,
whose output is not byte-identical between runs, so connectors identify every
document by the SHA-256 of what the source supplied, never of a converted copy.
"""
import hashlib
import os
import tempfile

from ...conversion import DocumentConversionError, ensure_dir, tiff_to_pdf, validate_pdf


class Unreadable(ValueError):
    """The file is not a PDF or TIFF Faxbot can read."""


def _temporary(directory, suffix):
    ensure_dir(directory)
    handle, path = tempfile.mkstemp(prefix='.connector-', suffix=suffix, dir=directory)
    os.close(handle)
    return path


def discard(*paths):
    for path in paths:
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except OSError:
            pass


def to_pdf(data, kind, directory):
    """Write ``data`` as a validated PDF; return (path, pages, sha256 of that PDF, converted)."""
    if kind == 'pdf':
        if not data.startswith(b'%PDF-'):
            raise Unreadable()
        path = _temporary(directory, '.pdf')
        try:
            with open(path, 'wb') as handle:
                handle.write(data)
            pages = validate_pdf(path)
        except DocumentConversionError:
            discard(path)
            raise Unreadable() from None
        except BaseException:
            discard(path)
            raise
        return path, pages, hashlib.sha256(data).hexdigest(), False
    if kind == 'tiff':
        source, path = _temporary(directory, '.tiff'), _temporary(directory, '.pdf')
        try:
            with open(source, 'wb') as handle:
                handle.write(data)
            pages, _ = tiff_to_pdf(source, path)
            with open(path, 'rb') as handle:
                digest = hashlib.sha256(handle.read()).hexdigest()
        except DocumentConversionError:
            discard(path)
            raise Unreadable() from None
        except BaseException:
            discard(path)
            raise
        finally:
            discard(source)
        return path, pages, digest, True
    raise Unreadable()


def join(paths, directory):
    """One PDF holding every page of ``paths`` in order; returns (path, pages)."""
    if len(paths) == 1:
        output = _temporary(directory, '.pdf')
        with open(paths[0], 'rb') as source, open(output, 'wb') as target:
            target.write(source.read())
        return output, validate_pdf(output)
    from pypdf import PdfReader, PdfWriter
    writer = PdfWriter()
    for path in paths:
        for page in PdfReader(path).pages:
            writer.add_page(page)
    output = _temporary(directory, '.pdf')
    try:
        with open(output, 'wb') as handle:
            writer.write(handle)
        return output, validate_pdf(output)
    except BaseException:
        discard(output)
        raise


def source_digest(digests):
    """One digest for an ordered list of source documents (a single one is its own digest)."""
    if len(digests) == 1:
        return digests[0]
    return hashlib.sha256('\n'.join(digests).encode('ascii')).hexdigest()
