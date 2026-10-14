"""Your Telnyx balance, and what happens when it runs out (read-only).

Live, 13 October 2026: Telnyx turned an installation's account off at a balance of -$1.38. Every call to its numbers
then got SIP 404 from Telnyx, and a Fax API send failed with ``account_disabled``, while Faxbot's board and health
read as if nothing were wrong. Telnyx stops a prepaid account's calls in both directions when its balance reaches $0.

When a Telnyx trunk or the Telnyx Fax API is set up with an API key, Faxbot reads ``GET /v2/balance``
(https://developers.telnyx.com/api-reference/billing/get-user-balance-details: ``balance``, ``credit_limit``,
``available_credit``, ``currency``, decimal text) every ``INTERVAL``, and again at once after a Fax API send fails
with ``account_disabled`` (``note_disabled``). Two states, said in one sentence on the health status, the outbound
board and ``faxbot overview``:

- **low**: the credit available is below a week of your usual Telnyx spending (Faxbot's own cost records for its
  Telnyx routes over the last ``SPEND_DAYS`` days), or below ``DEFAULT_WARNING`` when there is none;
- **stopped**: the credit available is $0 or less, or Telnyx reported ``account_disabled``. Faxbot's Telnyx routes are
  then skipped (``planned_skip``: ``account_stopped``), so a fax goes by your next route and says why.

The key goes only to api.telnyx.com and is never logged; neither are response bodies. A balance Faxbot could not read
changes nothing (the last reading stands, and ``account_disabled`` still stops the routes).
"""
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import threading

import httpx


URL = 'https://api.telnyx.com/v2/balance'
INTERVAL = timedelta(minutes=15)
SPEND_DAYS = 28
DEFAULT_WARNING = 5_000_000  # $5 when Faxbot has no record of your usual Telnyx spending
MICROS = Decimal(1_000_000)
TELNYX_ROUTES = ('telnyx_fax',)
SKIP = 'account_stopped'


# The setting ``telnyx_balance_warning``, in the console and ``faxbot delivery providers trunk balance --warn-below``,
# in the same words.
WARNING_LABEL = 'Warn when your Telnyx balance is below'
WARNING_HELP = ('An amount such as 10, in your Telnyx currency. Leave it empty to be warned below a week of your '
                'usual Telnyx spending, or $5 when Faxbot has none.')
# A burst of carrier refusals (SIP 404 or 403: Q.850 cause 1 or 21) reads the balance at once, at most this often.
BURST = 3
BURST_WINDOW = timedelta(minutes=5)


def normalize_warning(value):
    """'' or a positive amount with at most two decimals ('10.00'); raises ValueError in plain words."""
    text = str(value or '').strip().lstrip('$')
    if not text:
        return ''
    try:
        amount = Decimal(text)
    except (InvalidOperation, ValueError):
        raise ValueError('Enter an amount such as 10, or leave it empty.') from None
    if not amount.is_finite() or amount <= 0 or amount > 100000 or amount != amount.quantize(Decimal('0.01')):
        raise ValueError('Enter an amount above 0 and up to 100000, with at most two decimals, or leave it empty.')
    return f'{amount:.2f}'


class BalanceUnavailable(RuntimeError):
    """The balance could not be read now; one plain sentence, never a secret."""


@dataclass(frozen=True)
class Balance:
    balance_micros: int
    available_micros: int
    credit_limit_micros: int
    currency: str
    read_at: datetime


@dataclass(frozen=True)
class State:
    balance: Balance | None = None
    # Telnyx said the account is turned off (a Fax API send's ``account_disabled``), and when.
    disabled_at: datetime | None = None


_lock = threading.Lock()
_state = State()


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def _micros(value):
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise BalanceUnavailable('Telnyx returned an unusable balance.') from None
    if not amount.is_finite():
        raise BalanceUnavailable('Telnyx returned an unusable balance.')
    return int((amount * MICROS).to_integral_value())


