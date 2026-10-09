"""Digital routes for the route planner: candidates, prices, and the route the delivery worker sends by.

``candidates(engine, values, destination, pages, ...)`` lists, for WP-C's
planner (``routing/plan.py``), each confirmed Direct address and FHIR endpoint
of the recipient whose account is on and set up, priced through the shared
predictor (``routing.predict.predict_from``) by the account's plan. A missing
plan is an unknown cost, never zero. Keys are ``dsm:<id>`` and ``fhir:<id>``;
a rule can name one, name them all as ``digital``, or forbid them. A key a
fax's rules name that has no usable address now is reported as skipped
(``unavailable``), never silently dropped. A FHIR endpoint whose server needs
the patient (``digital/patient.py``) is skipped as ``needs_patient`` for a fax
that lacks the details, so that fax goes by fax.

``DigitalRoute`` has the relay route's contract (``prepare(claim, plan, job,
choice)``): its submission raises ``DirectRefused`` when nothing reached the
HISP or FHIR server, so the fax may still go by its own route in the same
attempt; anything that may have arrived is uncertain and waits for a person.
"""
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import re

from ..config_runtime import run_lifecycle_step
from ..routing.database import utcnow
from . import accounts as digital_accounts
from .store import DigitalStore
from .text import KIND_LABELS, address_label, ledger_key, parse_key, route_key


ACCOUNT_KIND = {'direct': 'hisp', 'fhir': 'fhir'}


@dataclass(frozen=True)
class DigitalCandidate:
    key: str
    kind: str                # 'direct' | 'fhir'
    address_id: str
    address: str
    organization: str | None
    account_key: str
    label: str
    price: object            # routing.pricing.Price

    @property
    def ledger_key(self):
        return ledger_key(self.key)


def _micros(text):
    from ..routing.costs import parse_amount
    return parse_amount(text, whole_digits=5) if text not in (None, '') else None


def plan_terms(account, now=None):
    """The account's plan as ``RateTerms`` for the predictor's digital branch, or None when no price is on file."""
    from ..routing.costs import RateCard, RateTerms
    per_message = _micros(account.setting('price_per_message'))
    fee = _micros(account.setting('monthly_fee'))
    if per_message is None and fee is None:
        return None
    day = account.setting('price_date')
    try:
        captured = datetime.fromisoformat(day) if day else (now or utcnow())
    except ValueError:
        captured = now or utcnow()
    card = RateCard(id=None, provider_id=account.key, direction='outbound', label=account.label[:100],
                    currency=account.setting('currency') or 'USD', per_minute_micros=0, per_page_micros=0,
                    per_call_micros=per_message or 0, billing_increment_seconds=1, minimum_seconds=0,
                    source_url=account.setting('price_source') or None, captured_on=captured,
                    monthly_fee_micros=fee or None)
    included = account.setting('included_messages') or None
    return RateTerms(card, included_pages=included,
                     overage_page_micros=(per_message if included and per_message is not None else None))


def month_start(now=None):
    moment = now or utcnow()
    return moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def plan_sentence(account, store=None, now=None):
    """One sentence for the account's plan on Providers."""
    from ..routing.costs import money_text, plan_fee_text
    terms = plan_terms(account, now)
    if terms is None:
        return 'No price is on file, so Faxbot treats its cost as unknown.'
    card = terms.card
    unit = 'message' if account.kind == 'hisp' else 'document'
    parts = []
    if card.monthly_fee_micros:
        parts.append(f'{plan_fee_text(card.monthly_fee_micros, card.currency)} a month')
        if terms.included_pages:
            parts.append(f'{terms.included_pages} {unit}s included')
    if card.per_call_micros:
        parts.append(f'{money_text(card.per_call_micros, card.currency)} a {unit}'
                     + (' past those' if terms.included_pages else ''))
    elif not card.monthly_fee_micros:
        parts.append(f'no charge for each {unit}')
    sentence = ', '.join(parts)
    sentence = sentence[0].upper() + sentence[1:] + '.'
    if card.source_url:
        day = card.captured_on
        sentence += f' Source: {card.source_url}, read {day.day} {day:%B %Y}.'
    return sentence


