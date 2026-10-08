"""The delivery worker's path for one call that carries several faxes.

A claim with ``members`` is a shared call. Before anything is sent it checks
that the call can go as planned: the faxes' own SIP trunk is still the route
this number's faxes take, sending together still saves money there, and the
fax engine is connected. Otherwise the call is split (``BatchSplit``) and each
fax goes through the ordinary routing path on its own; nothing is failed. Each
fax's route decision is recorded as ``RoutedTransport`` records one. A claim
without members is handed to the ordinary routed transport unchanged.
"""
from contextlib import AsyncExitStack, asynccontextmanager
import logging

from ..config_runtime import run_lifecycle_step
from ..outbound_worker import BatchSplit, PreparationFailure
from . import policy


class BatchingTransport:
    def __init__(self, routed):
        self.routed = routed
        self.inner = routed.inner
        self.store = routed.store

    def _check(self, claim):
        """Raise ``BatchSplit`` unless the shared call can go over the faxes' own SIP trunk now."""
        from ..routing.plan import RoutePlanner
        revision, profile, job = self.store.load_dispatch(claim)
        configuration = profile.configuration
        ami = getattr(self.inner, 'ami', None)
        if (configuration.manifest is not None or configuration.provider_id != 'sip'
                or ami is None or not ami._connected.is_set()):
            raise BatchSplit()
        routes = self.routed.routes()
        plan = RoutePlanner(routes).plan(to_number=job['to_number'], bound='sip', values=revision.values,
                                         pages=job.get('pages'), alternates=True)
        choice = plan.first
        verdict = policy.verdict(choice, choice.route.card if choice.route.kind != 'direct' else None,
                                 preset=getattr(revision.values, 'sip_trunk_preset', None))
        if not verdict.saves or not choice.route.bound:
            raise BatchSplit()
        if not policy.header_identifies_sender(revision.values):
            from .store import call_members
            members = call_members(self.store.configuration.engine, claim.attempt_id)
            if members and members[0].get('layout') == policy.LAYOUT_PAGE_HEADERS:
                raise BatchSplit()  # 47 CFR 68.318(d): the header this call would print does not name the sender
        return plan, choice

    def _record(self, claim, plan, choice):
        routes = self.routed.routes()
        for member in claim.everyone:
            routes.record_decision(attempt_id=member.attempt_id, job_id=member.job_id, destination=plan.destination,
                                   route=choice.route.key, reason=choice.reason, provider_id='sip')

    @asynccontextmanager
    async def prepare(self, claim):
        if not claim.members:
            async with self.routed.prepare(claim) as operation:
                yield operation
            return
        plan, choice = await run_lifecycle_step(lambda: self._check(claim))
        try:
            await run_lifecycle_step(lambda: self._record(claim, plan, choice))
        except Exception:
            logging.getLogger(__name__).warning('Route evidence could not be recorded for a shared call.')
        async with AsyncExitStack() as stack:
            try:
                operation = await stack.enter_async_context(self.inner.prepare(claim))
            except PreparationFailure:
                # Nothing was sent; each fax goes on its own and meets the ordinary checks there.
                raise BatchSplit() from None
            yield operation


def call_image(store, root, claim, lighten=None):
    """Make the one image a shared call sends; ``BatchSplit`` when it cannot be made. ``lighten``: each fax's
    lightened pages for this call (pages/friendly.py call_lightener), or None."""
    from .image import (CallImageError, MemberUnusable, build_call_image, index_entry, index_heading, page_mark,
                        separator_line)
    from .store import call_members
    members = call_members(store.configuration.engine, claim.attempt_id)
    expected = [member.job_id for member in claim.everyone]
    if [member['id'] for member in members] != expected:
        raise BatchSplit()
    lines = [(member['id'], member['pages'],
              separator_line(member['document_number'], member['documents'], member['reference'],
                             member['pages'], member['sender_name'])) for member in members]
    index = marks = None
    if members[0].get('layout') == policy.LAYOUT_INDEX_PAGE:
        # The page ranges printed are the ones stored when the call was formed, which outcomes map against.
        index = (*index_heading(len(members), members[-1]['last_page']),
                 [index_entry(member['document_number'], member['first_page'], member['last_page'],
                              member['reference'], member['pages'], member['sender_name']) for member in members])
    elif members[0].get('layout') == policy.LAYOUT_PAGE_HEADERS:
        marks = [[page_mark(member['document_number'], member['documents'], page, member['pages'],
                            member['reference'], member['sender_name']) for page in range(1, member['pages'] + 1)]
                 for member in members]
    try:
        return build_call_image(root, claim.attempt_id, lines, index=index, marks=marks, lighten=lighten)
    except MemberUnusable as error:
        # That fax goes on its own (and fails there if its document is really gone); the rest go together.
        raise BatchSplit({error.job_id}) from None
    except CallImageError:
        raise BatchSplit() from None
