"""Error correction on Faxbot's own calls to one number: one fact that every price and page choice reads.

With error correction (ITU-T T.30 Annex A) a page goes in partial pages of at
most 256 frames of 256 octets, or of 64 octets when the sending side asks for
64-octet frames in its DCS. Each partial page past a page's first costs one
more exchange and modem start on the line (``pages.coding.ecm_extra_seconds``),
so a page just past a block edge costs a whole turnaround more than one just
under it. Whether a call uses error correction depends on both ends: Faxbot's
side (your fax settings, the number's own limit on Recipients, and what engine
learning turned on, ``hylafax_engine.call_settings``) and the receiving
machine, which offers it in its DIS. Both of Faxbot's engines then use it for
every coding, MH and MR included: spandsp 0.0.6 sets error correction from the
far DIS alone (``t30.c`` ``process_rx_dis_dtc``), and HylaFAX+ 7.0.11 whenever
both ends support it and the job allows it (``faxd/FaxSend.c++``).

``fact_for`` is the one place this is worked out. The pre-dial predictor reads
it through the route's facts (``predict_facts.facts_for`` puts it on
``Link.ecm``), so the dry run, the joint route choice (``routing.joint``, each
account's ``pages.sending.Account``) and the layout and encoded-page choosers
(``conversion.choose_layout``, ``codec.decision``) price the same partial
pages; the codings a call may use (``pages.coding.usable_for``) and the dense
page cutter's block edges (``pages.packing``) read the same fact. Nothing here
writes a record or places a call.

What the receiving machine offers comes from what Faxbot saw last, newest
first: its DIS (the built-in engine's frames, or the SSL Fax engine's logged
capabilities, ``pages.capability``), or a successful call that used error
correction. A frame size other than 256 octets and a measured turnaround per
partial page come from engine learning's error-correction records
(``engine_learning``); until there are enough of those, a partial page costs
``pages.coding.ECM_BLOCK_SECONDS``, measured on the SSL Fax engine's loopback.
"""
from __future__ import annotations

from dataclasses import dataclass
import logging

FRAME_OCTETS = (64, 256)
# Octets each error-correction frame adds to its data on the line: a shared flag, the address, control and FCF
# octets, the frame number and the two-octet frame check (T.30 Annex A, A.3.1; HDLC framing per T.30 5.3). The
# predictor's page and block figures were measured with 256-octet frames, so only a smaller frame's extra framing
# is added (``frame_seconds``).
FRAME_OVERHEAD_OCTETS = 7


@dataclass(frozen=True)
class Fact:
    """Error correction on a call to one number. None is unknown, never no."""
    ours: bool                          # Faxbot's side allows it (your setting, the number's limit, engine learning)
    far: bool | None                    # the receiving machine offers it; None: not seen yet
    used: bool | None                   # the call will use it; None: not known (the coding then decides)
    frame_octets: int = 256             # the frame size the call sends with error correction
    block_seconds: float | None = None  # one partial page past a page's first, in seconds; None: the default
    block_calls: int = 0                # recorded partial-page turnarounds behind ``block_seconds``
    why: str | None = None              # 'setting', 'limit', 'dis', 'negotiated' or None: where ``used`` came from
    frame_why: str | None = None        # one sentence when the frame size is not the usual 256 octets
    frame_step: float = 0.0             # seconds each frame costs beyond its octets (the built-in engine's T.38 step)

    def __post_init__(self):
        if self.frame_octets not in FRAME_OCTETS:
            raise ValueError('Error-correction frames are 64 or 256 octets.')

    @property
    def turnaround(self):
        """Seconds one partial page past a page's first costs: measured on your calls, else the loopback's."""
        from ..pages.coding import ECM_BLOCK_SECONDS
        return self.block_seconds if self.block_seconds is not None else ECM_BLOCK_SECONDS

    def sentences(self):
        """Plain sentences for Recipients → Details, "Their fax machine", and ``faxbot recipients fax-machine``."""
        if self.used is False:
            if self.why == 'limit':
                first = 'Calls to this number go without error correction: you turned it off for this number.'
            elif self.why == 'setting':
                first = 'Calls to this number go without error correction: it is off in your fax settings.'
            else:
                first = 'Its fax machine does not offer error correction, so calls to it go without.'
            return [first]
        if self.used is None:
            return ['Faxbot has not yet seen whether its fax machine offers error correction.']
        found = ('Calls to this number use error correction: the last fax to it used it.' if self.why == 'negotiated'
                 else 'Calls to this number use error correction: its fax machine offers it.')
        size = 64 * 256 if self.frame_octets == 64 else 256 * 256
        seconds = f'{self.turnaround:.1f}'.rstrip('0').rstrip('.')
        where = (f'measured on {self.block_calls} of your calls' if self.block_calls
                 else "measured on Faxbot's own test calls")
        block = (f'Each {size // 1024} KB of a page past the first adds about {seconds} seconds on the line '
                 f'({where}).')
        return [found, block] + ([self.frame_why] if self.frame_why else [])

    def view(self):
        """The fact for the console and the command line: whether it is used, the frame size, the turnaround."""
        return {'used': self.used, 'frame_octets': self.frame_octets, 'block_seconds': round(self.turnaround, 2),
                'block_calls': self.block_calls, 'sentences': self.sentences()}


