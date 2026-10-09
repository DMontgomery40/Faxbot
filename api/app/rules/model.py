"""The frozen contract of sending rules: what a rule reads, what it decides, and the rules document's words.

Faxbot decides a fax's route once, when the fax is accepted. It reads a small
snapshot of facts about the fax (``Facts``), evaluates the active rules of each
scope that applies (the organization, the sending mailbox, the workflow) and
stores the result (``Decision``), whose ``Envelope`` is what delivery may use:
which accounts, in what order or by what method, under what cost cap and holds,
the number to dial and the page layout. At each attempt the existing readiness,
capacity, reliability and cost logic chooses inside the envelope, never outside.

The same facts, the same scope revisions and the same accounts always give the
same decision, so a decision can be replayed and explained later. Nothing here
holds a clock reading, a generated ID or a float: times are naive-UTC ISO
strings, money is integer micros of a currency, and the JSON form is canonical
(sorted keys, no spaces, ASCII).

Compatibility: ``FORMAT`` is 1. A later field is always optional with a
default, so a stored decision written by this release reads back unchanged.
Unknown keys in stored JSON are ignored when reading.

Rules documents
---------------
Each scope has one document, published as numbered, immutable revisions:

- organization: ``format``, ``lists``, ``labels``, ``regions``, ``sites``,
  ``workflows``, ``limits``, ``routes``;
- mailbox and workflow: ``format``, ``limits``, ``routes``.

``lists`` maps a recipient group's key to ``{"name", "numbers", "prefixes"}``;
``labels`` (top level, not inside ``lists``) is the list of labels senders may attach; ``regions`` maps a key to
``{"name", "countries", "prefixes"}``; ``sites`` and ``workflows`` are lists of
``{"key", "name", …}`` (see ``Site`` and ``Workflow``).

A rule is ``{"id", "name", "on", "when", "unless", "then"}``. An organization
rule may add ``"mandatory": true``: a mandatory routing rule cannot be replaced
by a mailbox or workflow rule, and a mandatory limit can never be approved
around (``Excluded.soft``). ``when`` and ``unless`` hold *fields*
(``CONDITIONS``). Every field present in ``when`` must match; inside a list
field any listed value matches; an empty ``when`` matches every fax.
``unless`` excludes a fax that matches all of its fields.

*Limits* all apply when they match and only narrow (``LIMIT_ACTIONS``), so
their order cannot change the result. *Routing rules* are read top to bottom
and the first match chooses (``ROUTE_ACTIONS`` plus ``ROUTE_SETTINGS``).
Precedence between scopes: a matching *mandatory* organization rule, then the
workflow's first match, then the mailbox's, then the recipient's preferred
route (an implicit organization rule), then the organization's first match,
then the automatic choice, which is Faxbot's behaviour without rules. Every
scope's limits apply to whichever rule chooses.
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass, replace
from datetime import datetime
from functools import lru_cache
import hashlib
import json
import re
import types
import typing


FORMAT = 1
LOCAL = 'local'
DIRECT = 'direct'
# "never: [relay]" keeps a fax off every partner relay (direct/relay.py).
RELAY = 'relay'
RESERVED_KEYS = (LOCAL, DIRECT, RELAY)
# A provider account's key, inside the route grammar of routing/store.py.
ACCOUNT_KEY = re.compile(r'[a-z0-9][a-z0-9_-]{0,31}')
# One partner's relay, named like an account: ``relay:`` and the partner's enrollment ID.
RELAY_KEY = re.compile(r'relay:[a-f0-9]{32}')


def is_relay(key):
    """Whether ``key`` names one partner's relay (``relay:<partner>``)."""
    return isinstance(key, str) and RELAY_KEY.fullmatch(key) is not None


# Every Direct message and FHIR route at once (``digital/``): "never: [digital]", "try_in_order: [digital, sip]".
DIGITAL = 'digital'
# One recipient's confirmed Direct address or FHIR endpoint (``digital_addresses.id``).
DIGITAL_KEY = re.compile(r'(dsm|fhir):[a-f0-9]{32}')


def is_digital(key):
    """Whether ``key`` names a digital route: one recipient's (``dsm:<id>``, ``fhir:<id>``) or all of them."""
    return isinstance(key, str) and (key == DIGITAL or DIGITAL_KEY.fullmatch(key) is not None)


