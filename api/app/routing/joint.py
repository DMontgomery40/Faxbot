"""Choose the account after measuring its best pages (JOINT-OPTIMIZER-AUDIT steps 1-3).

Before, the route choice priced every account by the fax's page count and only
afterwards prepared the chosen account's best pages, so an account that is
cheaper once its pages are packed or coded could lose to one priced on raw
pages. Now, for a fax whose route is chosen by cost among several accounts:

1. Each account the fax may use is described once (``pages.sending.Account``):
   its own rate card for the number it calls, how its pages go, and what the
   recipient allows. A missing price stays missing, never zero.
2. Every way that account may send the pages is measured and priced with that
   tariff (``pages.sending.evaluate``): the pages as they are always, packed
   pages, renderings and codings where allowed. Nothing is published; raster
   work is shared between the accounts (``RasterCache``) and happens outside
   any queue lock.
3. The route policy ranks the accounts again by each one's best measured price
   (``routing.pricing.prices_for(measured=...)``), with every rule, cap,
   preference, reliability record and plan hold applied exactly as before.
   The account bound is then handed its own measured pages (``Handoff``),
   which are published only while what they were measured on still holds.

A fax its rules send in a fixed order, a fax with a single account, and a
shared call keep their current path: the bound account still prepares its
pages with its own tariff (the extra-account fix), but no accounts are compared.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
import logging
from pathlib import Path
import re
import time


# Why accounts were not compared for an attempt, for its record.
LIMITS = ('document', 'unmeasured', 'one_account', 'ordered', 'shared_call', 'deadline')
_HEX32 = re.compile(r'[a-f0-9]{32}')
# Measuring stops this many seconds before the claim's lease ends, so binding, preparing the call and the submission
# marker always fit inside it: measuring can never make a fax miss its lease again and again.
SUBMIT_MARGIN = 10.0
# The measuring budget when a claim does not say when its lease ends.
DEFAULT_BUDGET = 15.0


def _clock():
    return time.monotonic()


def deadline_for(claim, now=None):
    """The monotonic time measuring must stop by for this claim: its lease end less ``SUBMIT_MARGIN``."""
    expires = getattr(claim, 'expires_at', None)
    if isinstance(expires, datetime):
        budget = (expires - (now or datetime.utcnow())).total_seconds() - SUBMIT_MARGIN
    else:
        budget = DEFAULT_BUDGET
    return _clock() + max(0.0, budget)


@dataclass(frozen=True)
class Handoff:
    """What the route choice hands the transport that prepares the attempt it bound: the account key, that
    account's measured selection (``pages.sending.Evaluated``) when accounts were compared, and what the attempt's
    record keeps besides the pages (``routing.selections.summary``: accounts compared, runner-up, candidates)."""
    attempt_id: str
    account: str | None
    selected: object = None
    summary: dict | None = None
    mailbox_id: str | None = None        # the fax's sending mailbox: its call presents that mailbox's reply number
    # Filled by page preparation when it publishes ``selected``: {'prepared', 'evaluated', 'pdf'}.
    published: dict = field(default_factory=dict, compare=False)


_HANDOFF: ContextVar = ContextVar('faxbot_route_handoff', default=None)


@contextmanager
def handing_over(handoff):
    """Make ``handoff`` visible to the transport preparing its attempt, inside this block only."""
    token = _HANDOFF.set(handoff)
    try:
        yield handoff
    finally:
        _HANDOFF.reset(token)


def handed_over(claim):
    """The ``Handoff`` for this claim's attempt, or None (no route choice ran, or it was for another attempt)."""
    found = _HANDOFF.get()
    if found is None or found.attempt_id != getattr(claim, 'attempt_id', None):
        return None
    return found


@dataclass
class Joint:
    """One attempt's measured accounts: ``frontiers`` {account key: Evaluated}, ``measured`` {account key:
    (predict.Shape, predict.RouteFacts)} for pricing them, and ``limit`` when accounts were not compared."""
    frontiers: dict = field(default_factory=dict)
    measured: dict = field(default_factory=dict)
    limit: str | None = None

    def selection(self, key):
        return self.frontiers.get(key)


def compares(claim, plan):
    """None when this attempt's accounts are compared after measuring, else why not (``LIMITS``)."""
    if getattr(claim, 'members', None):
        return 'shared_call'
    pinned = getattr(plan, 'pinned', None)
    if pinned is not None and pinned.envelope.mode in ('one', 'ordered'):
        return 'ordered'  # your order decides, not the price
    providers = [choice for choice in plan.choices if choice.route.kind == 'provider']
    skipped = [key for key, why in getattr(plan, 'skipped', ()) if why in ('over_cap', 'unknown_cost')]
    if len(providers) + len(skipped) < 2:
        return 'one_account'
    return None


