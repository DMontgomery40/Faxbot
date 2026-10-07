"""Received faxes: long pages another Faxbot packed are delivered as the original pages.

The received fax image is kept exactly as it arrived (it is the evidence).
When it carries Faxbot's page bands (``unpack``), the PDF Faxbot delivers is
made from the original pages instead, and ``inbound_page_splits`` records
how many pages arrived and how many were delivered. Anything that goes wrong
here delivers the fax as received.
"""
from __future__ import annotations

from dataclasses import dataclass
import logging
import os
import tempfile


@dataclass(frozen=True)
class Split:
    path: str  # the original pages as a fax image, to turn into the delivered PDF
    received_pages: int
    original_pages: int

    def record(self, engine, inbound_fax_id):
        """Record the split; a failure is logged and never reaches the fax."""
        try:
            from .capability import records_for
            return records_for(engine).record_split(inbound_fax_id, received_pages=self.received_pages,
                                                    original_pages=self.original_pages)
        except Exception:
            logging.getLogger(__name__).warning('A split received fax could not be recorded.')
            return None


def split_for_delivery(tiff_path, directory):
    """Split(path, pages received, original pages) when the image carries page bands, else None. Never raises."""
    from .. import conversion
    descriptor, temporary = None, None
    try:
        frames = conversion.read_fax_frames(tiff_path)
        if not frames:
            return None
        from .unpack import split_frames
        originals = split_frames(frames)
        if originals is None:
            return None
        descriptor, temporary = tempfile.mkstemp(dir=directory, prefix='.inbound-split-', suffix='.tiff')
        os.close(descriptor)
        conversion.write_fax_tiff(originals, temporary)
        return Split(temporary, len(frames), len(originals))
    except Exception:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass
        logging.getLogger(__name__).warning('A received fax with long pages is delivered as it arrived.')
        return None