def route_key(key):
    """Whether ``key`` can name a route in a rule: an account key, a partner relay or a digital route."""
    return isinstance(key, str) and (ACCOUNT_KEY.fullmatch(key) is not None or is_relay(key) or is_digital(key))
# Keys of lists, regions, sites, workflows and rules in a document.
DEFINITION_KEY = re.compile(r'[a-z0-9][a-z0-9_-]{0,63}')

SCOPE_KINDS = ('organization', 'mailbox', 'workflow')
ORGANIZATION, MAILBOX, WORKFLOW = SCOPE_KINDS
# ``one``: a single account (``use``); ``ordered``: the administrator's order (``try_in_order``);
# ``cheapest``: RoutePolicy's ranking among the listed accounts; ``automatic``: today's behaviour.
MODES = ('one', 'ordered', 'cheapest', 'automatic')
OUTCOMES = ('route', 'held', 'blocked')
HOLD_KINDS = ('approval', 'window', 'no_route')
HOLD_STATES = ('open', 'released', 'refused')
PAGE_LAYOUTS = ('as_receiver_allows', 'one_per_sheet')
ALTERNATE_SETTINGS = ('use', 'never', 'only')
WHEN_BUSY = ('wait', 'next')
DAYS = ('mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun')
# Whose clock a time condition or a time-window hold reads (its ``time_zone`` key).
TIME_ZONES = ('installation', 'sender_site')
SENDER_KINDS = ('person', 'key', 'system')
QUOTE_NUMBERS = ('original', 'alternate')
# A quote for a fax a monthly plan carries ('In your plan'), or carries past your normal-use budget.
QUOTE_PLANS = ('included', 'over_budget')
# What chose a decision's route: a written rule, the recipient's preferred route, or the automatic choice.
SOURCE_KINDS = ('rule', 'preferred', 'automatic')
STEP_KINDS = ('limit', 'route', 'preferred')
# ``not_applied``: the rule matched but did not decide, because a mandatory rule or a more specific
# scope chose, or because its accounts were all excluded (the preferred route under a limit).
STEP_RESULTS = ('matched', 'not_matched', 'unless', 'not_reached', 'not_applied')
# A stored decision keeps the steps that decided something; the rest are reproduced by replaying its facts.
STORED_RESULTS = ('matched', 'unless', 'not_applied')

# Decision.reason when the outcome is ``blocked``. A blocked fax is held for the administrator (Q1).
BLOCKED_REASONS = (
    'no_allowed_account',    # limits or scopes removed every account the route names, and no delivery inside Faxbot
    'no_account_under_cap',  # every remaining account costs more than the cap, or its cost is unknown
    'needs_partner',         # require_direct, and the number has no verified partner
    'needs_encryption',      # require_encryption: no partner, and no SSL Fax this number has used before
    'needs_alternate',       # alternate_number: only, and the recipient has no approved alternate number
    'no_site',               # site_accounts: sender, and the sender belongs to no site
)
# Excluded.why: why an account is not in a fax's envelope. Only a cap's exclusions can be soft: an
# administrator with "Approve faxes" may send by that account anyway, unless the cap's rule is mandatory.
EXCLUSIONS = (
    'never',             # a limit's ``never``
    'over_cap',          # its estimate is over a cap
    'unknown_cost',      # a cap applies and its cost is unknown, which is never zero
    'not_sending',       # the account does not send faxes
    'turned_off',        # the account was turned off when the fax was accepted
    'unknown_account',   # the rule names an account the fax's configuration does not have
    'direct_required',   # require_direct: no calling account may be used
    'not_encrypted',     # require_encryption: the account cannot carry SSL Fax for this number
)