def price(engine, values, account, key, destination, pages, *, now=None, store=None):
    """The ``routing.pricing.Price`` of one message on ``account``, from the shared predictor."""
    from ..routing.destinations import classify
    from ..routing.pricing import Price
    from ..routing.predict import PlanUse, RouteFacts, Shape, predict_from
    moment = now or utcnow()
    terms = plan_terms(account, moment)
    used = None
    if terms is not None and terms.included_pages:
        used = (store or DigitalStore(engine)).count_since(account.key, month_start(moment))
    facts = RouteFacts(route_key=key, label=account.label, destination=classify(
        destination, getattr(values, 'fax_default_country', 'US') or 'US'), terms=terms,
        plan=PlanUse(faxes=used), currency=terms.card.currency if terms is not None else 'USD')
    prediction = predict_from(facts, Shape(max(int(pages or 1), 1), None, 'standard', 'normal'))
    cost = prediction.cost
    micros = cost.micros if cost is not None else None
    return Price(key, micros, cost.currency if cost is not None else None,
                 in_plan=bool(prediction.marginal and micros == 0), sentence=prediction.basis)


def _usable(account, current, store):
    """None when the account can send now, else why not (``routing.envelope.SKIPS``)."""
    if account is None:
        return 'unavailable'
    now_account = digital_accounts.digital_account(current, account.key) if current is not None else account
    if now_account is None or not now_account.enabled or not account.enabled:
        return 'turned_off'
    bundle = store.bundle(account.key) if account.kind == 'hisp' else None
    state, _ = digital_accounts.health(account, bundle=bundle)
    return None if state == 'ready' else 'not_ready'


def _lacks_patient(account, values, job_id):
    """Whether the fax ``job_id`` lacks the patient details the FHIR client's server needs (never for a dry run)."""
    from . import patient as fax_patient
    mode = fax_patient.mode_of(account)
    if job_id is None or mode == 'optional':
        return False
    try:
        found = fax_patient.read(getattr(values, 'fax_data_dir', None) or '.', job_id)
    except fax_patient.PatientUnreadable:
        found = None
    return found is None or found.missing_for(mode)


def candidates(engine, values, destination, pages, *, pinned=None, current=None, now=None, store=None, job_id=None):
    """(DigitalCandidate list cheapest first, skipped ``(key, why)``) for a fax to ``destination`` now; ``job_id``
    names the fax when it is a real one (not a preview), so a FHIR server that needs a patient can be skipped."""
    from ..rules import model
    if pinned is not None and (pinned.envelope.require_direct or any(
            item.account == model.DIGITAL and item.why == 'never' for item in pinned.decision.excluded)):
        return [], []
    store = store or DigitalStore(engine)
    found, skipped = [], []
    confirmed = store.confirmed_for(destination)
    for view in confirmed:
        key = route_key(view['kind'], view['id'])
        if pinned is not None and not pinned.allows(key):
            continue
        kind = ACCOUNT_KIND[view['kind']]
        account = (digital_accounts.digital_account(values, view['account_key']) if view['account_key']
                   else digital_accounts.default_account(values, kind))
        if account is not None and account.kind != kind:
            account = None
        why = _usable(account, current, store)
        if why is None and view['kind'] == 'fhir' and _lacks_patient(account, current or values, job_id):
            why = 'needs_patient'
        if why is not None:
            skipped.append((key, why))
            continue
        cost = price(engine, values, account, key, destination, pages, now=now, store=store)
        found.append(DigitalCandidate(key, view['kind'], view['id'], view['address'], view['organization'],
                                      account.key, address_label(view['kind'], view['address'],
                                                                 view['organization']), cost))
    if pinned is not None:
        # A key the rules name whose address is no longer confirmed is unavailable, never silently dropped.
        offered = {route_key(view['kind'], view['id']) for view in confirmed}
        for key in pinned.envelope.accounts:
            if parse_key(key) is not None and key not in offered:
                skipped.append((key, 'unavailable'))
    found.sort(key=lambda item: (item.price.micros is None, item.price.currency or '', item.price.micros or 0,
                                 item.kind != 'direct', item.address))
    return found, skipped


