"""Country service rules for where Faxbot runs (M4): the UAE and Saudi Arabia. A guard, never a ban.

Some countries license who may provide calls over IP. What Faxbot found, from
the regulators' own pages:

- **United Arab Emirates.** TDRA's FAQ (tdra.gov.ae/en/FAQs, read
  2026-10-10) says VoIP services are telecommunications services, regulated
  by its VoIP Regulatory Policy version 2.0 of 30 December 2009; "Licensees
  are allowed to provide VoIP Services in the UAE"; third parties may provide
  them "in collaboration with the licensees" or with TDRA's approval; and
  "international telecom providers are not licensed in the UAE to provide VoIP
  Services". A business connecting its offices "must seek such services from a
  Licensee", while VoIP hardware and software inside a private network needs no
  licence. The public licensees are e& (Etisalat) and du.
- **Saudi Arabia.** CST's permit for virtual voice services (VVSP;
  cst.gov.sa, read 2026-10-10) covers services that let users in the Kingdom
  make and receive calls to and from the telephone network over IP with
  E.164 numbers, issued under CST's classification of licences (Decision
  510/1445 of 8 February 2024). A vendor guide says calls and media must stay
  in the Kingdom; CST's pages Faxbot read do not say so, so Faxbot does not
  repeat it as a rule.

Faxbot cannot see a carrier's licence or contract. So for each sending or
receiving account whose site is in one of these countries (or, with no site,
an installation set to one), the account shows these rules with their
sources and "not confirmed" until you confirm, with your evidence, that its
provider meets them (``confirm``). Nothing is blocked: a foreign provider may
work with a licensee, and an unknown rule never becomes a ban. Faxes sent to
these countries from elsewhere use your own carrier's international route;
these rules concern services provided there.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import re
from uuid import uuid4

import sqlalchemy as sa


READ_ON = '2026-10-10'
RULES = {
    'AE': {
        'country': 'the United Arab Emirates', 'regulator': 'TDRA',
        'sentence': ('In the UAE, calls over the internet are licensed telecommunications services: they must come '
                     'from a TDRA licensee (e& or du), from a provider working with one, or from one TDRA approved. '
                     'International providers are not licensed there.'),
        'needs': 'that this account\'s provider is a TDRA licensee, works with one, or is approved by TDRA',
        'sources': (('TDRA: frequently asked questions (VoIP Regulatory Policy 2.0)', 'https://tdra.gov.ae/en/FAQs'),),
    },
    'SA': {
        'country': 'Saudi Arabia', 'regulator': 'CST',
        'sentence': ('In Saudi Arabia, calls to and from the telephone network over the internet need a CST licence '
                     'or its permit for virtual voice services (VVSP), held by your provider.'),
        'needs': 'that this account\'s provider holds a CST licence or its virtual voice services permit',
        'sources': (('CST: permit to provide virtual voice services (VVSP)',
                     'https://www.cst.gov.sa/en/business/services/Permit-to-provide-virtual-voice-services-VVSP'),
                    ('CST: updating the regulations and licences for telecommunications services (Decision 510/1445)',
                     'https://www.cst.gov.sa/en/regulations-and-licenses/decisions/Regulation-459')),
    },
}
CONFIRMED, WITHDRAWN = 'confirmed', 'withdrawn'


class EligibilityError(ValueError):
    """A confirmation Faxbot cannot record; one plain sentence."""


ELIGIBILITY = sa.table('service_eligibility', *(sa.column(name, sa.DateTime() if name == 'created_at' else sa.String())
                                               for name in ('id', 'account', 'country', 'state', 'evidence',
                                                            'evidence_url', 'recorded_by', 'recorded_by_name',
                                                            'created_at')))


def _newest(connection, account, country):
    return connection.execute(sa.select(ELIGIBILITY).where(
        ELIGIBILITY.c.account == account, ELIGIBILITY.c.country == country)
        .order_by(ELIGIBILITY.c.created_at.desc(), ELIGIBILITY.c.id.desc()).limit(1)).mappings().first()


def _write(engine, account, country, state, evidence, evidence_url, actor, now):
    from .database import utcnow, write_transaction
    actor = actor or {}
    with write_transaction(engine) as connection:
        newest = _newest(connection, account, country)
        moment = now or utcnow()
        if newest is not None:
            created = newest['created_at']
            if isinstance(created, str):
                created = datetime.fromisoformat(created)
            moment = max(moment, created + timedelta(microseconds=1))
        connection.execute(ELIGIBILITY.insert().values(
            id=uuid4().hex, account=account, country=country, state=state, evidence=evidence,
            evidence_url=evidence_url, recorded_by=actor.get('id'), recorded_by_name=actor.get('name'),
            created_at=moment))


def confirm(engine, account, country, *, evidence, evidence_url=None, actor=None, now=None):
    """Record that ``account``'s provider meets ``country``'s rules, with your evidence."""
    country = str(country or '').upper()
    if country not in RULES:
        raise EligibilityError('Faxbot records confirmations for the UAE (AE) and Saudi Arabia (SA).')
    if not isinstance(evidence, str) or not evidence.strip() or len(evidence) > 2000:
        raise EligibilityError('Say how you know, such as the provider\'s licence or your contract, in up to 2,000 '
                               'characters.')
    if evidence_url and re.fullmatch(r'https?://\S+', evidence_url) is None:
        raise EligibilityError('The evidence link must be a web address.')
    _write(engine, account, country, CONFIRMED, evidence.strip(), evidence_url or None, actor, now)


def withdraw(engine, account, country, *, actor=None, now=None):
    country = str(country or '').upper()
    from .database import read_connection
    with read_connection(engine) as connection:
        newest = _newest(connection, account, country)
    if newest is None or newest['state'] != CONFIRMED:
        raise EligibilityError('This account has no confirmation for that country.')
    _write(engine, account, country, WITHDRAWN, None, None, actor, now)


def _country_of(values, account, sites):
    from .origin_rates import account_site
    site = account_site(values, account.key, sites)
    country = ((sites or {}).get(site) or {}).get('country') if site else None
    return str(country or getattr(values, 'fax_default_country', '') or '').upper()


def view(engine, values):
    """Every account in a country with rules, its state (confirmed or not), and the rules with their sources."""
    from ..accounts import all_accounts
    from .database import read_connection
    from .origin_rates import organization_sites
    sites = organization_sites(engine)
    found = []
    with read_connection(engine) as connection:
        for account in all_accounts(values):
            if not (account.sends or account.receives):
                continue
            country = _country_of(values, account, sites)
            rule = RULES.get(country)
            if rule is None:
                continue
            newest = _newest(connection, account.key, country)
            confirmed = newest is not None and newest['state'] == CONFIRMED
            found.append({
                'account': account.key, 'label': account.label, 'country': country, 'confirmed': confirmed,
                'evidence': newest['evidence'] if confirmed else None,
                'recorded_by': newest['recorded_by_name'] if confirmed else None,
                'sentence': (f'{account.label}: confirmed by {newest["recorded_by_name"] or "you"}.' if confirmed
                             else f'{account.label}: not confirmed. Faxes still go; confirm {rule["needs"]}.')})
    return {'accounts': found, 'read_on': READ_ON,
            'countries': [{'country': code, 'name': rule['country'], 'regulator': rule['regulator'],
                           'sentence': rule['sentence'],
                           'sources': [{'label': label, 'url': url} for label, url in rule['sources']]}
                          for code, rule in RULES.items()]}