# The rule document's words. ``when`` and ``unless`` hold these fields, grouped by block.
# List fields: any listed value matches. Yes/no fields: the fact must equal the value.
CONDITIONS = {
    'destination': {
        'numbers': 'list',        # exact E.164 numbers
        'prefixes': 'list',       # E.164 prefixes such as +4420 (an area code)
        'lists': 'list',          # recipient group keys
        'countries': 'list',      # ISO 3166 alpha-2
        'regions': 'list',        # region keys
        'recipients': 'list',     # saved recipient IDs
        'partner': 'yes_no',      # a verified direct partner at acceptance
        'own_number': 'yes_no',   # one of this installation's receiving numbers
        'approved_alternate': 'yes_no',   # the recipient has an approved alternate number
        'in_sender_country': 'yes_no',    # the destination is in the sender site's country
    },
    'sender': {
        'people': 'list',         # principal IDs (people and integrations)
        'keys': 'list',           # integration key bindings
        'groups': 'list',         # group IDs
        'mailboxes': 'list',      # the "send from" mailbox
        'sites': 'list',          # the sender's site key
    },
    'workflows': 'list',          # workflow keys (a top-level list field)
    'labels': 'list',             # labels the sender chose (a top-level list field)
    'document': {
        'pages_over': 'count',    # more than this many original pages
        'pages_under': 'count',   # fewer than this many original pages
        'size_over': 'count',     # more than this many bytes
        'case_packet': 'yes_no',
    },
    'urgent': 'yes_no',           # a top-level yes/no field
    'real_call': 'yes_no',        # the sender asked for a real call ("place a real call")
    # One field: inside a window starting on one of ``days`` (every day when absent), from ``from``
    # until ``until`` (HH:MM, local; a window may wrap past midnight; absent means the whole day),
    # on the installation's or the sender site's clock (``time_zone``, TIME_ZONES).
    'time': 'window',
}
# A routing rule's ``then`` holds exactly one route action and any route settings.
ROUTE_ACTIONS = (
    'use',                # one account key
    'try_in_order',       # account keys, in order
    'cheapest_reliable',  # account keys, ranked by RoutePolicy
    'site_accounts',      # 'sender' or a site key; its ``mode`` setting picks the order
    'automatic',          # true: today's behaviour, written explicitly
)
ROUTE_SETTINGS = (
    'mode',               # with site_accounts only: 'ordered' or 'cheapest_reliable' (the default)
    'when_busy',          # WHEN_BUSY; 'wait' when absent
    'page_layout',        # PAGE_LAYOUTS; Faxbot's own default when absent
    'alternate_number',   # ALTERNATE_SETTINGS; Faxbot's own default (dial an approved alternate) when absent
    'subaddress',         # the T.33 subaddress the fax asks for (SUBADDRESS): a department or mailbox behind the number
)
# A subaddress as a fax carries it (T.30 SUB, at most 20 characters): digits and +, # and *.
SUBADDRESS = re.compile(r'[0-9#*+]{1,20}')
SITE_ACCOUNT_MODES = ('ordered', 'cheapest_reliable')
# A limit's ``then`` holds one or more of these.
LIMIT_ACTIONS = (
    'never',              # account keys, or 'local' / 'direct'
    'require_direct',     # true
    'require_encryption', # true: direct delivery, or SSL Fax when this number has used it before
    'cap_cost',           # money: {"currency": "USD", "amount": "0.50"}
    'hold_for_approval',  # {"separate_approver": bool}
    'hold_until',         # {"days", "from", "until", "time_zone"}: the next window
    'place_a_real_call',  # true: no delivery inside Faxbot
    'alternate_number',   # 'never' only, as a limit
)
RULE_KEYS = ('id', 'name', 'on', 'mandatory', 'when', 'unless', 'then')
ORGANIZATION_KEYS = ('format', 'lists', 'labels', 'regions', 'sites', 'workflows', 'limits', 'routes')
SCOPE_DOCUMENT_KEYS = ('format', 'limits', 'routes')
# The check's limits, which also bound evaluation cost (design §6.4).
MAX_RULES = 500
MAX_CONDITIONS = 10
MAX_LISTED_NUMBERS = 10_000
MAX_DOCUMENT_BYTES = 1_000_000


def _one_of(value, allowed, name, *, optional=False):
    if (value is None and optional) or value in allowed:
        return
    raise ValueError(f'Unknown {name}.')


def _utc_text(value, name, *, optional=False):
    if value is None and optional:
        return
    if not isinstance(value, str):
        raise ValueError(f'{name} must be a time.')
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is not None:
        raise ValueError(f'{name} must be naive UTC.')