def measure(store, revision, profile, claim, plan, job, *, bound, configuration_for, ensure_artifact=None,
            now=None, mailbox_id=None):
    """``Joint`` for this attempt: every calling account the plan may use (and any its cost cap left out on the
    page-count price) measured and priced on its own tariff. ``configuration_for(key)`` builds an account's
    configuration from the accepted revision (raising ``routes.RouteUnavailable``); ``ensure_artifact`` makes the
    fax image an account needs (``routes.ensure_route_artifact``). An account that cannot be set up is left to the
    route choice (it skips it); a document that cannot be drawn or measured compares nothing (``limit``)."""
    from ..codec.store import KeySeal
    values = revision.values
    job_id = claim.job_id
    if not _HEX32.fullmatch(str(job_id)):
        return Joint(limit='document')
    root = Path(values.fax_data_dir)
    pdf, tiff = root / f'{job_id}.pdf', root / f'{job_id}.tiff'
    if pdf.is_symlink() or not pdf.is_file():
        return Joint(limit='document')
    engine = store.configuration.engine
    from ..outbound_transport import _layout_rule

    def configured(key):
        configuration = profile.configuration if key == bound else configuration_for(key)
        if ensure_artifact is not None:
            ensure_artifact(revision, configuration, job_id)
        return configuration
    return measure_document(engine, values, claim, plan, job, pdf, tiff, configuration_for=configured,
                            rule=_layout_rule(engine, job_id), seal=KeySeal(store.configuration), now=now,
                            deadline=deadline_for(claim), mailbox_id=mailbox_id)


def measure_document(engine, values, claim, plan, job, pdf, tiff, *, configuration_for, rule=None, seal=None,
                     now=None, deadline=None, mailbox_id=None):
    """``Joint`` for one document: each calling account in ``plan`` (and any its cost cap left out on the
    page-count price) measured and priced on its own tariff, from the same pages. The worker (``measure``) and the
    document preview (``predict_http``) both come here, so for the same document, rules, tariffs and recipient they
    choose the same account and pages. ``claim`` names the attempt (its ``job_id`` and ``attempt_id`` name the
    caches beside ``pdf``); ``tiff`` is the fax image for an account whose engine sends one. ``deadline`` (a
    ``_clock`` time): measuring stops there, keeps what it measured for the account that is bound, and the
    accounts are ranked by page count (``limit`` 'deadline'); its raster and coding work stays cached for the
    next claim."""
    import sqlalchemy as sa
    from .. import conversion
    from ..pages import coding, packing, sending
    from .database import DeliveryStoreError
    from .predict import ShapeRefused
    from .routes import RouteUnavailable
    cache = sending.RasterCache(Path(str(pdf)).parent, claim.job_id, claim.attempt_id)
    keys = [choice.route.key for choice in plan.choices if choice.route.kind == 'provider']
    keys += [key for key, why in getattr(plan, 'skipped', ()) if why in ('over_cap', 'unknown_cost')]
    recipient = plan.destination
    found = Joint()
    for key in dict.fromkeys(keys):
        if deadline is not None and _clock() >= deadline:
            logging.getLogger(__name__).warning(
                'Fax %s: measuring its pages for every account would outlast its claim, so its accounts are ranked '
                'by page count after %d of %d.', claim.job_id, len(found.frontiers), len(dict.fromkeys(keys)))
            return Joint(frontiers=found.frontiers, limit='deadline')
        try:
            configuration = configuration_for(key)
        except RouteUnavailable:
            continue  # the route choice skips an account that cannot be set up, as before
        number = plan.number_for(key)
        # The pages go to the number this account calls; the recipient's own settings still count.
        dialed = {**job, 'to_number': number, 'recipient_number': recipient}
        # The same fax image the captured transport hands this account (outbound_transport.CapturedTransport).
        image = tiff if configuration.traits.get('requires_tiff') is True else None
        try:
            account = sending.account_for(engine, values, configuration, number, key=key, now=now,
                                          mailbox_id=mailbox_id)
            evaluated = sending.evaluate(engine, values, account, claim, dialed, pdf, image, rule=rule, seal=seal,
                                         now=now, cache=cache, compare=True)
        except (conversion.DocumentConversionError, OSError, coding.CodingRefused, packing.NotPackable,
                ShapeRefused, DeliveryStoreError, sa.exc.SQLAlchemyError) as error:
            # Accounts priced partly from measured pages and partly from the page count are not compared: the
            # route choice keeps its page-count order, and the cause is logged.
            logging.getLogger(__name__).warning('Fax %s: its pages could not be measured for account %s (%s); its '
                                                'accounts are ranked by page count.', claim.job_id, key, error)
            return Joint(limit='unmeasured')
        if evaluated is None:
            return Joint(limit='unmeasured')
        found.frontiers[key] = evaluated
        found.measured[key] = (evaluated.shape, account.facts)
    if len(found.frontiers) < 2:
        return Joint(frontiers=found.frontiers, limit='one_account')
    return found
