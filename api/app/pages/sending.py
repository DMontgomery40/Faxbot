"""The attempt-time hook: the one layout the pages of one send take, and the changes that go with it.

Called for each attempt before anything is dialed or uploaded
(``outbound_transport.CapturedTransport.prepare``). The layout chooser
(``conversion.choose_layout``) prices the pages as they are, dense pages and
the experimental encoded pages (``codec/send.py``, only for a number whose
recipient agreed) from the same pages, plus the fax-friendly renderings of
them (``friendly.py``: shading kept with a pattern, and light areas made white
only with the opt-in), and keeps exactly one: the cheapest expected bill, and
the most faithful at that bill. The pages as they are and dense pages may also
be trimmed or kept at standard resolution; encoded pages never are. It is
decided again for every attempt, so a fax that moves to another route is
decided for that route.

Choosing and publishing are separate (JOINT-OPTIMIZER-AUDIT step 2):

- ``evaluate`` measures and prices every way one account may send the pages,
  with that account's own tariff for the number it calls (``Account``), and
  writes nothing but shared caches (the measured codings, the fax-friendly
  renderings). The route choice (``routing.joint``) evaluates every account a
  fax may use this way and compares them before binding one.
- ``publish`` writes exactly the chosen pages to the attempt's files and
  records what was sent. A selection the route choice measured is published
  only while what it was measured on still holds (``SelectionChanged``).

The fax's own PDF and fax image are never changed: a changed send gets its
own files beside them (``packed-<fax>-<attempt>.tiff`` / ``.pdf``), which the
usual retention cleanup removes. A provider that fetches the document from
Faxbot gets that attempt's PDF at the fetch address (``fetched_pdf``).
Without a measured selection, anything that goes wrong here is logged and the
fax goes exactly as it would have; this hook never stops a send then. Faxes
sent together in one call are left alone (batching screens each fax's own
image for the shared call).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import logging
import math
from pathlib import Path
import re

from . import capability as capabilities
from ..outbound_worker import CapacityWait

PREFIX = 'packed-'
_HEX32 = re.compile(r'[a-f0-9]{32}')


@dataclass(frozen=True)
class PreparedPages:
    pdf: str | None  # the PDF a cloud provider uploads instead of the fax's own, when changed
    tiff: str | None  # the fax image Faxbot's own engines send instead, when changed
    original_pages: int
    sent_pages: int
    trimmed_pages: int
    # The coding measured smallest for these pages (pages/coding.py), for Faxbot's own engines on the trunk; with
    # no page change, ``pdf`` and ``tiff`` are None and only the coding goes with the call.
    coding: object = None


class SelectionChanged(CapacityWait):
    """What the route choice measured has changed since (the document's files, the account's tariff for the number
    it calls, or the recipient's page and coding permissions): nothing was sent, and the fax is given back to be
    chosen again at once with today's facts."""

    def __init__(self, why):
        self.why = why
        super().__init__(seconds=0.5)


def unchanged(prepared):
    """Whether an attempt's pages go as they are: no PreparedPages, or one that only carries the coding."""
    return prepared is None or (prepared.pdf is None and prepared.tiff is None)


def how_sent(configuration):
    """'image' (Faxbot's own engines send a fax image), 'pdf_upload' or 'pdf_url' (the provider fetches it)."""
    manifest = configuration.manifest
    if manifest is not None:
        try:
            return 'pdf_upload' if manifest.actions['send_fax'].body_kind == 'multipart' else 'pdf_url'
        except Exception:
            return 'pdf_url'
    if configuration.traits.get('requires_tiff') is True or configuration.provider_id in capabilities.IMAGE_ROUTES:
        return 'image'
    return capabilities.page_model(configuration.provider_id).get('how_sent') or 'pdf_url'


def paths(root, job_id, attempt_id):
    stem = Path(root) / f'{PREFIX}{job_id}-{attempt_id}'
    return stem.with_suffix('.tiff'), stem.with_suffix('.pdf')


def _call_resolution(values):
    """Whether the trunk sends fine pages (the SSL Fax engine re-images a page at any other resolution)."""
    try:
        from ..sip_trunk import fax_options
        return 'fine' if fax_options(values).fine else 'standard'
    except Exception:
        return 'fine'


def _card(engine, route):
    try:
        from ..routing.store import RouteStore
        return RouteStore(engine).card_for(route)
    except Exception:
        return None


def _billing(card):
    """How the route bills, from its rate card: per_page, per_minute, plan or unpriced."""
    if card is None:
        return 'unpriced'
    if card.flat_plan:
        return 'plan'
    return 'per_page' if card.per_page_micros else 'per_minute' if card.per_minute_micros else 'unpriced'


def _trim_seconds(rows, cap, resolution):
    """Seconds the left-out rows would have taken: each costs the machine's minimum scan line time."""
    if not rows or cap.scan_ms is None:
        return None
    per_row = cap.scan_ms * (2 if resolution == 'standard' else 1) / 1000
    return math.floor(rows * per_row)


# The eligibility and tariff contract for one account (JOINT-OPTIMIZER-AUDIT step 1) ---------------------------

@dataclass(frozen=True)
class Account:
    """Who sends this attempt and how it is priced: the account (``sip-pages``), its provider (``sip``), how its
    pages go (``how_sent``), and the tariff for the number it calls. ``facts`` (``routing.predict.RouteFacts``)
    are the account's own (``routing.pricing.account_facts``); None keeps the shared predictor's facts for the
    provider, as before accounts were named here."""
    key: str
    provider_id: str
    mode: str
    card: object = None
    facts: object = None

    def predict(self):
        """The layout chooser's predictor on this account's facts, or None for the shared predictor."""
        if self.facts is None:
            return None
        from ..routing.predict import Shape, predict_from
        facts = self.facts

        def priced(route_key, destination, shape):
            return predict_from(facts, Shape(shape.pages, shape.page_bits, shape.resolution, shape.layout,
                                             getattr(shape, 'measured', None), getattr(shape, 'coding', None)))
        return priced

    def fingerprint(self):
        """The tariff and eligibility these candidates were priced with; it changes when any of them changes. What
        earlier calls showed and a plan's use this month are left out: they move with every fax sent."""
        facts, card = self.facts, self.card
        terms = getattr(facts, 'terms', None)
        parts = [self.key, self.provider_id, self.mode]
        if card is not None:
            parts += [card.id, card.provider_id, card.currency, card.per_minute_micros, card.per_page_micros,
                      card.per_call_micros, card.billing_increment_seconds, card.minimum_seconds,
                      getattr(card, 'monthly_fee_micros', None)]
        if terms is not None:
            parts += [terms.destination_class, terms.prefixes, terms.page_time_seconds, terms.max_pages_per_fax,
                      terms.included_pages, terms.included_minutes, terms.overage_page_micros]
        if facts is not None:
            parts += [facts.route_key, facts.refused, facts.missing, facts.origin,
                      getattr(facts.destination, 'kind', None), getattr(facts.destination, 'region', None)]
        return hashlib.sha256(repr(parts).encode('utf-8')).hexdigest()


def account_for(engine, values, configuration, number, *, key=None, now=None):
    """The ``Account`` for sending by ``configuration`` to ``number``. ``key`` names the account the attempt is
    bound to (its own rate card first, then its provider's); without it, the provider's first account."""
    route = configuration.provider_id
    mode = how_sent(configuration)
    if key is None:
        return Account(route, route, mode, card=_card(engine, route))
    from ..routing.pricing import account_facts
    facts = account_facts(engine, values, key, number, provider=route, now=now)
    terms = facts.terms
    return Account(key, route, mode, card=terms.card if terms is not None and not facts.refused else None, facts=facts)


@dataclass(frozen=True)
class Permission:
    """What the recipient, the route and your settings allow for this attempt's pages."""
    number: str
    chosen: str
    cap: object
    packing_ok: bool
    trim_ok: bool
    match_ok: bool
    codec_ok: bool
    lighten: bool
    why: str | None
    whiten: bool
    usable: object = None

    def fingerprint(self):
        usable = self.usable
        return (self.number, self.chosen, self.packing_ok, self.trim_ok, self.match_ok, self.codec_ok, self.lighten,
                self.whiten, getattr(self.cap, 'limit', None), getattr(self.cap, 'fine', None),
                getattr(self.cap, 'ecm', None),
                tuple(sorted(usable.codings)) if usable is not None else None,
                getattr(usable, 'ecm', None))


def permission_for(engine, values, account, job, *, rule=None):
    """The ``Permission`` for this account's call to the job's dialed number. Reads only."""
    from . import friendly as fax_friendly
    from ..codec.send import setting_for
    route, mode, number = account.provider_id, account.mode, job.get('to_number')
    records = capabilities.records_for(engine)
    # The machine that answers is the dialed number's; the person's page settings are also read for the recipient
    # they chose (an approved toll-free number is dialed instead, routing/alternates.py): the stricter one wins.
    chosen = job.get('recipient_number') or number
    cap = records.capability(number)
    packing_ok, _ = capabilities.long_pages_allowed(engine, route, number, rule=rule)
    if chosen != number and records.recipient_packing(chosen) == 'never':
        packing_ok = False
    trim_ok = (mode == 'image' and cap.ecm is False and records.trim_allowed(number)
               and records.trim_allowed(chosen))
    # A document that is really standard resolution goes at standard (lossless; Faxbot's own engines only).
    match_ok = mode == 'image'
    # Fax-friendly shading (pages/friendly.py), decided for this attempt: the setting for your documents, the
    # recipient's own choice, whether this account's rate card bills by time, and whether the receiving machine has
    # error correction say whether the screened pages (and, with the opt-in, the whitened pages) are made at all;
    # the layout chooser then keeps them only at a lower expected bill, or for a named reason.
    lighten, why = fax_friendly.should_lighten(engine, values, route, number, card=account.card, ecm=cap.ecm)
    if lighten and chosen != number and fax_friendly.recipient_choice(engine, chosen) == 'never':
        lighten = False  # never for the recipient the person chose holds on an approved toll-free number too
    whiten = bool(lighten and fax_friendly.whiten_allowed(values))
    # Encoded pages (experimental): a candidate only when the dialed number, and the recipient the person chose,
    # agreed to them (codec/send.py). This read is cheap; nothing is drawn for a number that did not agree.
    codec_ok = setting_for(engine, number, chosen) is not None
    # The codings this call may use, measured on each candidate's pages (pages/coding.py): Faxbot's own engines on
    # the trunk only, since a fax service codes the pages itself.
    usable = _usable_codings(engine, values, route, mode, number, cap)
    return Permission(number, chosen, cap, packing_ok, trim_ok, match_ok, codec_ok, lighten, why, whiten, usable)


# Shared raster work ------------------------------------------------------------------------------------------

class RasterCache:
    """The raster work one attempt shares between the accounts it compares: each source's pages read, or its PDF
    drawn, once, and each file's hash. Drawing happens outside any queue lock (the claim is a lease)."""

    def __init__(self, root, job_id, attempt_id):
        self.root, self.job_id, self.attempt_id = Path(root), job_id, attempt_id
        self._frames, self._digests = {}, {}

    def digest(self, path):
        """The SHA-256 of a file's bytes, or None when it is missing (a symbolic link counts as missing)."""
        if path is None:
            return None
        path = Path(path)
        if path.is_symlink() or not path.is_file():
            return None
        stat = path.stat()
        key = (str(path), stat.st_mtime_ns, stat.st_size)
        if key not in self._digests:
            self._digests[key] = hashlib.sha256(path.read_bytes()).hexdigest()
        return self._digests[key]

    def image(self, tiff):
        """The pages of a fax image (Faxbot's own engines send it)."""
        from .. import conversion
        key = ('image', str(tiff), self.digest(tiff))
        if key not in self._frames:
            self._frames[key] = conversion.read_fax_frames(str(tiff))
        return self._frames[key]

    def drawn(self, pdf):
        """The PDF's pages drawn as fax pages, for a provider that takes the PDF; drawn once per attempt into a
        temporary file beside the attempt files, which is removed at once."""
        from .. import conversion
        key = ('drawn', str(pdf), self.digest(pdf))
        if key not in self._frames:
            raster = paths(self.root, self.job_id, self.attempt_id)[0]
            raster = raster.with_name(raster.stem + '.source.tiff')
            try:
                conversion.pdf_to_tiff(str(pdf), str(raster))
                self._frames[key] = conversion.read_fax_frames(str(raster))
            finally:
                try:
                    raster.unlink(missing_ok=True)
                except OSError:
                    pass
        return self._frames[key]


# Every way one account may send the pages, measured and priced (JOINT-OPTIMIZER-AUDIT step 2) ----------------

@dataclass(frozen=True)
class Option:
    """One way an account may send the attempt's pages, as measured and priced. Immutable; None is unknown."""
    account: str
    rendering: str                  # 'as_is', 'screened' or 'whitened'
    layout: str                     # 'normal', 'dense' or 'codec'
    coding: str | None              # the coding it is priced with (Faxbot's own engines), else None
    pages: int                      # physical pages sent
    bits: tuple                     # ((coding, total bits), ...) measured on these pages
    seconds: float | None           # predicted time on the line
    micros: int | None              # expected bill, over the call's duration spread
    currency: str | None
    billed_seconds: float | None    # expected billed seconds, on an account that bills by time
    billed_pages: float | None      # pages billed at a page price
    digest: str | None              # the pages' pixel fingerprint (pages.coding.digest)
    faithful: tuple = ()            # fidelity rank: smaller is more faithful; the pages as they are rank first
    basis: str = ''

    @property
    def original(self):
        return self.rendering == 'as_is' and self.layout == 'normal'


@dataclass
class Evaluated:
    """One account's candidates for this attempt and the one it would send; nothing published yet."""
    account: Account
    permission: Permission
    original_pages: int
    options: tuple
    chosen: Option
    shape: object                    # routing.predict.Shape of the chosen pages, for pricing the account
    sources: tuple                   # (PDF hash, fax image hash or None) the pages were measured from
    measured: bool = True            # False: the pages as they are, priced by page count (nothing drawn)
    work: dict = field(default_factory=dict, repr=False)  # what publishing the chosen pages needs

    @property
    def changes(self):
        """Whether the chosen pages differ from the fax's own (a file is published for the attempt)."""
        return bool(self.work.get('changed'))


def _shape_of(pages_count, shape, prediction):
    """``routing.predict.Shape`` for a candidate the chooser priced (a ``pages.decision.Shape``)."""
    from ..routing.predict import Shape
    return Shape(pages_count, shape.page_bits, shape.resolution, shape.layout, getattr(shape, 'measured', None),
                 getattr(shape, 'coding', None))


def _option(account, item):
    from . import coding as codings
    shape, prediction = item['shape'], item['prediction']
    bits = tuple((name, sum(values)) for name, values in (getattr(shape, 'measured', None) or {}).items()) \
        if isinstance(getattr(shape, 'measured', None), dict) else ()
    if not bits and shape.page_bits:
        bits = (('MMR', sum(shape.page_bits)),)
    cost = prediction.cost
    chosen = item.get('coding')
    return Option(account.key, item['rendering'], item['layout'], getattr(chosen, 'priced', None) or shape.coding,
                  len(item['pages']), bits, prediction.seconds, cost.micros if cost is not None else None,
                  cost.currency if cost is not None else None, getattr(prediction, 'expected_billed_seconds', None),
                  getattr(prediction, 'expected_billed_pages', None) or prediction.billed_pages,
                  codings.digest(item['pages']), tuple(item['faithful']), prediction.basis)


def _as_counted(account, job, now=None):
    """The pages as they are, priced by their count (nothing drawn): for an account whose pages nothing may
    change, when accounts are not being compared."""
    from ..routing.predict import Shape, predict_from
    pages = max(int(job.get('pages') or 1), 1)
    shape = Shape(pages, None, 'standard', 'normal')
    prediction = predict_from(account.facts, shape) if account.facts is not None else None
    cost = prediction.cost if prediction is not None else None
    option = Option(account.key, 'as_is', 'normal', None, pages, (), getattr(prediction, 'seconds', None),
                    cost.micros if cost is not None else None, cost.currency if cost is not None else None,
                    getattr(prediction, 'expected_billed_seconds', None), getattr(prediction, 'billed_pages', None),
                    None, (), getattr(prediction, 'basis', '') or '')
    return option, shape


def evaluate(engine, values, account, claim, job, pdf, tiff, *, rule=None, seal=None, now=None, cache=None,
             compare=False):
    """``Evaluated`` for sending this attempt's pages by ``account``, or None when they go as they are and nothing
    would change them. Writes nothing but shared caches. ``compare`` (several accounts are being compared): the
    pages as they are are drawn and measured even when nothing may change them, so every account is priced from
    the same measured pages. Raises the document's own refusals (``conversion.DocumentConversionError``, OSError)."""
    from .. import conversion
    from ..codec.send import restore_original_image
    from . import friendly as fax_friendly
    from .resolution import is_standard, standard_frames
    from .trim import rendered_pages, trim_frames
    if engine is None or getattr(claim, 'members', None):
        return None
    job_id, attempt_id = claim.job_id, claim.attempt_id
    if not (_HEX32.fullmatch(str(job_id)) and _HEX32.fullmatch(str(attempt_id))):
        return None
    from ..routing.sender_pins import pinned
    if pinned(engine, job.get('recipient_number') or job.get('to_number')):
        return None  # a registered-sender recipient's copy is binding: the pages go as they are (sender_pins, N17)
    route, mode = account.provider_id, account.mode
    root = Path(str(pdf)).parent
    cache = cache or RasterCache(root, job_id, attempt_id)
    # A fax accepted by an earlier build may have encoded pages written over its fax image: made again from the
    # original document, once, so this attempt chooses from the original (codec/send.py).
    restore_original_image(engine, job_id, pdf, tiff if mode == 'image' else None)
    allowed = permission_for(engine, values, account, job, rule=rule)
    number, chosen, cap = allowed.number, allowed.chosen, allowed.cap
    packing_ok, trim_ok, match_ok, codec_ok = allowed.packing_ok, allowed.trim_ok, allowed.match_ok, allowed.codec_ok
    requests = []
    if allowed.lighten:
        requests.append(fax_friendly.Request('documents'))
        if allowed.whiten:
            requests.append(fax_friendly.Request('documents', method='whitened'))
    source = Path(str(tiff)) if mode == 'image' and tiff else None
    sources = (cache.digest(pdf), cache.digest(source) if source is not None else None)
    if not compare and not packing_ok and not trim_ok and not match_ok and not codec_ok and not requests:
        return None
    if too_long(account, pdf, tiff) is not None:
        # Every account sends a fax this long as it is: changing its pages would hold too much in memory. The bound
        # account's preparation records why, once (``prepare``).
        return None
    # The changed pages, made once for the fax and each method and kept with the attempt files (a retry, or another
    # account compared for this attempt, draws nothing again).
    rendered = {}
    for request in requests:
        path = fax_friendly.lightened_pages(root, job_id, pdf, source, request)
        if path is not None:
            rendered[request.method] = (path, request)
    if not compare and not rendered and not packing_ok and not trim_ok and not match_ok and not codec_ok:
        return None  # no page changed, and nothing else would change them
    # A cloud provider takes a PDF: its pages are drawn to price and change them, then sent as a PDF.
    frames = cache.image(source) if source is not None else cache.drawn(pdf)
    if not frames:
        return None
    others = {}
    for method, (path, _) in rendered.items():
        pages = conversion.read_fax_frames(str(path))
        if pages and len(pages) == len(frames):
            others[method] = pages
    matched = standard_frames(frames) if match_ok else None
    if matched is not None:
        frames = matched
        # A document that is really standard resolution goes at standard, losslessly: it was made from a
        # standard image, which has no uniform gray to screen.
        others = {}
    resolution = 'standard' if is_standard(frames) else 'fine'
    # Whether this call carries fine pages: the SSL Fax engine draws a fine page again at standard resolution
    # when the trunk or the receiving machine does not take fine.
    call_fine = cap.fine is not False and (mode != 'image' or _call_resolution(values) == 'fine')
    if mode == 'image' and resolution == 'fine' and not call_fine:
        packing_ok = trim_ok = False  # the pages would be drawn again: leave them alone
    # Each rendering with its blank page bottoms left out the same way: (pages, trimmed pages, trimmed rows).
    trimmed = {method: (pages, 0, 0) for method, pages in [(None, frames)] + list(others.items())}
    if trim_ok:
        flags = rendered_pages(str(pdf))
        if flags is not None and len(flags) == len(frames):
            trimmed = {method: trim_frames(pages, flags) for method, (pages, _, _) in trimmed.items()}
    frames, trimmed_pages, trimmed_rows = trimmed[None]
    renderings = {method: (trimmed[method][0], fax_friendly.fidelity_of(rendered[method][1].result))
                  for method in others}
    faster = fax_friendly.named_reason(engine, values, job, route, allowed.why, now=now) if renderings else None
    usable = allowed.usable
    out_tiff, _ = paths(root, job_id, attempt_id)
    # Exactly one way to send: the pages as they are, packed onto long pages, the experimental encoded pages, or a
    # fax-friendly rendering of them, each with its smallest coding; the cheapest expected bill on this account's
    # own tariff, and the most faithful at that bill (conversion.choose_layout).
    from .views import packed_sentence

    def describe_dense(original, sent):
        return packed_sentence({'layout': 'dense', 'original_pages': original, 'sent_pages': sent,
                                'page_limit': cap.limit, 'limit_learned_at': cap.learned_at})

    def codec(pages):
        # Encoded pages carry the original document; ``pages`` are what they are priced against.
        return conversion.codec_pages(pages, engine=engine, number=number, route=route, capability=cap,
                                      pdf_path=str(pdf), seal=seal, recipient=chosen,
                                      exact_raster=mode == 'image',
                                      resolution='fine' if call_fine else 'standard', usable=usable)
    choice = conversion.choose_layout(frames, route=route, destination=number, limit=cap.limit,
                                      dense_allowed=packing_ok, codec=codec if codec_ok else None,
                                      card=account.card, boundary_seconds=cap.boundary_seconds,
                                      predict=account.predict(), describe_dense=describe_dense, usable=usable,
                                      measure_cache=_coding_cache(out_tiff) if usable is not None else None,
                                      renderings=renderings, faster=faster)
    options = tuple(_option(account, item) for item in choice.get('candidates') or ())
    layout = None if choice['layout'] == 'normal' else choice['layout']
    rendering = choice['rendering']
    picked = next(item for item in choice['candidates']
                  if item['rendering'] == (rendering or 'as_is') and item['layout'] == choice['layout'])
    encoded = layout == 'codec'
    if encoded:
        # Encoded pages go exactly as the codec made them: nothing trimmed, kept at standard or screened.
        trimmed_pages = trimmed_rows = 0
        matched = None
    elif rendering is not None:
        _, trimmed_pages, trimmed_rows = trimmed[rendering]
    changed = not (layout is None and not trimmed_pages and matched is None and rendering is None)
    work = {'choice': choice, 'source': source, 'resolution': resolution, 'cap': cap, 'usable': usable,
            'rendered': rendered, 'matched': matched is not None, 'trimmed_pages': trimmed_pages,
            'trimmed_rows': trimmed_rows, 'changed': changed, 'original_frames': len(frames)}
    selected = next(option for option in options
                    if option.rendering == picked['rendering'] and option.layout == picked['layout'])
    return Evaluated(account, allowed, len(frames), options, selected,
                     _shape_of(len(picked['pages']), picked['shape'], picked['prediction']), sources, True, work)


def counted(account, job):
    """``Evaluated`` for an account whose pages could not be drawn or measured: the pages as they are, priced by
    page count. It publishes nothing (the fax goes as it is)."""
    option, shape = _as_counted(account, job)
    return Evaluated(account, None, option.pages, (option,), option, shape, (None, None), False, {'changed': False})


def publish(engine, evaluated, claim, pdf, tiff, *, now=None):
    """Write exactly the chosen pages for this attempt and record what is sent; PreparedPages, or None when the
    pages go as they are. Raises what the files or records raise."""
    from .. import conversion
    from . import friendly as fax_friendly
    from .decision import LINE_BITS_PER_SECOND
    work = evaluated.work
    choice = work.get('choice')
    if choice is None:
        return None
    job_id, attempt_id = claim.job_id, claim.attempt_id
    account, allowed = evaluated.account, evaluated.permission
    route, mode, number, cap = account.provider_id, account.mode, allowed.number, work['cap']
    usable, rendered, frames_count = work['usable'], work['rendered'], work['original_frames']
    layout = None if choice['layout'] == 'normal' else choice['layout']
    rendering = choice['rendering']
    coded = choice.get('coding')
    if coded is not None:
        _record_coding(engine, job_id, attempt_id, number, route, coded, usable, now)
        if not any(usable.needs_request(coded.request(placed), placed) for placed in ('hylafax', 'builtin')):
            coded = None  # what either engine would take anyway: the call goes with its usual settings
    trimmed_pages, trimmed_rows, matched = work['trimmed_pages'], work['trimmed_rows'], work['matched']
    if not work['changed']:
        # The pages go as they are; the coding measured for them still goes with the call.
        return (PreparedPages(None, None, frames_count, frames_count, 0, coded) if coded is not None else None)
    out_tiff, out_pdf = paths(Path(str(pdf)).parent, job_id, attempt_id)
    encoded = layout == 'codec'
    try:
        pages = choice['pages']
        conversion.write_fax_tiff(pages, str(out_tiff))
        if mode != 'image':
            conversion.tiff_to_pdf(str(out_tiff), str(out_pdf))
            out_pdf.chmod(0o600)
            out_tiff.unlink(missing_ok=True)
        seconds = None
        if layout is not None and choice['seconds_saved'] is not None:
            seconds = choice['seconds_saved']
        trim_seconds = _trim_seconds(trimmed_rows, cap, work['resolution'])
        if trim_seconds is not None:
            seconds = (seconds or 0) + trim_seconds
        source = work['source']
        if matched and layout is None and source is not None:
            before, after = conversion.fax_page_bits(str(source)), conversion.fax_page_bits(str(out_tiff))
            if before and after:
                seconds = (seconds or 0) + max(0, (sum(before) - sum(after)) // LINE_BITS_PER_SECOND)
        records = capabilities.records_for(engine)
        if layout is not None or trimmed_pages or matched:
            records.record_change(
                job_id=job_id, attempt_id=attempt_id, number=number, route=route, original_pages=frames_count,
                sent_pages=len(pages), capability=cap, billing=_billing(account.card) if layout is not None else None,
                trimmed_pages=trimmed_pages or None, trimmed_rows=trimmed_rows or None,
                resolution='standard' if matched else None, seconds_saved=seconds, layout=layout,
                reason=choice['reason'], now=now)
        if encoded:
            _record_codec(engine, job_id, choice['codec'], now)
        if rendering is not None:
            # What the rendering saved, in the bits the call sends (the chooser's priced coding), apart from the
            # layout's own saving recorded above.
            fax_friendly.record_send(engine, job_id=job_id, attempt_id=attempt_id, request=rendered[rendering][1],
                                     bits=choice['rendering_bits'], now=now)
        return PreparedPages(str(out_pdf) if mode != 'image' else None, str(out_tiff) if mode == 'image' else None,
                             frames_count, len(pages), trimmed_pages, coded)
    except BaseException:
        for path in (out_tiff, out_pdf):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        raise


def still_current(engine, values, evaluated, configuration, job, pdf, tiff, *, rule=None, now=None):
    """None when the measured selection may be published as it is, else what changed, as a clause: the document's
    files, the account's tariff for the number it calls, or what the recipient and your settings allow."""
    cache = RasterCache(Path(str(pdf)).parent, '', '')
    account = evaluated.account
    if configuration.provider_id != account.provider_id or how_sent(configuration) != account.mode:
        return 'the account it was measured for is not the one bound'
    image = Path(str(tiff)) if account.mode == 'image' and tiff else None
    if (cache.digest(pdf), cache.digest(image) if image is not None else None) != evaluated.sources:
        return "the fax's document changed"
    fresh = account_for(engine, values, configuration, job.get('to_number'),
                        key=account.key if account.facts is not None else None, now=now)
    if fresh.fingerprint() != account.fingerprint():
        return "the account's price for this number changed"
    if evaluated.permission is not None and permission_for(
            engine, values, fresh, job, rule=rule).fingerprint() != evaluated.permission.fingerprint():
        return 'what the recipient accepts changed'
    return None


def prepare(engine, values, configuration, claim, job, pdf, tiff, *, rule=None, seal=None, now=None, handoff=None):
    """PreparedPages for this attempt, or None when its pages go as they are.

    ``handoff`` (``routing.joint.Handoff``) names the account the attempt is bound to, so its own tariff prices the
    pages, and, when the route choice compared accounts, the pages it measured and chose for that account
    (``selected``). A selection is published exactly as measured, or ``SelectionChanged`` is raised when what it
    was measured on has changed; a selection whose files cannot be written fails preparation
    (``PreparationFailure``), since the account was chosen for those pages. Without a selection this never raises:
    a failure is logged and the pages go as they are. ``seal`` opens the shared key of a number whose encoded pages
    are encrypted (``codec.store.KeySeal``)."""
    account = getattr(handoff, 'account', None)
    selected = getattr(handoff, 'selected', None)
    if selected is not None and engine is not None and not getattr(claim, 'members', None):
        why = still_current(engine, values, selected, configuration, job, pdf, tiff, rule=rule, now=now)
        if why is not None:
            logging.getLogger(__name__).info('Fax %s: the pages measured for its route are chosen again, because %s.',
                                             claim.job_id, why)
            raise SelectionChanged(why)
        from ..outbound_worker import PreparationFailure
        try:
            prepared = publish(engine, selected, claim, pdf, tiff, now=now)
        except (OSError, ValueError) as error:
            logging.getLogger(__name__).warning('Fax %s: the pages chosen for its route could not be written: %s',
                                                claim.job_id, error)
            raise PreparationFailure('preparation_failed') from None
        # What was published, for the attempt's record (written by the route choice once this preparation
        # succeeded, before the submission marker: routing/transport.py).
        published = getattr(handoff, 'published', None)
        if published is not None:
            published.update(prepared=prepared, evaluated=selected, pdf=str(pdf))
        return prepared
    try:
        chosen = account_for(engine, values, configuration, job.get('to_number'), key=account, now=now)
        evaluated = evaluate(engine, values, chosen, claim, job, pdf, tiff, rule=rule, seal=seal, now=now)
        if evaluated is None:
            _note_too_long(engine, chosen, claim, job, pdf, tiff, now)
            return None
        return publish(engine, evaluated, claim, pdf, tiff, now=now)
    except Exception:
        logging.getLogger(__name__).warning('Fax pages could not be prepared; they go as they are.', exc_info=True)
        return None


def too_long(account, pdf, tiff):
    """The page count of a fax longer than ``conversion.MAX_OPTIMIZED_PAGES`` (its pages go as they are on every
    account), else None. Reads only the document's page headers."""
    from .. import conversion
    source = tiff if account.mode == 'image' and tiff else pdf
    count = conversion.document_page_count(source) if source else None
    return count if count is not None and count > conversion.MAX_OPTIMIZED_PAGES else None


def _note_too_long(engine, account, claim, job, pdf, tiff, now):
    """For the bound account only: when the fax is too long to change, say why in its Sent details, once."""
    from .. import conversion
    if engine is None or getattr(claim, 'members', None) or not (
            _HEX32.fullmatch(str(claim.job_id)) and _HEX32.fullmatch(str(claim.attempt_id))):
        return
    count = too_long(account, pdf, tiff)
    if count is not None:
        capabilities.records_for(engine).record_change(
            job_id=claim.job_id, attempt_id=claim.attempt_id, number=job.get('to_number'), route=account.provider_id,
            original_pages=count, sent_pages=count, reason=conversion.too_long_sentence(count), now=now)


def _usable_codings(engine, values, route, mode, number, cap):
    """The codings a call to ``number`` may request (``pages.coding.usable_for``), or None when Faxbot's own engines
    do not place it or the records it reads cannot be read (logged; the engines then code as they always did)."""
    if route != 'sip' or mode != 'image':
        return None
    import sqlalchemy as sa
    from .. import hylafax_engine
    from ..routing.database import DeliveryStoreError
    from . import coding
    try:
        recipient = hylafax_engine.recipient_limits(engine, number)
        return coding.usable_for(engine, values, number, recipient=recipient, capability=cap)
    except (sa.exc.SQLAlchemyError, DeliveryStoreError):
        logging.getLogger(__name__).warning('The fax codings this number takes could not be read; the call codes its '
                                            'pages as usual.', exc_info=True)
        return None


def _coding_cache(out_tiff):
    from .coding import cache_path
    return cache_path(out_tiff)


def _record_coding(engine, job_id, attempt_id, number, route, choice, usable, now):
    """The coding this attempt requests and what was measured (``fax_coding_choices``), once."""
    import sqlalchemy as sa
    from ..routing.database import DeliveryStoreError
    from .coding import record_choice
    try:
        record_choice(engine, job_id=job_id, attempt_id=attempt_id, number=number, route=route, choice=choice,
                      receiver_known=usable.receiver is not None, now=now)
    except (sa.exc.SQLAlchemyError, DeliveryStoreError):
        logging.getLogger(__name__).warning('The fax coding chosen for this attempt could not be recorded.')


def _record_codec(engine, job_id, details, now):
    """The codec's details for the fax (``codec_sends``), once; the attempt's own record is its page change."""
    import sqlalchemy as sa
    from ..codec.send import record_attempt
    row = getattr(details, 'row', None)
    if not row:
        return
    try:
        record_attempt(engine, job_id, row, now or datetime.utcnow())
    except sa.exc.SQLAlchemyError:
        logging.getLogger(__name__).warning('The details of the encoded pages could not be recorded for this fax.')


def fetched_pdf(pdf_path, job_id, media_url):
    """The PDF a provider fetching ``media_url`` gets: the pages chosen for the attempt that made that link
    (``packed-<fax>-<attempt>.pdf``) when that attempt changed them, else the fax's own PDF (``pdf_path``).

    The attempt is read from the link stored with the fax's current token (``outbound_store.grant_pdf``), never
    from the request, so a token only ever opens its own attempt's pages."""
    from urllib.parse import parse_qs, urlsplit
    try:
        attempt = parse_qs(urlsplit(str(media_url or '')).query).get('attempt', [''])[0]
    except ValueError:
        return pdf_path
    if not (_HEX32.fullmatch(str(job_id or '')) and _HEX32.fullmatch(attempt)):
        return pdf_path
    _, chosen = paths(Path(pdf_path).parent, job_id, attempt)
    if chosen.is_file() and not chosen.is_symlink():
        return chosen
    return pdf_path


def cleanup(root, cutoff):
    """Remove changed-page files older than ``cutoff`` (the retention cleanup's time)."""
    try:
        for path in Path(root).glob(PREFIX + '*'):
            if (path.is_file() and not path.is_symlink()
                    and datetime.utcfromtimestamp(path.stat().st_mtime) < cutoff):
                path.unlink(missing_ok=True)
    except OSError:
        return False
    return True