# Accounts -----------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Account:
    """One provider account as the fax's configuration revision has it; WP-B's ``sending_accounts`` returns these.

    Order matters: the default sending account first, then ``FAX_OUTBOUND_ROUTES`` in order (these two
    are ``automatic``), then every other account. The automatic choice keeps that order and leaves the
    ranking to RoutePolicy at dispatch, so with no rules the plan is exactly today's.
    """
    key: str
    provider: str
    label: str = ''
    sends: bool = True
    enabled: bool = True
    default: bool = False     # the default sending account: the anchor of fax_job_bindings
    automatic: bool = False   # the automatic choice may use it (the default and FAX_OUTBOUND_ROUTES)
    site: str | None = None   # the site its calls start from
    sslfax: bool = False      # a trunk whose calls offer SSL Fax

    def __post_init__(self):
        if not isinstance(self.key, str) or ACCOUNT_KEY.fullmatch(self.key) is None or self.key in RESERVED_KEYS:
            raise ValueError('Invalid account key.')


# Facts ---------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Sender:
    principal_id: str | None = None
    kind: str | None = None                # SENDER_KINDS
    key_id: str | None = None              # the integration key binding, when kind is 'key'
    groups: tuple[str, ...] = ()           # group IDs at acceptance, sorted

    def __post_init__(self):
        _one_of(self.kind, SENDER_KINDS, 'sender kind', optional=True)
        if list(self.groups) != sorted(set(self.groups)):
            raise ValueError('Sender groups must be sorted and distinct.')


@dataclass(frozen=True)
class Alternate:
    """A recipient's approved alternate number (AD's approvals), as it stood at acceptance."""
    number: str                            # E.164
    approval_id: str
    approved_by: str | None = None         # the approver's name as recorded
    approved_at: str | None = None         # naive UTC
    note: str | None = None                # the approval's evidence note

    def __post_init__(self):
        _utc_text(self.approved_at, 'Approval time', optional=True)


@dataclass(frozen=True)
class Quote:
    """One account's estimated cost for this fax at acceptance (origin-rated when a rate row applies).

    ``micros`` None is an unknown cost, never zero. ``number`` says whether it prices the original number
    or the approved alternate. ``origin`` is the rate row's origin ('any', a site key, 'country:GB'), or
    None for the card's flat price. ``pages`` is the page or sheet count the estimate used. ``plan`` is
    'included' when a monthly plan carries the fax (what it adds is 0, and it reads "In your plan", never
    "$0.00"), 'over_budget' when that plan is past the normal-use budget you set, else None.
    """
    account: str
    micros: int | None
    currency: str | None
    number: str = 'original'
    origin: str | None = None
    pages: int | None = None
    plan: str | None = None

    def __post_init__(self):
        _one_of(self.number, QUOTE_NUMBERS, 'quoted number')
        _one_of(self.plan, QUOTE_PLANS, 'plan quote', optional=True)
        if (self.micros is None) != (self.currency is None):
            raise ValueError('A quote has both an amount and a currency, or neither.')


@dataclass(frozen=True)
class Facts:
    """What the rules read about one fax, captured once at acceptance; never the document's content."""
    destination: str                       # canonical E.164, or the literal key for an unusual number
    accepted_at: str                       # naive UTC, ISO
    country: str | None = None             # the destination's ISO 3166 alpha-2 country
    recipient_id: str | None = None        # the saved recipient, if any
    preferred_route: str | None = None     # the recipient's preferred route at acceptance
    partner: bool = False                  # a verified direct partner at acceptance
    own_number: bool = False               # one of this installation's receiving numbers
    sslfax_seen: bool = False              # this number has completed an SSL Fax call before
    alternate: Alternate | None = None     # the approved alternate number, if any
    sender: Sender = field(default_factory=Sender)
    mailbox_id: str | None = None          # "send from mailbox"
    workflow: str | None = None            # the workflow chosen on the send form; the decision records the one used
    labels: tuple[str, ...] = ()           # sorted and distinct
    pages: int = 0                         # original pages
    size_bytes: int = 0
    case_packet: bool = False
    urgent: bool = False
    by_call: bool = False                  # "place a real call"
    time_zone: str = ''                    # the installation's IANA time zone at acceptance; '' is UTC
    quotes: tuple[Quote, ...] = ()
    approximate: bool = False              # rebuilt for a fax accepted before rules (the replay check)
    format: int = FORMAT

    def __post_init__(self):
        if not isinstance(self.destination, str) or not self.destination:
            raise ValueError('A fax needs a destination.')
        _utc_text(self.accepted_at, 'Acceptance time')
        if list(self.labels) != sorted(set(self.labels)):
            raise ValueError('Labels must be sorted and distinct.')
        if self.pages < 0 or self.size_bytes < 0:
            raise ValueError('Pages and size cannot be negative.')

    def to_json(self):
        return canonical(self)

    @classmethod
    def from_json(cls, text):
        return from_plain(cls, json.loads(text))

    def digest(self):
        return hashlib.sha256(self.to_json().encode('ascii')).hexdigest()


