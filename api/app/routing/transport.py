"""Choose each attempt's route, then hand it to the transport that owns that route.

The delivery worker's lease, durable submission marker and ambiguity handling
are unchanged: this wrapper only decides which prepared operation the worker
submits once. An extra provider route is bound to the attempt before the
submission marker, so results are accepted only from the account that sent it.
A direct delivery that is refused before any document bytes leave falls back to
the conventional route inside the same submission; an outcome that may have
reached the partner raises, so the worker records the attempt as uncertain and
the partner is asked instead of sending again.

A fax accepted under sending rules goes only by a route its envelope allows
(``routing.envelope``). Every drop-back to the fax's own account (the default
sending account it was accepted with) checks the envelope first; when nothing
the envelope allows can take the fax, it is held in Sent with one sentence
(``OutboundStore.hold_no_route``) and nothing is sent.
"""
from contextlib import AsyncExitStack, asynccontextmanager
import logging

from ..config_runtime import run_lifecycle_step
from ..outbound_worker import CapacityWait, PreparationFailure
from dataclasses import replace

from .alternates import attempt_number, claim_dial_state
from .dialing import reaches
from .policy import RouteCandidate
from .plan import RoutePlan, RoutePlanner, ledger_key
from .routes import RouteUnavailable, ensure_route_artifact, route_ready
from .store import RouteStore
from . import envelope as envelopes, holds as hold_store


class RecordDeferred(CapacityWait):
    """The attempt's route choice could not be recorded just now (the database was busy): nothing was sent, and the
    fax is given back to be chosen again in a moment, never failed for it."""

    def __init__(self):
        super().__init__(seconds=5)


class RouteHeld(CapacityWait):
    """Nothing the fax's rules allow can take it now: the delivery store already gave the claim back and holds the
    fax in Sent. The worker's capacity handling then finds nothing left to give back."""

    def __init__(self):
        super().__init__(seconds=5)


def _pinned(plan):
    """The plan's routing decision (``routing.envelope.Pinned``), or None for a fax without one."""
    return getattr(plan, 'pinned', None)


def _skipped(plan):
    return tuple(getattr(plan, 'skipped', ()) or ())


def _account_route_configuration(revision, key):
    """The ProviderConfiguration for sending by account ``key`` (``accounts.route_configuration``)."""
    from ..accounts import route_configuration
    return route_configuration(revision, key)


class DirectRefused(RuntimeError):
    """The partner definitely did not receive the document; nothing was accepted."""


class _Planned(tuple):
    """``(plan, job, revision)``, as ``RoutedTransport._plan`` always returned, with the accepted ``profile`` and the
    ``prices`` it ranked by as attributes (a plan made elsewhere, such as a test's, has neither: None)."""

    def __new__(cls, plan, job, revision, profile=None, prices=None):
        found = super().__new__(cls, (plan, job, revision))
        found.profile, found.prices = profile, prices
        return found


def _job_mailbox(job_id):
    """The fax's sending mailbox, as the call reads it (``ami.job_mailbox``: None for no mailbox, or when it cannot
    be read; the call then shows the organization's number and is priced at it)."""
    from ..ami import job_mailbox
    return job_mailbox(job_id)


def _extras(planned):
    return getattr(planned, 'profile', None), getattr(planned, 'prices', None)


class _Unexpected(Exception):
    """Carries an error measuring did not document past the route choice's fallback (its ``__cause__``)."""


def _unexpected_raises(operation):
    try:
        return operation()
    except CapacityWait:
        raise
    except Exception as error:
        raise _Unexpected() from error


class _HandedOver:
    """An inner transport's preparation, entered while the route choice's handoff (``routing.joint.Handoff``: the
    account the attempt was bound to and its measured pages) is visible to it."""

    def __init__(self, context, handoff, record=None):
        self.context, self.handoff, self.record = context, handoff, record

    async def __aenter__(self):
        from . import joint
        with joint.handing_over(self.handoff):
            operation = await self.context.__aenter__()
        if self.record is not None and self.handoff.published.get('evaluated') is not None:
            # The account, its pages and their price go with the attempt once its preparation succeeded, before the
            # submission marker: a fallback to the fax's own account records that account instead.
            try:
                await run_lifecycle_step(lambda: self.record(self.handoff))
            except BaseException as error:
                await self.context.__aexit__(type(error), error, error.__traceback__)
                raise
        return operation

    async def __aexit__(self, *exc):
        return await self.context.__aexit__(*exc)


