"""Published plans a provider advertises that Faxbot never adds as a rate card by itself.

``config/rate_cards.json`` lists them under ``reference_plans``, in the plan
card format plus ``country``, ``included_pages`` and ``overage_per_page``. eFax
prices the API Faxbot uses by quote in every country, so eFax gets no starting
rate card and reads "No published price; add your rate"; this module gives
that place one sentence about eFax's published plans for the installation
country and, where there is a price, a card a person can save as their own
estimate. A rate card holds a monthly fee but no page allowance, so the
estimate counts the monthly fee and the sentence names the allowance.
"""
import json
from pathlib import Path

from .seed import default_path


# eFax publishes plan prices in these countries; any other installation reads the US ones.
_COUNTRY_NAMES = {'US': 'the US', 'AU': 'Australia', 'GB': 'the UK'}
_PAGE_NAMES = {'US': 'US', 'AU': 'Australian', 'GB': 'UK'}
_PROVIDER_NAMES = {'efax': 'eFax'}
_UK_PAGE = 'https://ww2.efax.com/uk/'


def load_reference_plans(path=None):
    """Every reference plan in the shipped file; an unreadable file gives none."""
    path = Path(path) if path is not None else default_path()
    try:
        document = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return []
    plans = document.get('reference_plans') if isinstance(document, dict) else None
    return [plan for plan in plans if isinstance(plan, dict)] if isinstance(plans, list) else []


def _cents(amount, currency):
    value = float(amount)
    if value < 1:
        return f'{round(value * 100):d}¢'
    return f'{currency} {value:.2f}'


def _priced(plan):
    return plan.get('monthly_fee') not in (None, '')


def _card(plan):
    """The rate card a person can save as their own estimate from a priced plan."""
    included = plan.get('included_pages')
    label = plan['label'] + (f', {included} pages a month' if included else '') + ' (my estimate)'
    return {'provider_id': plan['provider_id'], 'label': label[:100], 'direction': plan.get('direction', 'outbound'),
            'currency': plan['currency'], 'per_minute': '0', 'per_page': '0', 'per_call': '0',
            'billing_increment_seconds': 60, 'minimum_seconds': 0, 'source_url': plan.get('source_url'),
            'captured_on': plan.get('advertised_on'), 'monthly_fee': plan['monthly_fee']}


def suggestion(provider_id, country, plans=None):
    """What to say where a provider has no published API price, for the installation country.

    Returns ``{'provider_id', 'country', 'sentence', 'card', 'page_url', 'plans'}``;
    ``card`` is None when no price could be read for that country.
    """
    plans = [plan for plan in (load_reference_plans() if plans is None else plans)
             if plan.get('provider_id') == provider_id]
    if not plans:
        return None
    name = _PROVIDER_NAMES.get(provider_id, provider_id)
    country = (country or 'US').upper()
    local = [plan for plan in plans if plan.get('country') == country]
    if not local:
        country, local = 'US', [plan for plan in plans if plan.get('country') == 'US']
    priced = sorted((plan for plan in local if _priced(plan)), key=lambda plan: float(plan['monthly_fee']))
    if not priced:
        page = (local[0].get('source_url') if local else None) or _UK_PAGE
        where = _COUNTRY_NAMES.get(country, country)
        return {'provider_id': provider_id, 'country': country, 'card': None, 'page_url': page, 'plans': plans,
                'page_label': f"{name}'s {_PAGE_NAMES.get(country, country)} page",
                'sentence': f"{name} prices its API by quote. Faxbot could not read {name}'s prices for {where}; "
                            f"see {name}'s {_PAGE_NAMES.get(country, country)} page."}
    cheapest = priced[0]
    details = []
    if cheapest.get('includes'):
        details.append(cheapest['includes'].replace(' a month', ''))
    if cheapest.get('overage_per_page'):
        details.append(f"then {_cents(cheapest['overage_per_page'], cheapest['currency'])} a page")
    tax = ', before GST' if country == 'AU' else ''
    sentence = (f"{name} prices its API by quote. Its published plans start at {cheapest['currency']} "
                f"{float(cheapest['monthly_fee']):.2f} a month in {_COUNTRY_NAMES.get(country, country)}{tax}"
                + (f" ({', '.join(details)})." if details else '.'))
    return {'provider_id': provider_id, 'country': country, 'card': _card(cheapest),
            'page_url': cheapest.get('source_url'), 'page_label': f"{name}'s {_PAGE_NAMES.get(country, country)} prices",
            'plans': plans, 'sentence': sentence}