# Decisions -----------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Source:
    """Which scope, revision and rule set one part of a decision."""
    kind: str                              # SOURCE_KINDS
    scope: str = ORGANIZATION
    scope_id: str = ''                     # '' for the organization, a mailbox ID, a workflow key
    revision: int | None = None            # that scope's revision number
    rule_id: str | None = None
    rule_name: str | None = None

    def __post_init__(self):
        _one_of(self.kind, SOURCE_KINDS, 'decision source')
        _one_of(self.scope, SCOPE_KINDS, 'scope')


AUTOMATIC = Source('automatic')


@dataclass(frozen=True)
class Cap:
    micros: int
    currency: str
    source: Source


@dataclass(frozen=True)
class Hold:
    kind: str                              # 'approval' or 'window' (a blocked fax's 'no_route' hold is its outcome)
    source: Source
    separate_approver: bool = False        # approval: the approver must not be the sender
    release_at: str | None = None          # window: when it opens, naive UTC, worked out at acceptance

    def __post_init__(self):
        _one_of(self.kind, ('approval', 'window'), 'hold')
        _utc_text(self.release_at, 'Release time', optional=True)


@dataclass(frozen=True)
class Excluded:
    account: str
    why: str                               # EXCLUSIONS
    source: Source | None = None
    soft: bool = False                     # "Approve faxes" may send by it anyway (a cap that is not mandatory)

    def __post_init__(self):
        _one_of(self.why, EXCLUSIONS, 'exclusion')


@dataclass(frozen=True)
class Envelope:
    """What delivery may use for this fax. Dispatch never leaves it.

    ``accounts`` are the calling accounts allowed, in order: the administrator's order for ``one`` and
    ``ordered``; the configured order (default account first) for ``cheapest`` and ``automatic``, which
    RoutePolicy ranks at each attempt. ``local`` and ``direct`` say whether delivery inside Faxbot (own
    numbers) and direct delivery to a verified partner are allowed; dispatch still checks they are ready.
    ``preferred`` is the recipient's preferred route, pinned at acceptance, for RoutePolicy's override.
    """
    mode: str                              # MODES
    accounts: tuple[str, ...] = ()
    local: bool = False
    direct: bool = False
    require_direct: bool = False           # only direct delivery (or delivery inside Faxbot); no calls
    require_encryption: bool = False       # direct delivery, or SSL Fax on ``accounts`` (see ``sslfax``)
    sslfax: bool = False                   # calls must offer SSL Fax; SSL Fax cannot verify the far end
    caps: tuple[Cap, ...] = ()             # the lowest cap per currency
    holds: tuple[Hold, ...] = ()
    when_busy: str = 'wait'                # WHEN_BUSY
    preferred: str | None = None
    alternate: str | None = None           # ALTERNATE_SETTINGS, or None when no rule set it
    dial: Alternate | None = None          # the number calling attempts dial instead of the original, pinned
    page_layout: str | None = None         # PAGE_LAYOUTS, or None for Faxbot's default
    strict_fallback: bool = False          # a rule chose the route: fall back only after a pre-data failure (Q3)
    # The T.33 subaddress a calling attempt asks for (the built-in engine's patch 0005; ami.py), or None. Requested,
    # never promised: the far end's machine must take subaddresses.
    subaddress: str | None = None

    def __post_init__(self):
        _one_of(self.mode, MODES, 'route mode')
        _one_of(self.when_busy, WHEN_BUSY, 'busy setting')
        _one_of(self.alternate, ALTERNATE_SETTINGS, 'alternate number setting', optional=True)
        _one_of(self.page_layout, PAGE_LAYOUTS, 'page layout', optional=True)
        if self.subaddress is not None and (not isinstance(self.subaddress, str)
                                            or SUBADDRESS.fullmatch(self.subaddress) is None):
            raise ValueError('Unknown subaddress.')
        if len(set(self.accounts)) != len(self.accounts) or any(key in RESERVED_KEYS for key in self.accounts):
            raise ValueError('Each account may appear once.')
        if len({cap.currency for cap in self.caps}) != len(self.caps):
            raise ValueError('One cap per currency.')