def decide(ours, *, views=(), capability=None, limit=None, frame_octets=256, block_seconds=None, block_calls=0,
           frame_why=None, frame_step=0.0) -> Fact:
    """The ``Fact`` from what is known (pure). ``ours``: this call's error correction on Faxbot's side
    (``hylafax_engine.call_settings``); ``limit``: the number's own setting (Recipients), when set; ``views``:
    engine learning's joined outbound calls to the number, newest first; ``capability``: the receiving machine's
    newest logged capabilities (``pages.capability.Capability``)."""
    if not ours:
        why = 'limit' if limit is False else 'setting'
        return Fact(False, None, False, frame_octets, block_seconds, block_calls, why, frame_why, frame_step)
    far, why, when = None, None, None
    for view in views:
        if view.get('direction', 'outbound') != 'outbound':
            continue
        if view.get('far_ecm') is not None:
            far, why, when = bool(view['far_ecm']), 'dis', view.get('when')
            break
        if view.get('status') == 'SUCCESS' and view.get('ecm') == 'on':
            far, why, when = True, 'negotiated', view.get('when')
            break
    seen = getattr(capability, 'learned_at', None)
    logged = getattr(capability, 'ecm', None)
    if logged is not None and (far is None or (seen is not None and when is not None and seen > when)):
        far, why = bool(logged), 'dis'
    return Fact(True, far, far, frame_octets, block_seconds, block_calls, why if far is not None else None,
                frame_why, frame_step)


def fact_for(engine, values, number, *, recipient=None, settings=None, views=None, capability=None, now=None) -> Fact:
    """The ``Fact`` for Faxbot's next call to ``number``, from the installation's records. Pass what the caller has
    already read (``recipient``, ``settings``, ``views``, ``capability``) so nothing is read twice. Records that
    cannot be read leave the receiving machine unknown, logged; anything else raises."""
    import sqlalchemy as sa
    from .. import engine_learning as learning, hylafax_engine
    if settings is None:
        if recipient is None and engine is not None:
            recipient = hylafax_engine.recipient_limits(engine, number)
        settings = hylafax_engine.call_settings(values, number, recipient=recipient)
    limit = (recipient or {}).get('ecm')
    limit = None if limit is None else bool(limit)
    frame = {}
    if engine is not None and settings.ecm:
        try:
            frame = _frame(engine, values, number, settings, now)
        except sa.exc.SQLAlchemyError as error:
            logging.getLogger(__name__).warning('The error-correction records could not be read (%s); the call is '
                                                'priced with 256-octet frames.', type(error).__name__)
    if not settings.ecm or engine is None:
        return decide(settings.ecm, limit=limit, capability=capability, **frame)
    if views is None or capability is None:
        from ..pages.capability import PageRecordError, records_for
        try:
            if capability is None:
                capability = records_for(engine).capability(number)
            if views is None:
                moment = now or learning.utcnow()
                epoch = learning.current_epoch(engine, values, now=moment, write=False)
                views = learning.on_trunk(learning.joined_calls(
                    engine, number, direction='outbound',
                    since=learning.evidence_since(epoch, moment, learning.LEARN_DAYS), limit=50), values)
        except (sa.exc.SQLAlchemyError, PageRecordError) as error:
            logging.getLogger(__name__).warning('What the receiving machine offers could not be read (%s); error '
                                                'correction is priced by the coding.', type(error).__name__)
            views = views or ()
    return decide(True, views=views or (), capability=capability, limit=limit, **frame)


def _frame(engine, values, number, settings, now):
    """The frame size this call uses (engine learning's decision on the call's own settings) and, from the same
    records (``engine_ecm.learned``), its sentence, the measured turnaround of a partial page and, on the built-in
    engine's T.38 calls, the step each frame costs in Asterisk's T.38 terminal (as the decision weighed it)."""
    from .. import engine_ecm, engine_learning as learning
    moment = now or learning.utcnow()
    decision = getattr(settings, 'learned', None)
    paced = (getattr(decision, 'engine', None) == 'builtin' and bool(getattr(settings, 't38', False))
             and getattr(values, 'sip_t38_enabled', True))
    epoch = learning.current_epoch(engine, values, now=moment, write=False)
    found = engine_ecm.learned(engine, number, since=learning.evidence_since(epoch, moment, learning.LEARN_DAYS),
                               rate=settings.max_rate, now=moment, paced=paced)
    frame = 64 if getattr(decision, 'ecm_frame', None) == 64 else 256
    return {'frame_octets': frame, 'frame_why': found.frame_why if frame == found.frame_octets == 64 else None,
            'block_seconds': found.block_seconds, 'block_calls': found.block_calls,
            'frame_step': engine_ecm.T38_TERMINAL_FRAME_SECONDS if paced else 0.0}


def frame_seconds(page_bits, frame_octets, rate, step=0.0):
    """Seconds a smaller frame's extra framing adds to these pages at ``rate`` bit/s: every 64-octet frame carries
    the same 7 octets of framing as a 256-octet one, so the same data takes four times as many frames, each with
    ``step`` seconds more where the engine adds a step per frame (``Fact.frame_step``). 0 at 256 octets, whose
    framing the measured page and block figures include."""
    if frame_octets == 256 or not rate:
        return 0.0
    import math
    frames = sum(math.ceil(math.ceil(bits / 8) / frame_octets) for bits in page_bits)
    usual = sum(math.ceil(math.ceil(bits / 8) / 256) for bits in page_bits)
    return (frames - usual) * (FRAME_OVERHEAD_OCTETS * 8 / rate + step)
