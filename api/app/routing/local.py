"""Deliver a fax to one of the installation's own receiving numbers inside Faxbot, without a call.

The sent fax's stored document becomes a received fax on the same installation
through the same acquisition records as any received fax (source ``local``), so
mailbox rules and email delivery treat it alike and Received says where it came
from. No provider or carrier is contacted.

Own numbers are the numbers that receive into this Faxbot, as
``own_numbers.receiving_numbers`` defines them for every use (the plan check
counts the wider "an account number of yours" with ``account_numbers``). A
HumbleFax account number is not one: HumbleFax cannot receive into Faxbot, so a
fax to it still places a real call and lands in the HumbleFax inbox.

Delivery is idempotent on the sent fax: the received-fax record is keyed on the
job id, so a retry, a restart or a later attempt never makes a second received
fax. A failure before that record is complete is a definite failure, and the
fax may then go by its normal route; once it is complete, the send is delivered.
"""
from contextlib import asynccontextmanager
import json
import logging
from pathlib import Path
import re

import sqlalchemy as sa

from ..config_runtime import run_lifecycle_step
from ..outbound_worker import SubmissionReceipt
from .database import read_connection, utcnow
from .numbers import InvalidNumber, normalize_number
from .own_numbers import receiving_numbers
from .store import destination_key


LOCAL = 'local'
SOURCE = 'local'
ACCOUNT = 'local:installation'
REASON = 'own_number'


def display_number(number):
    """"+1 720-856-5062" for "+17208565062"; the number unchanged when it cannot be read."""
    try:
        import phonenumbers
        return phonenumbers.format_number(phonenumbers.parse(number, None),
                                          phonenumbers.PhoneNumberFormat.INTERNATIONAL)
    except Exception:
        return number


def own_numbers(values):
    """The installation's own receiving numbers, in E.164 (the one shared rule: ``own_numbers.py``)."""
    return receiving_numbers(values)


def applies(values, destination, *, by_call=False):
    """Whether a fax to ``destination`` is delivered inside Faxbot under these settings."""
    return (bool(getattr(values, 'local_delivery_enabled', True)) and not by_call
            and destination in own_numbers(values))


def sending_number(values, destination):
    """The number a fax delivered inside Faxbot shows as its sender: the trunk's caller ID, or this number."""
    country = getattr(values, 'fax_default_country', 'US')
    for candidate in (getattr(values, 'sip_trunk_caller_id', ''), getattr(values, 'direct_fax_number', '')):
        if candidate:
            try:
                return normalize_number(candidate, country=country)
            except InvalidNumber:
                continue
    return destination


class LocalRefused(RuntimeError):
    """Nothing was recorded as received; the fax may go by its normal route."""


class LocalDelivery:
    """Record one sent fax as a received fax on this installation; idempotent on the sent fax."""

    def __init__(self, resources, *, data_dir):
        """``resources()`` returns the access runtime's inbound resources; ``data_dir()`` the fax data folder."""
        self.resources, self.data_dir = resources, data_dir

    def _store(self):
        from ..inbound.acquisition import ImportStore
        return ImportStore(self.resources())

    def find(self, job_id):
        """The received-fax record made from this sent fax, or None."""
        store = self._store()
        imports = store.imports
        with store.engine.connect() as connection:
            row = connection.execute(sa.select(imports).where(
                imports.c.source == SOURCE, imports.c.account == ACCOUNT, imports.c.operation_id == job_id,
                imports.c.revision == '')).mappings().first()
        return dict(row) if row is not None else None

    def document(self, job_id):
        path = Path(self.data_dir()) / (job_id + '.pdf')
        if re.fullmatch('[a-f0-9]{32}', job_id) is None or path.is_symlink() or not path.is_file():
            raise LocalRefused('The fax document is unavailable, so it cannot be delivered inside Faxbot.')
        from ..conversion import DocumentConversionError, validate_pdf
        try:
            validate_pdf(str(path))  # an unreadable document refuses before anything is recorded
        except DocumentConversionError:
            raise LocalRefused('The fax document cannot be read, so it cannot be delivered inside Faxbot.') from None
        return path.read_bytes()

    def deliver(self, *, job_id, attempt_id, values, destination, pages):
        """Record the received fax; returns its id. Repeating it returns the same received fax."""
        from ..inbound.acquisition import discard, store_document
        record = self.find(job_id)
        if record is not None and record['state'] in ('received', 'conflict'):
            return record['inbound_fax_id']
        data = self.document(job_id)  # a missing document refuses before anything is recorded
        store = self._store()
        begun = store.begin(source=SOURCE, account=ACCOUNT, operation_id=job_id, backend=LOCAL,
                            inbound_backend=LOCAL, to_number=destination,
                            from_number=sending_number(values, destination), reported_pages=pages,
                            report={'sent_fax': job_id, 'attempt': attempt_id, 'route': LOCAL}, schedule=False,
                            # Faxbot itself is the source, so it knows when the fax arrived (evidence exports).
                            source_received_at=utcnow(), country=getattr(values, 'fax_default_country', 'US'))
        if begun.state in ('received', 'conflict'):
            return begun.inbound_fax_id
        try:
            artifact = store_document(data, begun.inbound_fax_id, provider='Faxbot')
        except Exception:
            # Nothing was received: the record says so instead of waiting forever, and a retry resumes it.
            store.abandon(begun.import_id, 'This fax could not go straight into Received, so Faxbot sent it '
                                           'with a normal phone call.')
            raise
        completion = store.complete(begun.import_id, artifact_path=artifact.path, digest=artifact.digest,
                                    size=artifact.size, pages=artifact.pages)
        discard(artifact, completion)
        return completion.inbound_fax_id

    def delivered(self, job_id):
        record = self.find(job_id)
        return record is not None and record['state'] in ('received', 'conflict')