class _RoutedOperation:
    def __init__(self, transport, claim, plan, direct, conventional):
        self.transport, self.claim, self.plan = transport, claim, plan
        self.direct, self.conventional = direct, conventional

    async def submit(self):
        if self.direct is not None:
            try:
                return await self.direct.submit()
            except DirectRefused:
                if self.conventional is None:
                    raise
                # Nothing reached the partner, so the fax route is a first send.
                await run_lifecycle_step(lambda: self.transport.record_fallback(self.claim, self.plan))
        return await self.conventional.submit()


_AUTOMATIC = object()


def _installation_direct_route(inner):
    """The installation's direct route, built from the captured transport's runtime."""
    runtime = getattr(inner, 'runtime', None)
    store = getattr(getattr(runtime, 'manager', None), 'store', None)
    if store is None:
        return None
    try:
        from ..direct.service import DirectRoute, DirectService
        return DirectRoute(DirectService(store.engine, values=lambda: store.read().active.values,
                                         environment=getattr(runtime, 'environment', {})))
    except Exception:
        logging.getLogger(__name__).warning('Direct delivery is unavailable; faxes use their providers.')
        return None


def _installation_relay_route(inner):
    """The installation's partner relay route (``direct/relay_route.py``), built like the direct route."""
    runtime = getattr(inner, 'runtime', None)
    store = getattr(getattr(runtime, 'manager', None), 'store', None)
    if store is None:
        return None
    try:
        from ..direct.relay_route import RelayRoute
        from ..direct.service import DirectService
        from ..outbound_store import OutboundStore
        return RelayRoute(DirectService(store.engine, values=lambda: store.read().active.values,
                                        environment=getattr(runtime, 'environment', {})),
                          delivery=lambda: OutboundStore(store))
    except Exception:
        logging.getLogger(__name__).warning('Partner relays are unavailable; faxes use their providers.')
        return None


def _installation_digital_route(inner):
    """The installation's Direct message and FHIR route (``digital/routes.py``), built like the relay route."""
    runtime = getattr(inner, 'runtime', None)
    store = getattr(getattr(runtime, 'manager', None), 'store', None)
    if store is None:
        return None
    from ..digital.routes import DigitalRoute
    return DigitalRoute(store.engine, values=lambda: store.read().active.values)


class _DigitalOperation:
    """A Direct message or FHIR document, with the fax's own route behind it when nothing left Faxbot."""

    def __init__(self, transport, claim, plan, digital, conventional):
        self.transport, self.claim, self.plan = transport, claim, plan
        self.digital, self.conventional = digital, conventional

    async def submit(self):
        from ..outbound_worker import SubmissionReceipt
        try:
            return await self.digital.submit()
        except DirectRefused as refusal:
            if self.conventional is not None:
                # Nothing reached the HISP or FHIR server, so the fax route is a first send.
                await run_lifecycle_step(lambda: self.transport.record_fallback(self.claim, self.plan))
                return await self.conventional.submit()
            # A definite refusal is a failure, never an uncertain attempt; the fax may go by its next route.
            sentence = (str(refusal) or 'The digital route did not take this fax; nothing was sent.')[:200]
            return SubmissionReceipt(None, 'failed', error=sentence)