@dataclass(frozen=True)
class RevisionRef:
    scope: str
    scope_id: str
    revision_id: str
    number: int

    def __post_init__(self):
        _one_of(self.scope, SCOPE_KINDS, 'scope')

    @property
    def name(self):
        return scope_name(self.scope, self.scope_id)


@dataclass(frozen=True)
class Step:
    """One line of the trace: a rule, whether it matched, and the first field that decided it."""
    kind: str                              # STEP_KINDS
    result: str                            # STEP_RESULTS
    scope: str = ORGANIZATION
    scope_id: str = ''
    revision: int | None = None
    rule_id: str | None = None
    rule_name: str | None = None
    field: str | None = None               # 'destination.countries', 'time', …: the first that did not match
    note: str | None = None                # a code: 'mandatory', 'excluded', 'overridden', …

    def __post_init__(self):
        _one_of(self.kind, STEP_KINDS, 'trace step')
        _one_of(self.result, STEP_RESULTS, 'trace result')
        _one_of(self.scope, SCOPE_KINDS, 'scope')


@dataclass(frozen=True)
class Decision:
    """A fax's routing decision: outcome, envelope, provenance and trace. Stored as canonical JSON."""
    outcome: str                           # OUTCOMES
    envelope: Envelope
    route: Source                          # what chose the mode and accounts
    facts_digest: str
    revisions: tuple[RevisionRef, ...] = ()
    reason: str | None = None              # BLOCKED_REASONS when blocked
    site: str | None = None                # the sender's site, as the organization document defines it
    workflow: str | None = None            # the workflow the fax was decided under
    alternate_source: Source | None = None
    layout_source: Source | None = None
    excluded: tuple[Excluded, ...] = ()
    trace: tuple[Step, ...] = ()
    format: int = FORMAT

    def __post_init__(self):
        _one_of(self.outcome, OUTCOMES, 'decision outcome')
        if (self.outcome == 'blocked') != (self.reason is not None):
            raise ValueError('A blocked decision, and only a blocked one, has a reason.')
        _one_of(self.reason, BLOCKED_REASONS, 'blocked reason', optional=True)

    def compact(self):
        """The decision as stored with a fax: its trace keeps only the steps that decided something.

        Rules that did not match are left out (with hundreds of rules they would be most of the record);
        replaying the stored facts under the stored revisions gives them back exactly.
        """
        return replace(self, trace=tuple(step for step in self.trace
                                         if step.result in STORED_RESULTS or step.kind == 'preferred'))

    @property
    def revision_ids(self):
        """``{scope name: revision ID}``: the ``revisions`` column of fax_job_rule_decisions."""
        return {ref.name: ref.revision_id for ref in self.revisions}

    def to_json(self):
        return canonical(self)

    @classmethod
    def from_json(cls, text):
        return from_plain(cls, json.loads(text))


# Document parts ------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class RecipientList:
    key: str
    name: str
    numbers: tuple[str, ...] = ()
    prefixes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Region:
    """A named set of countries and number prefixes, such as Northern England: +44113, +44114, +44161."""
    key: str
    name: str
    countries: tuple[str, ...] = ()
    prefixes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Site:
    """A place the organization works from.

    A fax's sender site is the first site whose mailboxes include its sending mailbox, else the first whose
    groups include one of the sender's groups. A site's accounts are those it lists, then those whose own
    ``site`` names it, in configured order; one account belongs to at most one site.
    """
    key: str
    name: str
    country: str | None = None
    time_zone: str | None = None
    mailboxes: tuple[str, ...] = ()
    groups: tuple[str, ...] = ()
    accounts: tuple[str, ...] = ()