class LocalRoute:
    """The delivery worker's route for a fax to one of the installation's own numbers."""

    def __init__(self, delivery, *, values, available=None):
        """``values()`` returns the active configuration values; ``available()`` whether received-fax records are open."""
        self.delivery, self.values = delivery, values
        self.available = available or (lambda: True)

    def ready(self):
        try:
            return self.delivery is not None and bool(self.available())
        except Exception:
            return False

    @asynccontextmanager
    async def prepare(self, claim, plan, job):
        await run_lifecycle_step(lambda: self.delivery.document(claim.job_id))  # refuses before anything is recorded
        yield _LocalSubmission(self, claim, plan, job)


class _LocalSubmission:
    def __init__(self, route, claim, plan, job):
        self.route, self.claim, self.plan, self.job = route, claim, plan, job

    async def submit(self):
        delivery, claim = self.route.delivery, self.claim
        values = await run_lifecycle_step(self.route.values)
        try:
            await run_lifecycle_step(lambda: delivery.deliver(
                job_id=claim.job_id, attempt_id=claim.attempt_id, values=values,
                destination=self.plan.destination, pages=self.job.get('pages')))
            return SubmissionReceipt(None, 'success')
        except Exception:
            # Delivered after all (a commit whose answer was lost) or definitely not: ask the records.
            if await run_lifecycle_step(lambda: delivery.delivered(claim.job_id)):
                return SubmissionReceipt(None, 'success')
            logging.getLogger(__name__).warning('A fax to one of your own numbers could not be delivered inside '
                                                'Faxbot; it goes by its normal route.')
            return SubmissionReceipt(None, 'failed')


class LocalReconciler:
    """Settle faxes to own numbers whose answer was lost: delivered if the received fax exists, else sent normally."""

    def __init__(self, delivery, outbound, routes, *, values):
        self.delivery, self.outbound, self.routes, self.values = delivery, outbound, routes, values

    def waiting(self, *, limit=20):
        deliveries, costs, jobs = self.outbound.deliveries, self.routes.costs, self.routes.jobs
        query = (sa.select(deliveries.c.id, deliveries.c.attempt_id, jobs.c.to_number, jobs.c.pages)
                 .select_from(deliveries.join(costs, costs.c.id == deliveries.c.attempt_id)
                              .join(jobs, jobs.c.id == deliveries.c.id))
                 .where(deliveries.c.state == 'reconciliation_required', costs.c.route == LOCAL)
                 .order_by(deliveries.c.updated_at, deliveries.c.id).limit(limit))
        with read_connection(self.routes.engine) as connection:
            return connection.execute(query).all()

    def step(self):
        settled = 0
        for job_id, attempt_id, to_number, pages in self.waiting():
            values = self.values()
            try:
                self.delivery.deliver(job_id=job_id, attempt_id=attempt_id, values=values,
                                      destination=destination_key(to_number, values.fax_default_country), pages=pages)
            except LocalRefused:
                if not self.delivery.delivered(job_id):
                    self.outbound.requeue_after_failure(job_id, attempt_id=attempt_id, category='local_not_delivered')
                    settled += 1
                continue
            _, profile = self.outbound.attempt_context(job_id, attempt_id)
            self.outbound.observe(job_id, attempt_id=attempt_id, profile_id=profile.id, provider_sid=None,
                                  status='success', event_key='local:' + job_id)
            settled += 1
        return settled > 0


def for_application(app, runtime):
    """This application's local delivery: received-fax records from its access runtime, documents in its data folder."""
    values = lambda: runtime.manager.store.read().active.values  # noqa: E731
    delivery = LocalDelivery(lambda: app.state.access_runtime.inbound, data_dir=lambda: values().fax_data_dir)
    return delivery, values, (lambda: getattr(app.state, 'access_runtime', None) is not None)


def installation_route(app, runtime):
    delivery, values, available = for_application(app, runtime)
    return LocalRoute(delivery, values=values, available=available)


def report(record):
    """The parsed report of a local received-fax record."""
    try:
        parsed = json.loads(record.get('report') or '{}')
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}