def fetch(api_key, *, client_factory=None, timeout=15.0, now=None):
    """The account's ``Balance`` now; raises ``BalanceUnavailable``."""
    if not api_key:
        raise BalanceUnavailable('No Telnyx API key is set.')
    factory = client_factory or (lambda: httpx.Client(timeout=timeout))
    headers = {'Authorization': f'Bearer {api_key}', 'Accept': 'application/json'}
    try:
        with factory() as client:
            response = client.get(URL, headers=headers)
    except httpx.HTTPError:
        raise BalanceUnavailable('Telnyx could not be reached.') from None
    if response.status_code != 200:
        raise BalanceUnavailable('Telnyx did not answer the balance request.')
    try:
        data = response.json().get('data')
    except (ValueError, AttributeError):
        raise BalanceUnavailable('Telnyx returned an unusable balance.') from None
    if not isinstance(data, dict):
        raise BalanceUnavailable('Telnyx returned an unusable balance.')
    currency = str(data.get('currency') or 'USD').upper()[:3]
    balance = _micros(data.get('balance'))
    limit = _micros(data.get('credit_limit') or '0')
    available = _micros(data['available_credit']) if data.get('available_credit') not in (None, '') else (
        balance + limit)
    return Balance(balance, available, limit, currency, now or _now())


def current():
    with _lock:
        return _state


def record(balance):
    """Keep a new reading. Credit available again clears an earlier ``account_disabled``."""
    global _state
    with _lock:
        disabled = _state.disabled_at if balance.available_micros <= 0 else None
        _state = State(balance, disabled)
    return _state


def note_disabled(now=None):
    """Telnyx reported ``account_disabled``: the routes stop at once, and the balance is read again soon."""
    global _state, _due
    with _lock:
        _state = replace(_state, disabled_at=now or _now())
        _due = None
    return _state


def clear():
    """No Telnyx key any more: nothing to say."""
    global _state, _due, _daily, _warning, _burst_at
    with _lock:
        _state, _due, _daily, _warning, _burst_at = State(), None, None, None, None
        _refusals.clear()


_due = None
# Your usual Telnyx spending a day, as the last background check read it, for readers without the database.
_daily = None
# The warning level you set (``telnyx_balance_warning``), in micros; None: a week of usual spending, else $5.
_warning = None
# Carrier refusals seen lately, and when a burst of them last read the balance.
_refusals = []
_burst_at = None


def note_refusal(now=None):
    """A call the carrier refused (SIP 404 or 403). ``BURST`` of them within ``BURST_WINDOW`` read the balance at
    once (an account Telnyx turned off refuses every call), at most once per ``BURST_WINDOW``."""
    global _due, _burst_at
    now = now or _now()
    with _lock:
        _refusals[:] = [moment for moment in _refusals if now - moment < BURST_WINDOW] + [now]
        if len(_refusals) >= BURST and (_burst_at is None or now - _burst_at >= BURST_WINDOW):
            _due, _burst_at = None, now
            _refusals.clear()
            return True
    return False


def step(api_key, *, engine=None, client_factory=None, now=None, values=None):
    """The background check: read the balance when it is due (every ``INTERVAL``, or at once after
    ``note_disabled``). Returns False (the caller's repeat waits its interval)."""
    global _due, _daily, _warning
    now = now or _now()
    key = api_key() if callable(api_key) else api_key
    if values is not None:
        _warning = configured_micros(values)
    if not key:
        clear()
        return False
    with _lock:
        due = _due
    if due is not None and now < due:
        return False
    try:
        record(fetch(key, client_factory=client_factory, now=now))
    except BalanceUnavailable:
        pass  # the last reading stands
    if engine is not None:
        _daily = usual_daily_micros(engine, now)
    with _lock:
        _due = now + INTERVAL
    return False