class RoutedTransport:
    def __init__(self, inner, *, direct=_AUTOMATIC, route_store=None, local=None, relay=_AUTOMATIC,
                 digital=_AUTOMATIC):
        """``local`` delivers faxes to the installation's own numbers inside Faxbot (``routing.local``);
        ``relay`` sends a fax through a partner's relay when the plan chose ``relay:<partner>``; ``digital``
        sends it as a Direct message or FHIR document when the plan chose ``dsm:<id>`` or ``fhir:<id>``."""
        self.inner = inner
        self.store = inner.store
        self.direct = _installation_direct_route(inner) if direct is _AUTOMATIC else direct
        self.relay = _installation_relay_route(inner) if relay is _AUTOMATIC else relay
        self.digital = _installation_digital_route(inner) if digital is _AUTOMATIC else digital
        self.local = local
        self._route_store = route_store

    def routes(self):
        if self._route_store is None:
            self._route_store = RouteStore(self.store.configuration.engine)
        return self._route_store

    def _pinned(self, claim):
        """The fax's routing decision, or None for a fax accepted before rules. Raises UnreadableDecision."""
        return envelopes.load(self.store.configuration.engine, claim.job_id)

    def _bound_key(self, revision, profile):
        try:
            from ..accounts import default_sending_key
            return default_sending_key(revision.values) or profile.configuration.provider_id
        except Exception:
            return profile.configuration.provider_id

    def _bound_for(self, revision, profile, pinned):
        """The fax's own account key for planning. Under sending rules, the default sending account (as before);
        without rules, that account too when the fax was accepted with it (an extra account such as ``sinch-uk``
        set as the default), else the accepted provider's first account, as before."""
        if pinned is not None:
            return self._bound_key(revision, profile)
        provider = profile.configuration.provider_id
        try:
            from ..accounts import account_named, default_sending_key
            key = default_sending_key(revision.values)
            account = account_named(revision.values, key) if key and key != provider else None
        except Exception:
            return provider
        return key if account is not None and account.provider == provider else provider

    def _current(self):
        """The configuration in force now (an account turned off stops new attempts at once), or None."""
        try:
            return self.store.configuration.read().active.values
        except Exception:
            return None

    def _plan(self, claim, measured=None):
        """``(plan, job, revision)`` (``_Planned``, with the profile and prices). Every account is ranked by the shared predictor's price for this
        fax (``pricing.prices_for``), never the older 30 seconds plus 30 seconds a page; ``measured`` prices the
        accounts whose pages were measured (``routing.joint``) by those pages instead of the page count."""
        revision, profile, job = self.store.load_dispatch(claim)
        routes = self.routes()
        planner = RoutePlanner(routes, direct_ready=self.direct.ready if self.direct is not None else None,
                               local_ready=self.local.ready if self.local is not None else None)
        pinned = self._pinned(claim)
        bound = self._bound_for(revision, profile, pinned)
        # The number choice kept at acceptance; a route is left out only for the number it already called.
        dial = claim_dial_state(self.store, claim, job.get('dial'))
        from .pricing import prices_for
        # A queued fax under sending rules sees a scarce plan's room after the faxes it is held for (plan_allocation).
        # The mailbox the fax was sent from: its call presents that mailbox's reply number, and a price by caller ID
        # follows it (resolved exactly as the call resolves it, ami.job_mailbox).
        mailbox = _job_mailbox(claim.job_id)
        prices = prices_for(routes, revision.values, job['to_number'], job.get('pages'), pinned=pinned,
                            bound=bound, dial=dial, job_id=claim.job_id if pinned is not None else None,
                            measured=measured, mailbox_id=mailbox)
        plan = planner.plan(to_number=job['to_number'], bound=bound, values=revision.values,
                            pages=job.get('pages'), alternates=True, dial=dial,
                            tried=planner.tried(claim.job_id, claim.attempt_id),
                            by_call=bool(job.get('send_by_call')), pinned=pinned,
                            current=self._current() if pinned is not None else None, prices=prices,
                            job_id=claim.job_id)
        return _Planned(plan, job, revision, profile, prices)

    def _measure(self, claim, plan, job, revision, profile):
        """``routing.joint.Joint``: each account the plan may use, its pages measured and priced on its own tariff,
        when the plan ranks several accounts by cost; otherwise only why not."""
        from . import joint
        why = joint.compares(claim, plan) if isinstance(plan, RoutePlan) else 'one_account'
        if why is not None:
            return joint.Joint(limit=why)
        return joint.measure(self.store, revision, profile, claim, plan, job,
                             bound=self._bound_for(revision, profile, _pinned(plan)), mailbox_id=_job_mailbox(
                                 claim.job_id),
                             configuration_for=lambda key: _account_route_configuration(revision, key),
                             ensure_artifact=ensure_route_artifact)

    def _inner(self, claim, key, measured, plan=None, prices=None, record=False):
        """``self.inner.prepare(claim)``, handed the account it sends by, that account's measured pages and what
        the attempt's record keeps of the comparison (``routing.selections.summary``). ``record``: this operation
        is the attempt's own send, so its selection is recorded once its preparation succeeded."""
        from . import joint, selections
        selected = measured.selection(key) if measured is not None and key is not None else None
        summary = selections.summary(plan, measured, key, prices) if selected is not None else None
        handoff = joint.Handoff(claim.attempt_id, key, selected, summary, _job_mailbox(claim.job_id))
        return _HandedOver(self.inner.prepare(claim), handoff,
                           record=(lambda found: self._record_selection(claim, found)) if record else None)

    def _record_selection(self, claim, handoff):
        """Write the attempt's selection (``routing.selections.record``) before the submission marker. A database
        that cannot take it just now (locked, unreachable) gives the fax back to wait a moment, nothing sent and
        nothing failed (``RecordDeferred``); the file it names unreadable fails preparation, as a document would.
        Both are logged."""
        import sqlalchemy as sa
        from . import selections
        from .database import DeliveryStoreError
        published = handoff.published
        try:
            selections.record(self.store.configuration.engine, claim=claim, handoff=handoff,
                              evaluated=published['evaluated'], prepared=published.get('prepared'),
                              pdf=published.get('pdf'))
        except (DeliveryStoreError, sa.exc.SQLAlchemyError) as error:
            logging.getLogger(__name__).warning('Fax %s: its route choice could not be recorded just now, so it '
                                                'waits a moment; nothing was sent: %s', claim.job_id, error)
            raise RecordDeferred() from None
        except OSError as error:
            logging.getLogger(__name__).warning('Fax %s: the pages chosen for its route could not be read back, so '
                                                'nothing is sent: %s', claim.job_id, error)
            raise PreparationFailure('preparation_failed') from None

    def _record_dialed(self, claim, plan, route, dial, values):
        """Record the number this attempt calls on ``route`` before its durable submission marker.

        The same rule the plan priced the route by (``alternates.attempt_number``); a route with no call records none.
        """
        record = getattr(self.store, 'record_dialed', None)
        if record is None or route.kind != 'provider':
            return
        alternate = (dial or {}).get('alternate')
        number, _ = attempt_number(plan.destination, alternate=alternate, refused=bool((dial or {}).get('refused')),
                                   route_reaches=bool(alternate) and reaches(route.provider_id, alternate, values))
        approvals = (dial or {}).get('approvals') if number != plan.destination else None
        record(claim, number, approvals or None)

    def _trunk_has_room(self, claim, revision, key='sip'):
        """Whether the account ``key`` (a trunk, or an account with a "faxes at once" limit) can take this fax now.

        The claim gate covers faxes bound to it and faxes whose rules allow nothing else; this covers the rest.
        """
        capacity = getattr(self.store, 'capacity', lambda: None)()
        if capacity is None:
            return True
        from datetime import datetime
        with self.store.configuration.engine.connect() as connection:
            room = capacity.room(connection, revision.values, datetime.utcnow(),
                                 exclude=[member.job_id for member in claim.everyone], trunk=key)
        return not (room.trunk_full or room.rate_full)

    @staticmethod
    def _limited(revision, key):
        """Whether ``key`` is an account whose calls or faxes at once are limited (capacity.limited_accounts)."""
        from ..capacity import limited_accounts
        try:
            return key in limited_accounts(revision.values)
        except Exception:
            return False

    def _assign(self, claim, plan, revision):
        """Bind the first usable provider route; the fax's own provider needs no change.

        A route over the trunk is used only while the trunk has room; otherwise the
        fax waits (``CapacityWait``) before any route is bound or anything is sent,
        unless its rule says to use the next allowed account when the trunk is
        busy (``when_busy: next``). What could not be used is kept on the plan's
        ``skipped`` list for the attempt's record.
        """
        pinned = _pinned(plan)
        skipped = list(_skipped(plan))
        from .sender_pins import dispatch_refusal
        from .after_answer import dispatch_refusal as needs_keys
        for place, choice in enumerate(plan.choices):
            route = choice.route
            if needs_keys(self.store.configuration.engine, revision.values, plan.destination, route.key):  # N7
                skipped.append((route.key, 'digits'))
                continue
            # A registered-sender recipient (sender_pins, N17): only its trunk, showing its registered identity.
            if dispatch_refusal(self.store.configuration.engine, revision.values, plan.destination, route.key):
                skipped.append((route.key, 'pin'))
                continue
            if route.kind in ('direct', 'local', 'relay', 'digital'):
                return self._chosen(plan, choice, skipped), claim
            # Each trunk (and each account with a "faxes at once" limit) has its own room (capacity.py).
            if (route.provider_id == 'sip' or self._limited(revision, route.key)) and not self._trunk_has_room(
                    claim, revision, route.key):
                if pinned is not None and pinned.envelope.when_busy == 'next':
                    skipped.append((route.key, 'busy'))
                    continue
                raise CapacityWait()
            if route.bound:
                if _pinned(plan) is not None and not self._bound_ready(claim):
                    # Under sending rules the fax's own account is checked like any other: with the trunk's engine
                    # down, the next allowed account takes the fax instead of a send that would fail.
                    skipped.append((route.key, 'not_ready'))
                    continue
                return self._chosen(plan, choice, skipped), claim
            try:
                configuration = _account_route_configuration(revision, route.key)
                if not route_ready(configuration, ami=getattr(self.inner, 'ami', None)):
                    skipped.append((route.key, 'not_ready'))
                    continue
                # Prepare what this route needs (a fax TIFF for SIP) before binding it.
                ensure_route_artifact(revision, configuration, claim.job_id)
                chosen = self._chosen(plan, choice, skipped)
                return chosen, self.store.assign_route(claim, configuration, account_key=route.key, choice={
                    'place': self._place(plan, route.key), 'skipped': tuple(skipped),
                    'unreliable': getattr(plan, 'unreliable', ())})
            except RouteUnavailable:
                skipped.append((route.key, 'unavailable'))
                continue
        self._skipped = tuple(skipped)
        if any(why == 'busy' for _, why in skipped):
            # Every allowed account that could take the fax is busy for now: it waits for room (it stays ready),
            # never held in Sent for a person; a line frees up by itself.
            raise CapacityWait()
        return None, claim

    def _bound_ready(self, claim):
        """Whether the fax's own (bound) account can take a call now: local readiness only, never a provider call."""
        try:
            _, bound = self.store.configuration.outbound_context(claim.job_id)
            return route_ready(bound.configuration, ami=getattr(self.inner, 'ami', None))
        except Exception:
            return False

    def _chosen(self, plan, choice, skipped):
        self._skipped = tuple(skipped)
        return choice

    @staticmethod
    def _place(plan, key):
        accounts = _pinned(plan).envelope.accounts if _pinned(plan) is not None else ()
        return accounts.index(key) if key in accounts else 0

    def _record(self, claim, plan, choice):
        # The ledger's grammar has no colon: a partner relay is recorded as relay.<partner> (direct.relay.ledger_key).
        self.routes().record_decision(attempt_id=claim.attempt_id, job_id=claim.job_id,
                                      destination=plan.destination, route=ledger_key(choice.route.key),
                                      reason=choice.reason, provider_id=choice.route.provider_id)
        if choice.reason == 'plan_reserved' and getattr(plan, 'held', None) is not None:
            # Why this fax did not use its plan, with the amounts as they were, for Sent details (plan_allocation).
            from .plan_allocation import record
            record(self.routes().engine, attempt_id=claim.attempt_id, job_id=claim.job_id, held=plan.held)

    def _record_choice(self, claim, plan, choice):
        """The route this attempt was given, with what was skipped, before anything is sent (rules only).

        ``assign_route`` records an account it binds; this records the fax's own account, delivery inside Faxbot,
        direct delivery and a partner relay. Without the record, a fax whose rules exclude its own account is
        refused at the submission marker: nothing goes out unrecorded.
        """
        if _pinned(plan) is None:
            return
        key = choice.route.key
        self.store.record_route_choice(claim, account_key=key, place=self._place(plan, key),
                                       skipped=_skipped(plan), unreliable=getattr(plan, 'unreliable', ()))

    def _bound_allowed(self, claim):
        """Whether the fax may go by its own account: no rules, or its rules allow it. False when unreadable."""
        try:
            pinned = self._pinned(claim)
            if pinned is None:
                return True
            revision, profile = self.store.attempt_context(claim.job_id, claim.attempt_id)
            return pinned.allows(self._bound_key(revision, profile))
        except Exception:
            return False

    def _hold(self, claim, plan, reason=None):
        """Hold the fax in Sent: nothing its rules allow can take it now. Raises RouteHeld."""
        if reason is None:
            labels = {}
            if plan is not None and _pinned(plan) is not None:
                try:
                    from ..accounts import sending_accounts
                    revision, _ = self.store.configuration.outbound_context(claim.job_id)
                    labels = {account.key: account.label for account in sending_accounts(revision.values)}
                except Exception:
                    labels = {}
            reason = hold_store.no_route_sentence(lambda key: labels.get(key) or key,
                                                  _skipped(plan) if plan is not None else ())
        self.store.hold_no_route(claim, reason=reason, skipped=_skipped(plan) if plan is not None else ())
        raise RouteHeld()

    def _restore_bound(self, claim, plan, revision):
        """An extra route failed local preparation; use the fax's own provider instead, when its rules allow it."""
        if plan is not None and _pinned(plan) is not None and not _pinned(plan).allows(
                self._bound_key(revision, self.store.attempt_context(claim.job_id, claim.attempt_id)[1])):
            self._hold(claim, plan, 'The account your rules chose could not prepare this fax, and no other account '
                                    'your rules allow could take it. It waits for you in Sent; nothing was sent.')
        bound = next((choice for choice in plan.choices if choice.route.bound), None)
        _, accepted = self.store.configuration.outbound_context(claim.job_id)
        restored = self.store.assign_route(claim, accepted.configuration)
        if bound is not None:
            self.routes().reroute_decision(claim.attempt_id, route=bound.route.key,
                                           provider_id=bound.route.provider_id, reason='alternative')
        # The fax's own provider calls the number it may call, recorded again before submission.
        _, _, job = self.store.load_dispatch(restored)
        route = RouteCandidate(accepted.configuration.provider_id, 'provider', accepted.configuration.provider_id)
        self._record_dialed(restored, plan, route, claim_dial_state(self.store, restored, job.get('dial')),
                            revision.values)
        return restored

    def record_fallback(self, claim, plan):
        # Only reached with a conventional route prepared, which exists only when the plan kept the fax's own account.
        bound = next(choice.route for choice in plan.choices if choice.route.bound)
        self.routes().reroute_decision(claim.attempt_id, route=bound.key, provider_id=bound.provider_id,
                                       reason='alternative')

    @asynccontextmanager
    async def prepare(self, claim):
        plan = choice = measured = prices = None
        assigned = claim
        bound_key = None
        try:
            planned = await run_lifecycle_step(lambda: self._plan(claim))
            plan, job, revision = planned
            profile, prices = _extras(planned)
            if profile is not None:
                bound_key = self._bound_for(revision, profile, _pinned(plan))
                # Measure each account's best pages before binding one, then rank the accounts again by those prices
                # with every rule, cap and preference as before (routing.joint).
                measured = await run_lifecycle_step(lambda: _unexpected_raises(
                    lambda: self._measure(claim, plan, job, revision, profile)))
                from . import selections
                await run_lifecycle_step(lambda: selections.note_measured(
                    self.store.configuration.engine, claim, measured))
                if measured.measured:
                    planned = await run_lifecycle_step(lambda: self._plan(claim, measured=measured.measured))
                    plan, job, revision = planned
                    profile, prices = _extras(planned)
            self._skipped = _skipped(plan)
            choice, assigned = await run_lifecycle_step(lambda: self._assign(claim, plan, revision))
            # What this attempt could not use, for its record and for "send anyway" on a held fax.
            if isinstance(plan, RoutePlan):
                plan = replace(plan, skipped=self._skipped)
            if choice is not None:
                dial = claim_dial_state(self.store, claim, job.get('dial'))
                await run_lifecycle_step(lambda: self._record_dialed(claim, plan, choice.route, dial, revision.values))
        except CapacityWait:
            raise
        except _Unexpected as error:
            # Measuring refuses what it documents (a document that cannot be drawn: accounts are then ranked by page
            # count). Anything else is a bug or an integration failure: it raises, and nothing is sent.
            raise error.__cause__
        except Exception:
            # Route evidence is optional; the accepted provider still works (and chooses its own number), unless
            # the fax's rules exclude it: then the fax waits in Sent instead of going outside its rules.
            if not await run_lifecycle_step(lambda: self._bound_allowed(claim)):
                logging.getLogger(__name__).warning('Route choice is unavailable; the fax waits for a route its '
                                                    'rules allow.')
                await run_lifecycle_step(lambda: self._hold(claim, None, (
                    'Faxbot could not choose a route your rules allow just now, so nothing was sent. It waits for '
                    'you in Sent; check again in a moment.')))
            logging.getLogger(__name__).warning('Route choice is unavailable; using the outbound provider.')
            plan = choice = measured = None
        if choice is None and plan is not None and _pinned(plan) is not None:
            # Nothing the rules allow can take the fax now, its own account included (owner's answer Q1).
            await run_lifecycle_step(lambda: self._hold(claim, plan))
        if choice is not None:
            try:
                await run_lifecycle_step(lambda: self._record_choice(claim, plan, choice))
            except Exception:
                # Without the record a fax whose rules exclude its own account is refused at submission.
                logging.getLogger(__name__).warning('The route choice could not be recorded for a fax.')
            try:
                await run_lifecycle_step(lambda: self._record(claim, plan, choice))
            except Exception:
                logging.getLogger(__name__).warning('Route evidence could not be recorded for a fax.')
        if choice is not None and choice.route.kind == 'local' and self.local is not None:
            async with AsyncExitStack() as stack:
                try:
                    operation = await stack.enter_async_context(self.local.prepare(claim, plan, job))
                except Exception:
                    # Nothing was recorded as received; the fax goes by its normal route as a first send, when its
                    # rules allow its own account.
                    if not any(c.route.bound for c in plan.choices):
                        await run_lifecycle_step(lambda: self._hold(claim, plan, (
                            'This fax could not be delivered inside Faxbot, and your rules allow no call for it. It '
                            'waits for you in Sent; nothing was sent.')))
                    await run_lifecycle_step(lambda: self.record_fallback(claim, plan))
                    operation = await stack.enter_async_context(
                        self._inner(claim, bound_key, measured, plan, prices, record=True))
                yield operation
            return
        if choice is not None and plan is not None and _pinned(plan) is not None and (
                (choice.route.kind == 'relay' and self.relay is None)
                or (choice.route.kind == 'digital' and self.digital is None)
                or (choice.route.kind == 'direct' and self.direct is None)) \
                and not await run_lifecycle_step(lambda: self._bound_allowed(claim)):
            # The route the rules allow cannot be prepared here, and the fax's own account is not allowed.
            await run_lifecycle_step(lambda: self._hold(claim, plan, (
                'The route your rules chose for this fax is not available on this Faxbot, and no other account your '
                'rules allow could take it. It waits for you in Sent; nothing was sent.')))
        if choice is not None and choice.route.kind == 'relay' and self.relay is not None:
            # A partner relays it as a local call; a signed refusal (nothing accepted) lets the fax's own
            # route send it in the same attempt, recorded as a fallback.
            async with AsyncExitStack() as stack:
                conventional = None
                if any(c.route.bound for c in plan.choices):
                    try:
                        conventional = await stack.enter_async_context(
                            self._inner(claim, bound_key, measured, plan, prices))
                    except PreparationFailure:
                        conventional = None
                try:
                    relayed = await stack.enter_async_context(self.relay.prepare(claim, plan, job, choice))
                except Exception:
                    if conventional is None and _pinned(plan) is not None:
                        # Nothing was sent: under sending rules the fax waits in Sent rather than failing.
                        await run_lifecycle_step(lambda: self._hold(claim, plan, (
                            'The partner relay your rules chose could not take this fax, and no other account '
                            'your rules allow could. It waits for you in Sent; nothing was sent.')))
                    if conventional is None:
                        raise PreparationFailure('provider_unavailable') from None
                    await run_lifecycle_step(lambda: self.record_fallback(claim, plan))
                    yield conventional
                    return
                yield _RoutedOperation(self, claim, plan, relayed, conventional)
            return
        if choice is not None and choice.route.kind == 'digital' and self.digital is not None:
            # A Direct message or FHIR document: a definite refusal (nothing left Faxbot) lets the fax's own route
            # send it in the same attempt, recorded as a fallback; anything that may have arrived is uncertain.
            async with AsyncExitStack() as stack:
                conventional = None
                if any(c.route.bound for c in plan.choices):
                    try:
                        conventional = await stack.enter_async_context(
                            self._inner(claim, bound_key, measured, plan, prices))
                    except PreparationFailure:
                        conventional = None
                try:
                    sending = await stack.enter_async_context(self.digital.prepare(
                        claim, plan, job, choice, revision_values=revision.values))
                except DirectRefused as refusal:
                    logging.getLogger(__name__).info('A digital route could not start: %s', refusal)
                    if conventional is None and _pinned(plan) is not None:
                        await run_lifecycle_step(lambda: self._hold(claim, plan, (
                            'The Direct or FHIR route your rules chose could not take this fax, and no other account '
                            'your rules allow could. It waits for you in Sent; nothing was sent.')))
                    if conventional is None:
                        raise PreparationFailure('provider_unavailable') from None
                    await run_lifecycle_step(lambda: self.record_fallback(claim, plan))
                    yield conventional
                    return
                yield _DigitalOperation(self, claim, plan, sending, conventional)
            return
        if choice is None or choice.route.kind != 'direct' or self.direct is None:
            # The account this attempt goes by, handed its own measured pages when accounts were compared.
            key = choice.route.key if choice is not None and choice.route.kind == 'provider' else bound_key
            async with AsyncExitStack() as stack:
                try:
                    operation = await stack.enter_async_context(
                        self._inner(assigned, key, measured, plan, prices, record=True))
                except PreparationFailure:
                    if assigned is claim:
                        raise
                    assigned = await run_lifecycle_step(lambda: self._restore_bound(claim, plan, revision))
                    operation = await stack.enter_async_context(
                        self._inner(assigned, bound_key, measured, plan, prices, record=True))
                yield operation
            return
        async with AsyncExitStack() as stack:
            conventional = None
            if any(c.route.bound for c in plan.choices):
                try:
                    conventional = await stack.enter_async_context(
                        self._inner(claim, bound_key, measured, plan, prices))
                except PreparationFailure:
                    conventional = None  # Direct delivery can still proceed alone.
            try:
                direct = await stack.enter_async_context(self.direct.prepare(claim, plan, job))
            except Exception:
                # Nothing was sent; the conventional route is still a first send.
                if conventional is None and _pinned(plan) is not None:
                    await run_lifecycle_step(lambda: self._hold(claim, plan, (
                        'Direct delivery to the partner could not start, and your rules allow no call for this fax. '
                        'It waits for you in Sent; nothing was sent.')))
                if conventional is None:
                    raise PreparationFailure('provider_unavailable') from None
                await run_lifecycle_step(lambda: self.record_fallback(claim, plan))
                yield conventional
                return
            yield _RoutedOperation(self, claim, plan, direct, conventional)