# The route the delivery worker sends by ----------------------------------------------------------------------------

class DigitalRoute:
    """Used when the planner chose ``dsm:<id>`` or ``fhir:<id>``: sends one Direct message or FHIR document."""

    def __init__(self, engine, *, values, transport=None, fhir_transport=None):
        self.engine = engine
        self.values = values                    # callable: the configuration in force now
        self.transport = transport              # Direct: SMTP and certificate discovery (direct_message.Transport)
        self.fhir_transport = fhir_transport    # FHIR: HTTP (fhir.Transport)

    def ready(self):
        return True

    @asynccontextmanager
    async def prepare(self, claim, plan, job, choice=None, *, revision_values=None):
        from ..routing.transport import DirectRefused
        route = getattr(choice, 'route', None)
        parsed = parse_key(getattr(route, 'key', None))
        if parsed is None:
            raise DirectRefused('The digital route was not named.')
        kind, address_id = parsed
        values = revision_values if revision_values is not None else await run_lifecycle_step(self.values)
        current = await run_lifecycle_step(self.values)
        store = DigitalStore(self.engine)
        view = await run_lifecycle_step(lambda: store.address(address_id))
        if view is None or view['state'] != 'confirmed':
            raise DirectRefused(f'This {KIND_LABELS[kind]} address is no longer confirmed; nothing was sent.')
        account = (digital_accounts.digital_account(values, view['account_key']) if view['account_key']
                   else digital_accounts.default_account(values, ACCOUNT_KIND[kind]))
        why = await run_lifecycle_step(lambda: _usable(account, current, store))
        if why is not None:
            raise DirectRefused('The account for this route cannot send now; nothing was sent.')
        pdf = Path(current.fax_data_dir) / (claim.job_id + '.pdf')
        if re.fullmatch('[a-f0-9]{32}', claim.job_id) is None or pdf.is_symlink() or not pdf.is_file():
            raise DirectRefused('The fax document is unavailable; nothing was sent.')
        document = await run_lifecycle_step(pdf.read_bytes)
        patient = None
        if kind == 'fhir':
            # The patient given with the fax, kept beside its document (digital/patient.py); document content.
            from . import patient as fax_patient
            try:
                patient = await run_lifecycle_step(lambda: fax_patient.read(current.fax_data_dir, claim.job_id))
            except fax_patient.PatientUnreadable:
                raise DirectRefused("The fax's patient details cannot be read; nothing was sent.") from None
        private = lambda: bool(getattr(current, 'direct_allow_private_peers', False))  # noqa: E731
        if kind == 'direct':
            from .direct_message import DirectSender, Transport
            sender = DirectSender(store, account, transport=self.transport or Transport(allow_private=private))
        else:
            from .fhir import FhirSender, Transport
            sender = FhirSender(store, account, transport=self.fhir_transport or Transport(allow_private=private))
        from . import certificates, smime
        try:
            submission = await run_lifecycle_step(lambda: sender.prepare(
                claim=claim, job=job, view=view, document=document, values=current,
                **({'patient': patient} if kind == 'fhir' else {})))
        except (certificates.CertificateRefused, smime.SmimeError) as refusal:
            # The account's own certificate, key or trust bundle cannot be used: nothing was built or sent.
            raise DirectRefused(f'{refusal} Nothing was sent.') from None
        yield submission


def build_route(store_engine, values, environment=None):
    """The installation's digital route for ``routing.transport.RoutedTransport``."""
    return DigitalRoute(store_engine, values=values)