@dataclass(frozen=True)
class Workflow:
    """A fax's workflow: the send form's, else its mailbox's, else the first whose labels the fax carries."""
    key: str
    name: str
    mailboxes: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()


@dataclass(frozen=True)
class RateRow:
    """An origin-rated price row of a rate card (B10; WP-T owns its table and lookup).

    The estimate for an account uses the rows for the account's origin with the longest matching
    destination prefix, falling back to the card's flat price. Prices use the card's currency.
    """
    origin: str                            # 'any', a site key, or 'country:GB'
    destination_prefix: str                # E.164 digits without '+'; '' matches every number
    per_minute_micros: int
    per_page_micros: int
    per_call_micros: int
    billing_increment_seconds: int
    minimum_seconds: int
    card_id: str | None = None
    source_url: str | None = None
    captured_on: str | None = None         # naive UTC

    def __post_init__(self):
        if not re.fullmatch(r'[0-9]{0,15}', self.destination_prefix or ''):
            raise ValueError('A destination prefix is digits only.')
        if min(self.per_minute_micros, self.per_page_micros, self.per_call_micros, self.minimum_seconds) < 0 \
                or self.billing_increment_seconds < 1:
            raise ValueError('Invalid rate row amounts.')


# Scopes --------------------------------------------------------------------------------------------------------

def scope_name(kind, scope_id=''):
    """'organization', 'mailbox:<id>' or 'workflow:<key>': the CLI's --scope and the decision's revision keys."""
    _one_of(kind, SCOPE_KINDS, 'scope')
    if kind == ORGANIZATION:
        if scope_id:
            raise ValueError('The organization scope has no ID.')
        return ORGANIZATION
    if not scope_id or not isinstance(scope_id, str) or len(scope_id) > 100:
        raise ValueError('A mailbox or workflow scope names its mailbox or workflow.')
    return f'{kind}:{scope_id}'


def parse_scope(name):
    """The inverse of ``scope_name``: ``(kind, scope_id)``."""
    if name == ORGANIZATION:
        return ORGANIZATION, ''
    kind, _, scope_id = (name or '').partition(':')
    if kind not in (MAILBOX, WORKFLOW):
        raise ValueError('Unknown scope.')
    scope_name(kind, scope_id)
    return kind, scope_id


# Canonical JSON ------------------------------------------------------------------------------------------------

_NAMES = {}


def plain(value):
    """Dataclasses as dicts and tuples as lists, for JSON."""
    names = _NAMES.get(type(value))
    if names is None and is_dataclass(value):
        names = _NAMES[type(value)] = tuple(item.name for item in fields(value))
    if names is not None:
        return {name: plain(getattr(value, name)) for name in names}
    if isinstance(value, (tuple, list)):
        return [plain(item) for item in value]
    if isinstance(value, float):
        raise ValueError('Rules never store floats.')
    return value


def canonical(value):
    return json.dumps(plain(value), sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)


@lru_cache(maxsize=None)
def _hints(cls):
    return typing.get_type_hints(cls)


def _build(hint, value):
    if value is None:
        return None
    origin = typing.get_origin(hint)
    if origin in (typing.Union, types.UnionType):
        options = [item for item in typing.get_args(hint) if item is not type(None)]
        return _build(options[0], value)
    if origin is tuple:
        if not isinstance(value, list):
            raise ValueError('Expected a list.')
        item = typing.get_args(hint)[0]
        return tuple(_build(item, entry) for entry in value)
    if is_dataclass(hint):
        return from_plain(hint, value)
    if hint is bool:
        if not isinstance(value, bool):
            raise ValueError('Expected yes or no.')
    elif hint is int:
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError('Expected a whole number.')
    elif hint is str:
        if not isinstance(value, str):
            raise ValueError('Expected text.')
    return value


def from_plain(cls, data):
    """Rebuild a dataclass from ``plain`` output, checking types; unknown keys are ignored."""
    if not isinstance(data, dict):
        raise ValueError('Expected an object.')
    hints = _hints(cls)
    values = {item.name: _build(hints[item.name], data[item.name]) for item in fields(cls) if item.name in data}
    return cls(**values)