def usual_daily_micros(engine, now=None):
    """Your usual Telnyx spending a day, from Faxbot's own cost records for its Telnyx routes over the last
    ``SPEND_DAYS`` days (the carrier's charge where reported, else the estimate); None when there is none."""
    if engine is None:
        return None
    import sqlalchemy as sa
    now = now or _now()
    try:
        costs = sa.Table('delivery_attempt_costs', sa.MetaData(), autoload_with=engine)
    except sa.exc.SQLAlchemyError:
        return None
    amount = sa.func.coalesce(*([costs.c.reported_cost_micros] if 'reported_cost_micros' in costs.c else []),
                              costs.c.estimated_cost_micros)
    query = sa.select(sa.func.sum(amount)).where(
        sa.or_(costs.c.route == 'sip', costs.c.provider_id.in_(TELNYX_ROUTES), costs.c.route.in_(TELNYX_ROUTES)),
        costs.c.created_at >= now - timedelta(days=SPEND_DAYS), costs.c.currency == 'USD')
    try:
        with engine.connect() as connection:
            total = connection.execute(query).scalar()
    except sa.exc.SQLAlchemyError:
        return None
    if not total or total <= 0:
        return None
    return -(-int(total) // SPEND_DAYS)


def configured_micros(values):
    """The warning level you set, in micros, or None for the default."""
    try:
        text = normalize_warning(getattr(values, 'telnyx_balance_warning', '') or '')
    except ValueError:
        return None
    return _micros(text) if text else None


def warning_micros(engine=None, now=None):
    """The credit below which Faxbot warns: a week of your usual Telnyx spending, else ``DEFAULT_WARNING``."""
    daily = usual_daily_micros(engine, now)
    return DEFAULT_WARNING if daily is None else max(daily * 7, 1)


def status(engine=None, now=None, state=None):
    """``{'state': 'low' or 'stopped', 'sentence', 'available', 'read_at'}``, or None when there is nothing to say."""
    from .costs import format_amount, money_text
    state = state or current()
    balance = state.balance
    stopped = state.disabled_at is not None or (balance is not None and balance.available_micros <= 0)
    if balance is None and not stopped:
        return None
    money = money_text(balance.available_micros, balance.currency) if balance is not None else None
    available = ({'amount': format_amount(balance.available_micros) if balance.available_micros >= 0
                  else '-' + format_amount(-balance.available_micros), 'currency': balance.currency}
                 if balance is not None else None)
    read_at = balance.read_at.isoformat() + 'Z' if balance is not None else None
    if stopped:
        told = f' (your balance is {money})' if money else ''
        return {'state': 'stopped', 'available': available, 'read_at': read_at, 'sentence': (
            f'Telnyx has turned your account off{told}, so faxes cannot be sent or received through Telnyx. Faxbot '
            'sends by your other routes until you add funds in the Telnyx portal under Billing.')}
    daily = usual_daily_micros(engine, now) if engine is not None else _daily
    chosen = _warning
    threshold = chosen if chosen is not None else DEFAULT_WARNING if daily is None else max(daily * 7, 1)
    if balance.available_micros >= threshold:
        return None
    if daily:
        days = balance.available_micros // daily
        span = 'less than a day' if days < 1 else f'about {days} day{"s" if days != 1 else ""}'
        how = f'{span} of your usual faxing'
    elif chosen is not None:
        how = f'below the {money_text(threshold, balance.currency)} you chose to be warned at'
    else:
        how = f'below the {money_text(threshold, balance.currency)} Faxbot warns at'
    return {'state': 'low', 'available': available, 'read_at': read_at, 'sentence': (
        f'Your Telnyx balance is {money}, {how}. When it reaches $0, Telnyx stops your calls in both directions. '
        'Add funds in the Telnyx portal under Billing.')}


def is_telnyx_route(values, key, provider):
    """Whether account ``key`` (of ``provider``) sends through Telnyx: the Telnyx Fax API, or a trunk with the Telnyx
    preset."""
    if provider in TELNYX_ROUTES or key in TELNYX_ROUTES:
        return True
    if provider != 'sip':
        return False
    try:
        from ..ami import trunk_values
        own, _ = trunk_values(values, None if key == 'sip' else key)
    except Exception:
        own = values
    return (getattr(own, 'sip_trunk_preset', '') or '') == 'telnyx'


def stopped(state=None):
    """Whether Telnyx routes are stopped now (``account_disabled``, or no credit left)."""
    state = state or current()
    return state.disabled_at is not None or (state.balance is not None and state.balance.available_micros <= 0)
