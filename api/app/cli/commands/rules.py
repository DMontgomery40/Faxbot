"""Sending rules: which provider account carries each fax, and the limits every fax must meet.

`faxbot delivery rules` edits a draft of the rules for one scope (the organization, a mailbox or a
workflow), checks it and publishes it, as Providers -> Rules does in the console. Each rule reads as
one sentence; the console builds the same sentences (ProviderRulesText.ts), and a shared fixture
(admin_ui/src/__tests__/providerRulesSentences.json) keeps the two identical.

The held-fax commands at the end (route, approve, refuse, held) belong to `faxbot faxes sent`.
"""
import copy
from decimal import Decimal, InvalidOperation
import json
import re
import sys
from pathlib import Path

import typer

from .. import state
from ..client import segment
from ..errors import EXIT_FAILURE, EXIT_NOT_FOUND, CliError
from ..output import home_currency, local_time, money_amount

# -- rules in words --------------------------------------------------------------------------------

BUILT_IN_ROUTES = {'direct': 'direct delivery', 'local': 'delivery inside Faxbot'}
DAYS = ('mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun')
DAY_NAMES = {'mon': 'Monday', 'tue': 'Tuesday', 'wed': 'Wednesday', 'thu': 'Thursday', 'fri': 'Friday',
             'sat': 'Saturday', 'sun': 'Sunday'}
AUTOMATIC_ROW = 'Everything else: the cheapest reliable route, as before.'


# English country names from CLDR 47, the data the console's browser uses, so the command line names a
# country exactly as the console does even where the babel package is not installed.
_COUNTRIES = (
    'AC=Ascension Island|AD=Andorra|AE=United Arab Emirates|AF=Afghanistan|AG=Antigua & Barbuda|'
    'AI=Anguilla|AL=Albania|AM=Armenia|AN=Curaçao|AO=Angola|AQ=Antarctica|AR=Argentina|AS=American Samoa|'
    'AT=Austria|AU=Australia|AW=Aruba|AX=Åland Islands|AZ=Azerbaijan|BA=Bosnia & Herzegovina|BB=Barbados|'
    'BD=Bangladesh|BE=Belgium|BF=Burkina Faso|BG=Bulgaria|BH=Bahrain|BI=Burundi|BJ=Benin|'
    'BL=St. Barthélemy|BM=Bermuda|BN=Brunei|BO=Bolivia|BQ=Caribbean Netherlands|BR=Brazil|BS=Bahamas|'
    'BT=Bhutan|BU=Myanmar (Burma)|BV=Bouvet Island|BW=Botswana|BY=Belarus|BZ=Belize|CA=Canada|'
    'CC=Cocos (Keeling) Islands|CD=Congo - Kinshasa|CF=Central African Republic|CG=Congo - Brazzaville|'
    'CH=Switzerland|CI=Côte d’Ivoire|CK=Cook Islands|CL=Chile|CM=Cameroon|CN=China|CO=Colombia|'
    'CP=Clipperton Island|CQ=Sark|CR=Costa Rica|CS=Serbia|CU=Cuba|CV=Cape Verde|CW=Curaçao|'
    'CX=Christmas Island|CY=Cyprus|CZ=Czechia|DD=Germany|DE=Germany|DG=Diego Garcia|DJ=Djibouti|'
    'DK=Denmark|DM=Dominica|DO=Dominican Republic|DY=Benin|DZ=Algeria|EA=Ceuta & Melilla|EC=Ecuador|'
    'EE=Estonia|EG=Egypt|EH=Western Sahara|ER=Eritrea|ES=Spain|ET=Ethiopia|FI=Finland|FJ=Fiji|'
    'FK=Falkland Islands|FM=Micronesia|FO=Faroe Islands|FR=France|FX=France|GA=Gabon|GB=United Kingdom|'
    'GD=Grenada|GE=Georgia|GF=French Guiana|GG=Guernsey|GH=Ghana|GI=Gibraltar|GL=Greenland|GM=Gambia|'
    'GN=Guinea|GP=Guadeloupe|GQ=Equatorial Guinea|GR=Greece|GS=South Georgia & South Sandwich Islands|'
    'GT=Guatemala|GU=Guam|GW=Guinea-Bissau|GY=Guyana|HK=Hong Kong SAR China|HM=Heard & McDonald Islands|'
    'HN=Honduras|HR=Croatia|HT=Haiti|HU=Hungary|HV=Burkina Faso|IC=Canary Islands|ID=Indonesia|'
    'IE=Ireland|IL=Israel|IM=Isle of Man|IN=India|IO=British Indian Ocean Territory|IQ=Iraq|IR=Iran|'
    'IS=Iceland|IT=Italy|JE=Jersey|JM=Jamaica|JO=Jordan|JP=Japan|KE=Kenya|KG=Kyrgyzstan|KH=Cambodia|'
    'KI=Kiribati|KM=Comoros|KN=St. Kitts & Nevis|KP=North Korea|KR=South Korea|KW=Kuwait|'
    'KY=Cayman Islands|KZ=Kazakhstan|LA=Laos|LB=Lebanon|LC=St. Lucia|LI=Liechtenstein|LK=Sri Lanka|'
    'LR=Liberia|LS=Lesotho|LT=Lithuania|LU=Luxembourg|LV=Latvia|LY=Libya|MA=Morocco|MC=Monaco|MD=Moldova|'
    'ME=Montenegro|MF=St. Martin|MG=Madagascar|MH=Marshall Islands|MK=North Macedonia|ML=Mali|'
    'MM=Myanmar (Burma)|MN=Mongolia|MO=Macao SAR China|MP=Northern Mariana Islands|MQ=Martinique|'
    'MR=Mauritania|MS=Montserrat|MT=Malta|MU=Mauritius|MV=Maldives|MW=Malawi|MX=Mexico|MY=Malaysia|'
    'MZ=Mozambique|NA=Namibia|NC=New Caledonia|NE=Niger|NF=Norfolk Island|NG=Nigeria|NH=Vanuatu|'
    'NI=Nicaragua|NL=Netherlands|NO=Norway|NP=Nepal|NR=Nauru|NU=Niue|NZ=New Zealand|OM=Oman|PA=Panama|'
    'PE=Peru|PF=French Polynesia|PG=Papua New Guinea|PH=Philippines|PK=Pakistan|PL=Poland|'
    'PM=St. Pierre & Miquelon|PN=Pitcairn Islands|PR=Puerto Rico|PS=Palestinian Territories|PT=Portugal|'
    'PW=Palau|PY=Paraguay|QA=Qatar|RE=Réunion|RH=Zimbabwe|RO=Romania|RS=Serbia|RU=Russia|RW=Rwanda|'
    'SA=Saudi Arabia|SB=Solomon Islands|SC=Seychelles|SD=Sudan|SE=Sweden|SG=Singapore|SH=St. Helena|'
    'SI=Slovenia|SJ=Svalbard & Jan Mayen|SK=Slovakia|SL=Sierra Leone|SM=San Marino|SN=Senegal|SO=Somalia|'
    'SR=Suriname|SS=South Sudan|ST=São Tomé & Príncipe|SU=Russia|SV=El Salvador|SX=Sint Maarten|SY=Syria|'
    'SZ=Eswatini|TA=Tristan da Cunha|TC=Turks & Caicos Islands|TD=Chad|TF=French Southern Territories|'
    'TG=Togo|TH=Thailand|TJ=Tajikistan|TK=Tokelau|TL=Timor-Leste|TM=Turkmenistan|TN=Tunisia|TO=Tonga|'
    'TP=Timor-Leste|TR=Türkiye|TT=Trinidad & Tobago|TV=Tuvalu|TW=Taiwan|TZ=Tanzania|UA=Ukraine|UG=Uganda|'
    'UK=United Kingdom|UM=U.S. Outlying Islands|US=United States|UY=Uruguay|UZ=Uzbekistan|'
    'VA=Vatican City|VC=St. Vincent & Grenadines|VD=Vietnam|VE=Venezuela|VG=British Virgin Islands|'
    'VI=U.S. Virgin Islands|VN=Vietnam|VU=Vanuatu|WF=Wallis & Futuna|WS=Samoa|YD=Yemen|YE=Yemen|'
    'YT=Mayotte|YU=Serbia|ZA=South Africa|ZM=Zambia|ZR=Congo - Kinshasa|ZW=Zimbabwe'
)
COUNTRY_NAMES = dict(item.split('=', 1) for item in _COUNTRIES.split('|'))


def country_name(code):
    """The English name of a country code, as the console's browser gives it, or the code itself."""
    if code in COUNTRY_NAMES:
        return COUNTRY_NAMES[code]
    try:
        from babel import Locale
        return Locale('en').territories.get(code) or code
    except Exception:  # babel is optional; the code is still exact
        return code


class Names:
    """What each key and id in a rule is called, as the API's choices and the document give them."""

    def __init__(self, maps=None, *, money=None):
        maps = maps or {}
        self._maps = {name: dict(maps.get(name) or {}) for name in (
            'accounts', 'lists', 'regions', 'sites', 'workflows', 'people', 'recipients', 'keys', 'groups', 'mailboxes',
            'countries')}
        self._money = money or money_amount

    def _pick(self, kind, value, fallback):
        return self._maps[kind].get(value, fallback)

    def account(self, key):
        return self._pick('accounts', key, BUILT_IN_ROUTES.get(key, key))

    def list(self, key):
        return self._pick('lists', key, key)

    def region(self, key):
        return self._pick('regions', key, key)

    def site(self, key):
        return self._pick('sites', key, key)

    def workflow(self, key):
        return self._pick('workflows', key, key)

    def person(self, value):
        return self._pick('people', value, 'an unknown person')

    def recipient(self, value):
        return self._pick('recipients', value, 'an unknown recipient')

    def key(self, value):
        return self._pick('keys', value, 'an unknown key')

    def group(self, value):
        return self._pick('groups', value, 'an unknown group')

    def mailbox(self, value):
        return self._pick('mailboxes', value, 'an unknown mailbox')

    def country(self, code):
        return self._maps['countries'].get(code) or country_name(code)

    def money(self, value):
        return self._money(value)


def recipient_lists(document):
    return {key: value for key, value in ((document or {}).get('lists') or {}).items() if isinstance(value, dict)}


def document_labels(document):
    return list((document or {}).get('labels') or [])


def names_for(document, choices):
    """The names a rules command knows: the accounts and access names from the API, and the document's own."""
    document = document or {}
    choices = choices or {}
    return Names({
        'accounts': {item['key']: item['label'] for item in choices.get('accounts') or []},
        'lists': {key: item.get('name') or key for key, item in recipient_lists(document).items()},
        'regions': {key: item.get('name') or key for key, item in (document.get('regions') or {}).items()},
        'sites': {item['key']: item.get('name') or item['key'] for item in document.get('sites') or []},
        'workflows': {item['key']: item.get('name') or item['key'] for item in document.get('workflows') or []},
        'people': {item['id']: item['name'] for item in choices.get('people') or []},
        'recipients': {item['id']: item['name'] for item in choices.get('recipients') or []},
        'keys': {item['id']: item['name'] for item in choices.get('keys') or []},
        'groups': {item['id']: item['name'] for item in choices.get('groups') or []},
        'mailboxes': {item['id']: item['name'] for item in choices.get('mailboxes') or []},
    })


def _joined(items, word):
    items = list(items)
    if len(items) <= 1:
        return items[0] if items else ''
    return f"{', '.join(items[:-1])} {word} {items[-1]}"


def join_and(items):
    return _joined(items, 'and')


def join_or(items):
    return _joined(items, 'or')


def days_text(days):
    if not days:
        return None
    ordered = [day for day in DAYS if day in set(days)]
    if len(ordered) == 7:
        return 'every day'
    if ordered == ['mon', 'tue', 'wed', 'thu', 'fri']:
        return 'on weekdays'
    if ordered == ['sat', 'sun']:
        return 'at weekends'
    return 'on ' + join_and(DAY_NAMES[day] for day in ordered)


def window_text(time):
    """"on weekdays between 18:00 and 07:00 (the sender's site's time)"."""
    if not time:
        return None
    parts = []
    days = days_text(time.get('days'))
    if days:
        parts.append(days)
    start, end = time.get('from'), time.get('until')
    if start and end:
        parts.append(f'between {start} and {end}')
    elif start:
        parts.append(f'after {start}')
    elif end:
        parts.append(f'before {end}')
    if not parts:
        return None
    zone = " (the sender's site's time)" if time.get('time_zone') == 'sender_site' else ''
    return ' '.join(parts) + zone


def _plural(count, one, many):
    return f'{count} {one if count == 1 else many}'


def _megabytes(size):
    mb = size / 1_000_000
    return str(int(mb)) if mb == int(mb) else f'{round(mb, 1):g}'


def _flag(value, yes, no):
    if value is None:
        return []
    return [yes if value else no]


def condition_clauses(conditions, names):
    """Each condition of a when or unless block as a clause, in the console's order."""
    if not conditions:
        return []
    clauses = []
    destination = conditions.get('destination') or {}
    numbers = destination.get('numbers') or []
    if len(numbers) > 3:
        clauses.append(f'the number is one of {len(numbers)} numbers')
    elif numbers:
        clauses.append(f'the number is {join_or(numbers)}')
    if destination.get('lists'):
        clauses.append(f"the number is in {join_or(names.list(key) for key in destination['lists'])}")
    if destination.get('prefixes'):
        clauses.append(f"the number starts with {join_or(destination['prefixes'])}")
    if destination.get('countries'):
        clauses.append(f"the destination country is {join_or(names.country(code) for code in destination['countries'])}")
    if destination.get('regions'):
        clauses.append(f"the number is in {join_or(names.region(key) for key in destination['regions'])}")
    if destination.get('recipients'):
        clauses.append(f"the recipient is {join_or(names.recipient(value) for value in destination['recipients'])}")
    clauses += _flag(destination.get('partner'), 'the number has a verified partner', 'the number has no verified partner')
    clauses += _flag(destination.get('own_number'), 'the number is one of your own numbers',
                     'the number is not one of your own numbers')
    clauses += _flag(destination.get('approved_alternate'), 'the recipient has an approved alternate number',
                     'the recipient has no approved alternate number')
    clauses += _flag(destination.get('in_sender_country'), "the destination is in the sender's site's country",
                     "the destination is outside the sender's site's country")
    sender = conditions.get('sender') or {}
    if sender.get('people'):
        clauses.append(f"the sender is {join_or(names.person(value) for value in sender['people'])}")
    if sender.get('keys'):
        clauses.append(f"the fax comes from the key {join_or(names.key(value) for value in sender['keys'])}")
    if sender.get('groups'):
        clauses.append(f"the sender is in {join_or(names.group(value) for value in sender['groups'])}")
    if sender.get('mailboxes'):
        clauses.append(f"the fax is sent from the mailbox {join_or(names.mailbox(value) for value in sender['mailboxes'])}")
    if sender.get('sites'):
        clauses.append(f"the fax is sent from {join_or(names.site(key) for key in sender['sites'])}")
    if conditions.get('workflows'):
        clauses.append(f"the fax is part of {join_or(names.workflow(key) for key in conditions['workflows'])}")
    document = conditions.get('document') or {}
    if document.get('pages_over') is not None:
        clauses.append(f"the fax has more than {_plural(document['pages_over'], 'page', 'pages')}")
    if document.get('pages_under') is not None:
        clauses.append(f"the fax has fewer than {_plural(document['pages_under'], 'page', 'pages')}")
    if document.get('size_over') is not None:
        clauses.append(f"the file is larger than {_megabytes(document['size_over'])} MB")
    clauses += _flag(document.get('case_packet'), 'the fax is a case packet', 'the fax is not a case packet')
    clauses += _flag(conditions.get('urgent'), 'the fax is marked urgent', 'the fax is not marked urgent')
    clauses += _flag(conditions.get('real_call'), 'the sender asked for a real call', 'the sender did not ask for a real call')
    if conditions.get('labels'):
        clauses.append(f"the fax is labelled {join_or(conditions['labels'])}")
    window = window_text(conditions.get('time'))
    if window:
        clauses.append(f'the fax is sent {window}')
    return clauses


def accounts_in_order(keys, names):
    return ', then '.join(names.account(key) for key in keys)


def action_phrases(then, names):
    """What a rule does, as phrases joined into the sentence."""
    phrases = []
    if then.get('use'):
        phrases.append(f"use {names.account(then['use'])}")
    ordered = then.get('try_in_order') or []
    if ordered:
        phrases.append(f'use {names.account(ordered[0])}' if len(ordered) == 1 else f'try {accounts_in_order(ordered, names)}')
    cheapest = then.get('cheapest_reliable') or []
    if cheapest:
        phrases.append(f'use {names.account(cheapest[0])}' if len(cheapest) == 1
                       else f"use the cheapest reliable of {join_and(names.account(key) for key in cheapest)}")
    if then.get('site_accounts'):
        site = then['site_accounts']
        whose = "the sender's site's" if site == 'sender' else f"{names.site(site)}'s"
        order = 'in order' if then.get('mode') == 'ordered' else 'cheapest reliable first'
        phrases.append(f'use {whose} accounts, {order}')
    if then.get('automatic'):
        phrases.append('choose the cheapest reliable route, as before')
    if then.get('never'):
        phrases.append(f"never use {join_or(names.account(key) for key in then['never'])}")
    if then.get('require_direct'):
        phrases.append('send only by direct delivery to a verified partner')
    if then.get('require_encryption'):
        phrases.append('send only encrypted (direct delivery, or SSL Fax where this number has used it before)')
    if then.get('cap_cost'):
        phrases.append(f"use only routes that cost at most {names.money(then['cap_cost'])} for the fax")
    if then.get('hold_for_approval') is not None:
        phrases.append('hold the fax for approval by someone other than the sender'
                       if (then['hold_for_approval'] or {}).get('separate_approver') else 'hold the fax for approval')
    window = window_text(then.get('hold_until'))
    if window:
        phrases.append(f'send the fax only {window}')
    if then.get('place_a_real_call'):
        phrases.append('place a real call, even to your own numbers')
    return phrases


def setting_sentences(then):
    sentences = []
    busy = then.get('when_busy')
    if busy == 'next':
        sentences.append('If every line is busy, Faxbot uses the next account.')
    if busy == 'wait':
        sentences.append('If every line is busy, Faxbot waits for a free line.')
    layout = then.get('page_layout')
    if layout == 'as_receiver_allows':
        sentences.append('Pages per sheet: as the receiving machine allows.')
    if layout == 'one_per_sheet':
        sentences.append('Pages per sheet: one.')
    alternate = then.get('alternate_number')
    if alternate == 'use':
        sentences.append("Faxbot dials the recipient's approved alternate number when there is one.")
    if alternate == 'only':
        sentences.append("Faxbot dials only the recipient's approved alternate number, and holds the fax when there is none.")
    if alternate == 'never':
        sentences.append('Faxbot always dials the number the sender gave.')
    if then.get('subaddress'):
        sentences.append(f"The fax asks for subaddress {then['subaddress']} at the recipient's number.")
    return sentences


def rule_sentence(rule, names):
    """The whole rule in words; conditions are joined by "and", as in the console."""
    when = condition_clauses(rule.get('when'), names)
    unless = condition_clauses(rule.get('unless'), names)
    opening = f"When {' and '.join(when)}" if when else 'For every fax'
    if unless:
        opening += f", unless {' and '.join(unless)}"
    actions = action_phrases(rule.get('then') or {}, names)
    main = f"{opening}, {join_and(actions) if actions else 'change nothing'}."
    return ' '.join([main, *setting_sentences(rule.get('then') or {})])


def minutes_text(minutes):
    """Minutes after midnight as a 24-hour time ("18:00")."""
    return f'{(minutes // 60) % 24:02d}:{minutes % 60:02d}'


def receiving_sentence(rule, names, connectors=None):
    """A number rule in words, as the console's Numbers page reads it."""
    sentence = 'Faxes to any of your numbers' if rule.get('any_number') or not rule.get('to_number') \
        else f"Faxes to {rule['to_number']}"
    if rule.get('subaddress'):
        sentence += f" with subaddress {rule['subaddress']}"
    if rule.get('diverted_from'):
        sentence += (f" forwarded from {rule['diverted_from']}"
                     + (' (verified or not)' if rule.get('diversion_unsigned') else ''))
    if rule.get('account_key'):
        sentence += f" received on {names.account(rule['account_key'])}"
    elif rule.get('site_key'):
        sentence += f" received on an account of {names.site(rule['site_key'])}"
    sources = [f'numbers starting with {entry[:-1]}' if entry.endswith('*') else entry
               for entry in rule.get('from_numbers') or []]
    if sources:
        sentence += f' from {join_or(sources)}'
    window = window_text({
        'days': rule.get('days') or [],
        'from': None if rule.get('start_minute') is None else minutes_text(rule['start_minute']),
        'until': None if rule.get('end_minute') is None else minutes_text(rule['end_minute'])})
    if window:
        sentence += f' {window}'
    sentence += f" go to {rule['mailbox_label']}"
    if rule.get('urgent'):
        sentence += ', marked urgent'
    if rule.get('email_off'):
        sentence += ', with no email'
    elif rule.get('email_connector_id'):
        sentence += f", emailed through {(connectors or {}).get(rule['email_connector_id'], 'another email connector')}"
    if rule.get('keep_days'):
        sentence += f", kept for {'1 day' if rule['keep_days'] == 1 else str(rule['keep_days']) + ' days'}"
    return sentence + '.'


RECEIVING_OPTION_KEYS = ('enabled', 'any_number', 'account_key', 'site_key', 'subaddress', 'from_numbers', 'days',
                         'start_minute', 'end_minute', 'email_connector_id', 'email_off', 'urgent', 'keep_days',
                         'diverted_from', 'diversion_unsigned')

KEEP_DAYS_NOTE = ('This is when cleanup removes the fax from Faxbot. It is not a legal hold, and it does not promise to '
                  'keep the fax that long.')


# -- reading and saving a scope's draft ---------------------------------------------------------------

rules = typer.Typer(help='Sending rules: which provider account carries each fax, and limits every fax must meet. '
                         'Changes go into a draft until you publish it.', no_args_is_help=True)
lists_app = typer.Typer(help='Recipient groups that rules can name, and the labels senders can put on a fax.',
                        no_args_is_help=True)
regions_app = typer.Typer(help='Regions: named sets of countries and number prefixes that rules can name.',
                          no_args_is_help=True)
sites_app = typer.Typer(help='Sites: the places your organization sends from, with their mailboxes and groups.',
                        no_args_is_help=True)
workflows_app = typer.Typer(help='Workflows: named kinds of work, such as referrals, that can have their own rules.',
                            no_args_is_help=True)
rules.add_typer(lists_app, name='lists')
rules.add_typer(regions_app, name='regions')
rules.add_typer(sites_app, name='sites')
rules.add_typer(workflows_app, name='workflows')

SCOPE = typer.Option('organization', '--scope', metavar='SCOPE',
                     help="Whose rules: organization (the default), mailbox:NAME or workflow:KEY.")


def scope_param(api, scope):
    """The API's scope value for what was typed: organization, mailbox:ID or workflow:KEY."""
    value = (scope or 'organization').strip()
    kind, _, name = value.partition(':')
    kind = kind.strip().casefold()
    if kind == 'organization' and not name:
        return 'organization'
    if kind == 'mailbox' and name.strip():
        from .. import resolve
        return 'mailbox:' + resolve.mailbox(api, name.strip())['id']
    if kind == 'workflow' and name.strip():
        return 'workflow:' + name.strip()
    raise CliError("Name the rules as organization, mailbox:NAME or workflow:KEY.", EXIT_FAILURE)


def load(api, scope):
    return api.get('/routing/rules', params={'scope': scope})


def definitions(current, document):
    """The document whose lists, regions, sites, workflows and labels a rule may name: the organization's
    rules for a mailbox's or a workflow's own rules."""
    organization = current.get('organization')
    return organization['document'] if organization else document


def working_document(current):
    """The draft's document, or a copy of the active one, or an empty one."""
    if current.get('draft'):
        return copy.deepcopy(current['draft']['document'])
    if current.get('active'):
        return copy.deepcopy(current['active']['document'])
    return {'format': 1, 'limits': [], 'routes': []}


def draft_version(current):
    return (current.get('draft') or {}).get('version', 0)


def save(api, scope, current, document, message):
    """Save the draft and say what changed and what to do next."""
    draft = api.put('/routing/rules/draft', params={'scope': scope},
                    json={'document': document, 'expected_version': draft_version(current)})
    out = state.out()

    def human(out):
        out.line(message)
        check = draft.get('check') or {}
        for issue in check.get('errors') or []:
            out.line('Needs fixing before you publish: ' + issue['message'])
        out.line("Your changes are in the draft. Publish them with 'faxbot delivery rules publish --note TEXT'.")
    out.result(draft, human)
    return draft


def _rule_lookup(document, reference):
    """(section, index) of a rule by its id, or by its name."""
    for section in ('limits', 'routes'):
        for index, rule in enumerate(document.get(section) or []):
            if rule.get('id') == reference:
                return section, index
    matches = [(section, index) for section in ('limits', 'routes')
               for index, rule in enumerate(document.get(section) or [])
               if (rule.get('name') or '').strip().casefold() == reference.strip().casefold()]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise CliError(f"No rule is called '{reference}'. See 'faxbot delivery rules list'.", EXIT_NOT_FOUND)
    raise CliError(f"More than one rule is called '{reference}'. Use its id from 'faxbot delivery rules list'.")


def new_rule_id(document, section, name):
    """A short id from the rule's name: l-never-uk for a limit, r-uk-numbers for a routing rule."""
    slug = re.sub(r'[^a-z0-9]+', '-', (name or '').casefold()).strip('-')[:40].strip('-') or 'rule'
    base = ('l-' if section == 'limits' else 'r-') + slug
    taken = {rule.get('id') for part in ('limits', 'routes') for rule in document.get(part) or []}
    candidate, count = base, 2
    while candidate in taken:
        candidate, count = f'{base}-{count}', count + 1
    return candidate


# -- conditions and actions from options --------------------------------------------------------------

YES = {'yes', 'y', 'true', 'on', '1'}
NO = {'no', 'n', 'false', 'off', '0'}
TIME = re.compile(r'^([01]\d|2[0-3]):[0-5]\d$')

# --when and --unless fields: (block, key, kind). Kinds: list (several values), flag (yes/no), count, size, days, between.
CONDITION_FIELDS = {
    'to-number': ('destination', 'numbers', 'list'),
    'to-list': ('destination', 'lists', 'list'),
    'to-prefix': ('destination', 'prefixes', 'list'),
    'to-country': ('destination', 'countries', 'list'),
    'to-region': ('destination', 'regions', 'list'),
    'to-recipient': ('destination', 'recipients', 'list'),
    'partner': ('destination', 'partner', 'flag'),
    'own-number': ('destination', 'own_number', 'flag'),
    'approved-alternate': ('destination', 'approved_alternate', 'flag'),
    'in-site-country': ('destination', 'in_sender_country', 'flag'),
    'from-person': ('sender', 'people', 'list'),
    'from-key': ('sender', 'keys', 'list'),
    'from-group': ('sender', 'groups', 'list'),
    'from-mailbox': ('sender', 'mailboxes', 'list'),
    'from-site': ('sender', 'sites', 'list'),
    'workflow': (None, 'workflows', 'list'),
    'pages-over': ('document', 'pages_over', 'count'),
    'pages-under': ('document', 'pages_under', 'count'),
    'larger-than-mb': ('document', 'size_over', 'size'),
    'case-packet': ('document', 'case_packet', 'flag'),
    'urgent': (None, 'urgent', 'flag'),
    'real-call': (None, 'real_call', 'flag'),
    'label': (None, 'labels', 'list'),
    'days': ('time', 'days', 'days'),
    'between': ('time', 'between', 'between'),
    'site-time': ('time', 'time_zone', 'flag'),
}

WHEN_HELP = ('A condition, FIELD=VALUE; give several, and every one must match. Fields: '
             + ', '.join(CONDITION_FIELDS) + '. Several values: to-country=GB,IE. Yes or no fields: partner=yes. '
             'days=mon-fri or weekends; between=18:00-07:00.')


def parse_days(value):
    text = value.strip().casefold()
    if text in {'every day', 'everyday', 'all', 'daily'}:
        return list(DAYS)
    if text in {'weekdays', 'weekday'}:
        return list(DAYS[:5])
    if text in {'weekends', 'weekend'}:
        return ['sat', 'sun']
    chosen = []
    for part in re.split(r'[,\s]+', text):
        if not part:
            continue
        start, _, end = part.partition('-')
        start, end = start[:3], end[:3]
        if start not in DAYS or (end and end not in DAYS):
            raise CliError(f"Write days as mon-fri, weekdays, weekends or mon,wed,fri, not '{value}'.")
        if end:
            first, last = DAYS.index(start), DAYS.index(end)
            span = DAYS[first:last + 1] if first <= last else DAYS[first:] + DAYS[:last + 1]
            chosen.extend(span)
        else:
            chosen.append(start)
    return [day for day in DAYS if day in set(chosen)]


def parse_between(value):
    start, _, end = value.replace(' ', '').partition('-')
    if not TIME.match(start) or not TIME.match(end) or start == end:
        raise CliError(f"Write a time window as 18:00-07:00, not '{value}'.")
    return start, end


def _yes_no(field, value):
    text = value.strip().casefold()
    if text in YES:
        return True
    if text in NO:
        return False
    raise CliError(f'Write {field}=yes or {field}=no.')


def _resolve_named(choices, kind, value, what, hint):
    """An id from the API's choices, by name or id."""
    items = (choices or {}).get(kind) or []
    found = [item for item in items if item['id'] == value] or [
        item for item in items if item['name'].strip().casefold() == value.strip().casefold()]
    if len(found) == 1:
        return found[0]['id']
    if not found:
        raise CliError(f"No {what} is called '{value}'. {hint}", EXIT_NOT_FOUND)
    raise CliError(f"More than one {what} is called '{value}'.")


def _resolve_key(table, value, what, hint):
    """A list, region, site or workflow key, by key or name."""
    if value in table:
        return value
    found = [key for key, name in table.items() if (name or '').strip().casefold() == value.strip().casefold()]
    if len(found) == 1:
        return found[0]
    raise CliError(f"No {what} is called '{value}'. {hint}", EXIT_NOT_FOUND)


def conditions_from(pairs, document, choices):
    """A when or unless block from FIELD=VALUE pairs."""
    block = {}
    lists = {key: item.get('name') for key, item in recipient_lists(document).items()}
    regions = {key: item.get('name') for key, item in (document.get('regions') or {}).items()}
    sites = {item['key']: item.get('name') for item in document.get('sites') or []}
    workflows = {item['key']: item.get('name') for item in document.get('workflows') or []}
    for pair in pairs or []:
        field, sep, value = pair.partition('=')
        field = field.strip().casefold()
        if not sep or field not in CONDITION_FIELDS:
            raise CliError(f"Write each condition as FIELD=VALUE with one of: {', '.join(CONDITION_FIELDS)}.")
        part, key, kind = CONDITION_FIELDS[field]
        target = block if part is None else block.setdefault(part, {})
        if kind == 'list':
            values = [item.strip() for item in value.split(',') if item.strip()] if field != 'to-number' else [
                value.strip()]
            resolved = []
            for item in values:
                if field == 'to-country':
                    item = item.upper()
                elif field == 'to-recipient':
                    item = _resolve_named(choices, 'recipients', item, 'saved recipient', "See 'faxbot recipients list'.")
                elif field == 'from-person':
                    item = _resolve_named(choices, 'people', item, 'person or integration', "See 'faxbot admin access users list'.")
                elif field == 'from-key':
                    item = _resolve_named(choices, 'keys', item, 'key', "See 'faxbot admin access keys list'.")
                elif field == 'from-group':
                    item = _resolve_named(choices, 'groups', item, 'group', "See 'faxbot admin access groups list'.")
                elif field == 'from-mailbox':
                    item = _resolve_named(choices, 'mailboxes', item, 'mailbox', "See 'faxbot delivery mailboxes list'.")
                elif field == 'to-list':
                    item = _resolve_key(lists, item, 'recipient group', "See 'faxbot delivery rules lists list'.")
                elif field == 'to-region':
                    item = _resolve_key(regions, item, 'region', "See 'faxbot delivery rules regions list'.")
                elif field == 'from-site':
                    item = _resolve_key(sites, item, 'site', "See 'faxbot delivery rules sites list'.")
                elif field == 'workflow':
                    item = _resolve_key(workflows, item, 'workflow', "See 'faxbot delivery rules workflows list'.")
                resolved.append(item)
            target.setdefault(key, [])
            target[key] += [item for item in resolved if item not in target[key]]
        elif kind == 'flag':
            flag = _yes_no(field, value)
            target[key] = ('sender_site' if flag else 'installation') if field == 'site-time' else flag
        elif kind == 'count':
            if not value.strip().isdigit():
                raise CliError(f'Write {field} as a whole number of pages.')
            target[key] = int(value.strip())
        elif kind == 'size':
            try:
                target[key] = int(Decimal(value.strip()) * 1_000_000)
            except InvalidOperation:
                raise CliError(f'Write {field} as a number of megabytes, such as 5.') from None
        elif kind == 'days':
            target['days'] = parse_days(value)
        elif kind == 'between':
            target['from'], target['until'] = parse_between(value)
    return block


def parse_money(value, what='amount'):
    try:
        amount = Decimal(value.strip().lstrip('$£€'))
    except (InvalidOperation, AttributeError):
        raise CliError(f"Write the {what} as a number, such as 0.50.") from None
    if amount < 0 or not amount.is_finite():
        raise CliError(f'The {what} must be zero or more.')
    return {'currency': home_currency(), 'amount': format(amount, 'f')}


def _accounts(choices, keys, hint='accounts'):
    """Account keys as typed; a label works too."""
    known = {item['key']: item['label'] for item in (choices or {}).get('accounts') or []}
    known.update({'direct': 'Direct delivery', 'local': 'Delivery inside Faxbot'})
    return [_resolve_key(known, key.strip(), 'account', "See 'faxbot delivery providers accounts list'.")
            for value in keys for key in value.split(',') if key.strip()]


def actions_from(*, use, try_order, cheapest, site_accounts, in_order, automatic, never, require_direct,
                 require_encryption, cap, approval, separate_approver, send_days, send_between, real_call_always,
                 when_busy, pages_per_sheet, alternate, choices, document, subaddress=None):
    then = {}
    if use:
        then['use'] = _accounts(choices, [use])[0]
    if try_order:
        then['try_in_order'] = _accounts(choices, try_order)
    if cheapest:
        then['cheapest_reliable'] = _accounts(choices, cheapest)
    if site_accounts:
        sites = {item['key']: item.get('name') for item in document.get('sites') or []}
        then['site_accounts'] = 'sender' if site_accounts.strip().casefold() == 'sender' else _resolve_key(
            sites, site_accounts, 'site', "See 'faxbot delivery rules sites list'.")
        then['mode'] = 'ordered' if in_order else 'cheapest_reliable'
    if automatic:
        then['automatic'] = True
    if never:
        then['never'] = _accounts(choices, never)
    if require_direct:
        then['require_direct'] = True
    if require_encryption:
        then['require_encryption'] = True
    if cap is not None:
        then['cap_cost'] = parse_money(cap, 'cost cap')
    if approval or separate_approver:
        then['hold_for_approval'] = {'separate_approver': True} if separate_approver else {}
    if send_days or send_between:
        window = {}
        if send_days:
            window['days'] = parse_days(send_days)
        if send_between:
            window['from'], window['until'] = parse_between(send_between)
        then['hold_until'] = window
    if real_call_always:
        then['place_a_real_call'] = True
    if when_busy:
        if when_busy not in ('wait', 'next'):
            raise CliError('Write --when-busy wait or --when-busy next.')
        then['when_busy'] = when_busy
    if pages_per_sheet:
        layouts = {'as-allowed': 'as_receiver_allows', 'one': 'one_per_sheet'}
        if pages_per_sheet not in layouts:
            raise CliError('Write --pages-per-sheet as-allowed or --pages-per-sheet one.')
        then['page_layout'] = layouts[pages_per_sheet]
    if alternate:
        if alternate not in ('use', 'never', 'only'):
            raise CliError('Write --alternate use, never or only.')
        then['alternate_number'] = alternate
    if subaddress is not None:
        clean = subaddress.replace(' ', '')
        if not SUBADDRESS_DIGITS.fullmatch(clean):
            raise CliError('A subaddress is up to 20 digits, such as 2001; it may also use +, # and *.')
        then['subaddress'] = clean
    routes = [key for key in ('use', 'try_in_order', 'cheapest_reliable', 'site_accounts', 'automatic') if key in then]
    if len(routes) > 1:
        raise CliError('Give a rule one way to send: --use, --try, --cheapest, --site-accounts or --automatic.')
    return then, bool(routes)


ROUTE_ACTIONS = ('use', 'try_in_order', 'cheapest_reliable', 'site_accounts', 'automatic')

# The options add and update share.
WHEN = typer.Option(None, '--when', metavar='FIELD=VALUE', help=WHEN_HELP)
UNLESS = typer.Option(None, '--unless', metavar='FIELD=VALUE',
                      help='An exception, FIELD=VALUE, with the same fields as --when: the rule does not apply to a '
                           'fax that matches every exception.')
USE = typer.Option(None, '--use', metavar='ACCOUNT', help='Send by this account only.')
TRY = typer.Option(None, '--try', metavar='ACCOUNT', help='Try these accounts in the order given (repeat it).')
CHEAPEST = typer.Option(None, '--cheapest', metavar='ACCOUNT',
                        help='Send by the cheapest reliable of these accounts (repeat it).')
SITE_ACCOUNTS = typer.Option(None, '--site-accounts', metavar='SITE',
                             help="Send by a site's accounts: sender for the sender's own site, or a site's key.")
IN_ORDER = typer.Option(False, '--in-order', help="With --site-accounts: use the site's accounts in their listed order.")
AUTOMATIC = typer.Option(False, '--automatic', help='Let Faxbot choose the cheapest reliable route, as it does today.')
NEVER = typer.Option(None, '--never', metavar='ACCOUNT', help='Never send by these accounts (repeat it).')
REQUIRE_DIRECT = typer.Option(False, '--require-direct', help='Send only by direct delivery to a verified partner.')
REQUIRE_ENCRYPTION = typer.Option(
    False, '--require-encryption',
    help='Send only encrypted: direct delivery, or SSL Fax where the number has used it before. SSL Fax cannot '
         'confirm who answers at the other end.')
CAP = typer.Option(None, '--cap', metavar='AMOUNT', help='Use only routes that cost at most this much for the fax.')
APPROVAL = typer.Option(False, '--approval', help='Hold the fax until someone who may approve faxes approves it.')
SEPARATE = typer.Option(False, '--separate-approver', help='Hold the fax for approval by someone other than the sender.')
SEND_DAYS = typer.Option(None, '--send-days', metavar='DAYS', help='Send the fax only on these days, such as mon-fri.')
SEND_BETWEEN = typer.Option(None, '--send-between', metavar='HH:MM-HH:MM',
                            help='Send the fax only between these times, such as 18:00-07:00.')
REAL_CALL = typer.Option(False, '--real-call', help='Place a real call, even to your own numbers.')
WHEN_BUSY = typer.Option(None, '--when-busy', metavar='wait|next',
                         help='When every line is busy: wait for a free line, or use the next account.')
PAGES_PER_SHEET = typer.Option(None, '--pages-per-sheet', metavar='as-allowed|one',
                               help='Pages per sheet: as many as the receiving machine allows, or one.')
SUBADDRESS = typer.Option(None, '--subaddress', metavar='DIGITS',
                          help="The department or mailbox to ask for at the recipient's number (a subaddress, up to "
                               '20 digits). Their fax machine must take subaddresses. A setting of a rule that says '
                               'how to send: add --automatic to keep the usual route.')
SUBADDRESS_DIGITS = re.compile(r'[0-9#*+]{1,20}')
ALTERNATE = typer.Option(None, '--alternate', metavar='use|never|only',
                         help="Dial the recipient's approved alternate number: when there is one, never, or only "
                              '(hold the fax when there is none).')
MANDATORY = typer.Option(None, '--mandatory/--not-mandatory',
                         help='Organization rules only: mailbox and workflow rules cannot replace a mandatory routing '
                              'rule, and no one can send a fax anyway around a mandatory limit.')


def _human_lines(result_lines):
    def human(out):
        for line in result_lines:
            out.line(line)
    return human


# -- list, show, add, update, move, enable, disable, remove ------------------------------------------

def rule_rows(document, names, matches, *, numbered=True):
    rows = []
    for section, title in (('limits', 'Limit'), ('routes', 'Routing')):
        for position, rule in enumerate(document.get(section) or [], start=1):
            name = rule.get('name') or ''
            if rule.get('mandatory'):
                name += ' (mandatory)'
            rows.append([title, position if numbered else '-', name, rule.get('id'), 'on' if rule.get('on', True) else 'off',
                         (matches or {}).get(rule.get('id'), '-'), rule_sentence(rule, names)])
    rows.append(['Routing', '-', 'Everything else', '-', 'on', '-', AUTOMATIC_ROW])
    return rows


RULE_COLUMNS = ['Kind', '#', 'Rule', 'Id', 'On', 'Faxes in 30 days', 'What it does']


def _version_line(current):
    scope = current.get('scope') or {}
    title = scope.get('name') or 'Organization'
    active = current.get('active')
    if active:
        line = f"{title} rules, version {active['number']}, published {local_time(active.get('created_at'))}"
        if active.get('actor_name'):
            line += f" by {active['actor_name']}"
        line += '.'
    else:
        line = f'{title} has no published rules yet, so Faxbot chooses the cheapest reliable route, as before.'
    if current.get('draft'):
        line += ' You have changes that are not published yet.'
    return line


@rules.command('list')
def rules_list(scope: str = SCOPE):
    """List the rules, as they read in your draft when you have one."""
    api = state.api()
    current = load(api, scope_param(api, scope))
    document = working_document(current)
    names = names_for(document, current.get('choices'))

    def human(out):
        out.line(_version_line(current))
        organization = current.get('organization')
        if organization:
            out.line('Organization rules, which apply first:')
            out.table(RULE_COLUMNS, rule_rows(organization['document'], names_for(organization['document'],
                                                                                  current.get('choices')), {}))
        out.table(RULE_COLUMNS, rule_rows(document, names, current.get('matches_30_days')))
    state.out().result(current, human)


@rules.command('show')
def rules_show(scope: str = SCOPE,
               revision: int = typer.Option(None, '--revision', min=1, help='An earlier version to show.')):
    """Show the published rules, or an earlier version of them."""
    api = state.api()
    scope_value = scope_param(api, scope)
    current = load(api, scope_value)
    if revision is None:
        detail = current.get('active')
        if not detail:
            state.out().result(current, lambda out: out.line(_version_line(current)))
            return
    else:
        detail = api.get(f'/routing/rules/revisions/{revision}', params={'scope': scope_value})
    names = names_for(detail['document'], current.get('choices'))

    def human(out):
        by = f" by {detail['actor_name']}" if detail.get('actor_name') else ''
        out.line(f"Version {detail['number']}, published {local_time(detail.get('created_at'))}{by}."
                 + (f" Note: {detail['note']}" if detail.get('note') else ''))
        out.table(RULE_COLUMNS, rule_rows(detail['document'], names, current.get('matches_30_days')))
    state.out().result(detail, human)


def _insert(rules_list, rule, before):
    if before is None:
        rules_list.append(rule)
        return
    for index, item in enumerate(rules_list):
        if item.get('id') == before or (item.get('name') or '').casefold() == before.casefold():
            rules_list.insert(index, rule)
            return
    raise CliError(f"No rule of the same kind is called '{before}'. See 'faxbot delivery rules list'.", EXIT_NOT_FOUND)


@rules.command('add')
def rules_add(name: str = typer.Argument(..., help='What the rule is for, in your words, such as "UK numbers go '
                                                   'through Sinch".'),
              scope: str = SCOPE, when: list[str] = WHEN, unless: list[str] = UNLESS, use: str = USE,
              try_order: list[str] = TRY, cheapest: list[str] = CHEAPEST, site_accounts: str = SITE_ACCOUNTS,
              in_order: bool = IN_ORDER, automatic: bool = AUTOMATIC, never: list[str] = NEVER,
              require_direct: bool = REQUIRE_DIRECT, require_encryption: bool = REQUIRE_ENCRYPTION,
              cap: str = CAP, approval: bool = APPROVAL, separate_approver: bool = SEPARATE,
              send_days: str = SEND_DAYS, send_between: str = SEND_BETWEEN, real_call: bool = REAL_CALL,
              when_busy: str = WHEN_BUSY, pages_per_sheet: str = PAGES_PER_SHEET, alternate: str = ALTERNATE,
              subaddress: str = SUBADDRESS, mandatory: bool = MANDATORY,
              before: str = typer.Option(None, '--before', metavar='RULE', help='Put it before this rule.'),
              off: bool = typer.Option(False, '--off', help='Add it switched off.')):
    """Add a rule to the draft. A rule that says how to send is a routing rule; any other rule is a limit."""
    api = state.api()
    scope_value = scope_param(api, scope)
    current = load(api, scope_value)
    document = working_document(current)
    choices = current.get('choices')
    conditions = conditions_from(when, definitions(current, document), choices)
    exceptions = conditions_from(unless, definitions(current, document), choices) if unless else None
    then, routing = actions_from(
        use=use, try_order=try_order, cheapest=cheapest, site_accounts=site_accounts, in_order=in_order,
        automatic=automatic, never=never, require_direct=require_direct, require_encryption=require_encryption, cap=cap,
        approval=approval, separate_approver=separate_approver, send_days=send_days, send_between=send_between,
        real_call_always=real_call, when_busy=when_busy, pages_per_sheet=pages_per_sheet, alternate=alternate,
        choices=choices, document=definitions(current, document), subaddress=subaddress)
    if not then:
        raise CliError('Say what the rule does, for example --use ACCOUNT, --try ACCOUNT, --never ACCOUNT or '
                       '--approval.')
    section = 'routes' if routing else 'limits'
    rule = {'id': new_rule_id(document, section, name), 'name': name, 'on': not off, 'when': conditions, 'then': then}
    if exceptions:
        rule['unless'] = exceptions
    if mandatory:
        rule['mandatory'] = True
    document.setdefault(section, [])
    _insert(document[section], rule, before)
    kind = 'Routing rule' if routing else 'Limit'
    save(api, scope_value, current, document,
         f"{kind} '{name}' added: {rule_sentence(rule, names_for(definitions(current, document), choices))}")


@rules.command('update')
def rules_update(rule: str = typer.Argument(..., metavar='RULE', help="The rule's id or name."),
                 scope: str = SCOPE, name: str = typer.Option(None, '--name', help='A new name.'),
                 when: list[str] = WHEN, unless: list[str] = UNLESS,
                 no_unless: bool = typer.Option(False, '--no-unless', help='Remove the exceptions.'),
                 use: str = USE, try_order: list[str] = TRY, cheapest: list[str] = CHEAPEST,
                 site_accounts: str = SITE_ACCOUNTS, in_order: bool = IN_ORDER, automatic: bool = AUTOMATIC,
                 never: list[str] = NEVER, require_direct: bool = REQUIRE_DIRECT,
                 require_encryption: bool = REQUIRE_ENCRYPTION, cap: str = CAP, approval: bool = APPROVAL,
                 separate_approver: bool = SEPARATE, send_days: str = SEND_DAYS, send_between: str = SEND_BETWEEN,
                 real_call: bool = REAL_CALL, when_busy: str = WHEN_BUSY, pages_per_sheet: str = PAGES_PER_SHEET,
                 alternate: str = ALTERNATE, subaddress: str = SUBADDRESS, mandatory: bool = MANDATORY):
    """Change a rule in the draft. --when replaces all its conditions, and any action option replaces all it does."""
    api = state.api()
    scope_value = scope_param(api, scope)
    current = load(api, scope_value)
    document = working_document(current)
    choices = current.get('choices')
    section, index = _rule_lookup(document, rule)
    found = document[section][index]
    then, routing = actions_from(
        use=use, try_order=try_order, cheapest=cheapest, site_accounts=site_accounts, in_order=in_order,
        automatic=automatic, never=never, require_direct=require_direct, require_encryption=require_encryption, cap=cap,
        approval=approval, separate_approver=separate_approver, send_days=send_days, send_between=send_between,
        real_call_always=real_call, when_busy=when_busy, pages_per_sheet=pages_per_sheet, alternate=alternate,
        choices=choices, document=definitions(current, document), subaddress=subaddress)
    changed = False
    if name:
        found['name'], changed = name, True
    if when:
        found['when'], changed = conditions_from(when, definitions(current, document), choices), True
    if unless:
        found['unless'], changed = conditions_from(unless, definitions(current, document), choices), True
    if no_unless and found.pop('unless', None) is not None:
        changed = True
    if then:
        if routing != (section == 'routes'):
            raise CliError('A routing rule needs a way to send, and a limit cannot have one. Remove the rule and add '
                           'it again to change its kind.')
        found['then'], changed = then, True
    if mandatory is not None:
        found['mandatory'], changed = mandatory, True
    if not changed:
        raise CliError('Nothing to change. Give at least one option.')
    save(api, scope_value, current, document,
         f"Rule '{found['name']}' changed: {rule_sentence(found, names_for(definitions(current, document), choices))}")


@rules.command('move')
def rules_move(rule: str = typer.Argument(..., metavar='RULE', help="The rule's id or name."),
               before: str = typer.Option(None, '--before', metavar='RULE', help='Put it before this rule.'),
               to_end: bool = typer.Option(False, '--to-end', help='Put it last.'), scope: str = SCOPE):
    """Move a rule. Routing rules are read from the top, and the first that matches a fax chooses its route."""
    if bool(before) == bool(to_end):
        raise CliError('Say where it goes: --before RULE or --to-end.')
    api = state.api()
    scope_value = scope_param(api, scope)
    current = load(api, scope_value)
    document = working_document(current)
    section, index = _rule_lookup(document, rule)
    moving = document[section].pop(index)
    if to_end:
        document[section].append(moving)
    else:
        target_section, _ = _rule_lookup(document, before)
        if target_section != section:
            raise CliError('Limits and routing rules are kept apart; move a rule among its own kind.')
        _insert(document[section], moving, before)
    position = document[section].index(moving) + 1
    save(api, scope_value, current, document, f"Rule '{moving['name']}' is now number {position}.")


def _switch(rule, scope, on):
    api = state.api()
    scope_value = scope_param(api, scope)
    current = load(api, scope_value)
    document = working_document(current)
    section, index = _rule_lookup(document, rule)
    document[section][index]['on'] = on
    save(api, scope_value, current, document,
         f"Rule '{document[section][index]['name']}' switched {'on' if on else 'off'}.")


@rules.command('enable')
def rules_enable(rule: str = typer.Argument(..., metavar='RULE', help="The rule's id or name."), scope: str = SCOPE):
    """Switch a rule on in the draft."""
    _switch(rule, scope, True)


@rules.command('disable')
def rules_disable(rule: str = typer.Argument(..., metavar='RULE', help="The rule's id or name."), scope: str = SCOPE):
    """Switch a rule off in the draft, keeping it for later."""
    _switch(rule, scope, False)


@rules.command('remove')
def rules_remove(rule: str = typer.Argument(..., metavar='RULE', help="The rule's id or name."), scope: str = SCOPE):
    """Remove a rule from the draft."""
    api = state.api()
    scope_value = scope_param(api, scope)
    current = load(api, scope_value)
    document = working_document(current)
    section, index = _rule_lookup(document, rule)
    removed = document[section].pop(index)
    save(api, scope_value, current, document, f"Rule '{removed['name']}' removed.")


# -- check, publish, discard, history, diff, restore ---------------------------------------------------

def show_check(out, check):
    errors, warnings = check.get('errors') or [], check.get('warnings') or []
    if not errors and not warnings:
        out.line('No problems found.')
    for issue in errors:
        out.line('Needs fixing: ' + issue['message'])
    for issue in warnings:
        out.line('Worth a look: ' + issue['message'])
    replay = check.get('replay')
    if replay:
        if not replay['checked']:
            line = 'There are no recent faxes to try these rules on yet.'
        elif not replay['changed']:
            line = f"All of your last {replay['checked']} faxes would go the same way."
        else:
            line = f"{replay['changed']} of your last {replay['checked']} faxes would go differently."
        if replay.get('approximate'):
            line += (f" {replay['approximate']} of them were sent before rules existed, so Faxbot used today's groups "
                     'and preferences for them.')
        out.line(line)
        if replay.get('items'):
            out.table(['Fax', 'To', 'Sent', 'Went', 'Would go'],
                      [[item['job_id'], item['to_number'], local_time(item.get('accepted_at')), item['before'],
                        item['after'] + (' (approximate)' if item.get('approximate') else '')]
                       for item in replay['items']])


@rules.command('check')
def rules_check(scope: str = SCOPE,
                replay: int = typer.Option(200, '--replay', min=0, max=1000,
                                           help='How many recent faxes to try under the draft.')):
    """Check the draft for problems, and see which recent faxes it would send differently."""
    api = state.api()
    scope_value = scope_param(api, scope)
    result = api.post('/routing/rules/draft/check', params={'scope': scope_value}, json={'replay': replay})
    state.out().result(result, lambda out: show_check(out, result))
    if result.get('errors'):
        raise typer.Exit(1)


@rules.command('publish')
def rules_publish(note: str = typer.Option(..., '--note', help='What changed and why, for the history.'),
                  scope: str = SCOPE):
    """Put the draft into effect for new faxes. Faxes already waiting keep the rules they were accepted under."""
    api = state.api()
    scope_value = scope_param(api, scope)
    current = load(api, scope_value)
    if not current.get('draft'):
        raise CliError('There is no draft to publish. Change a rule first.')
    active = (current.get('active') or {}).get('number')
    result = api.post('/routing/rules/publish', params={'scope': scope_value},
                      json={'expected_active_revision': active, 'expected_draft_version': current['draft']['version'],
                            'note': note})
    state.out().result(result, lambda out: out.line(
        f"Version {result['number']} is in effect for new faxes. To send waiting faxes by it too, run "
        "'faxbot delivery rules apply-to-waiting'."))


@rules.command('discard')
def rules_discard(scope: str = SCOPE, yes: bool = typer.Option(False, '--yes', help='Do not ask first.')):
    """Throw away the draft. The published rules stay as they are."""
    if not yes and not typer.confirm('Throw away your unpublished changes?', default=False):
        raise typer.Exit(1)
    api = state.api()
    api.delete('/routing/rules/draft', params={'scope': scope_param(api, scope)})
    state.out().result({'discarded': True}, lambda out: out.line('Draft thrown away. The published rules are unchanged.'))


@rules.command('history')
def rules_history(scope: str = SCOPE):
    """List the published versions of the rules, newest first."""
    api = state.api()
    result = api.get('/routing/rules/revisions', params={'scope': scope_param(api, scope)})
    state.out().result(result, lambda out: out.table(
        ['Version', 'Published', 'By', 'Note'],
        [[item['number'], local_time(item.get('created_at')), item.get('actor_name'), item.get('note')]
         for item in result.get('revisions') or []], empty='No published versions yet.'))


SECTION_NAMES = {'limits': 'Limit', 'routes': 'Routing rule', 'lists': 'Recipient group', 'labels': 'Labels',
                 'regions': 'Region', 'sites': 'Site', 'workflows': 'Workflow'}
CHANGE_NAMES = {'added': 'Added', 'removed': 'Removed', 'changed': 'Changed', 'moved': 'Moved'}


def change_text(change, names):
    """What one difference between two versions is, in words."""
    def describe(value):
        if isinstance(value, dict) and 'then' in value:
            return rule_sentence(value, names)
        if isinstance(value, list):
            return ', '.join(str(item) for item in value) or 'none'
        if isinstance(value, dict):
            return ', '.join(str(item) for key in ('numbers', 'prefixes', 'countries', 'mailboxes', 'groups', 'labels')
                             for item in value.get(key) or []) or value.get('name') or '-'
        return '-' if value is None else str(value)
    kind = change.get('change')
    if kind == 'added':
        return describe(change.get('after'))
    if kind == 'removed':
        return describe(change.get('before'))
    if kind == 'moved':
        return 'Moved to another place in the list.'
    return f"Was: {describe(change.get('before'))} Now: {describe(change.get('after'))}"


@rules.command('diff')
def rules_diff(first: int = typer.Argument(..., metavar='A', min=1, help='The earlier version.'),
               second: int = typer.Argument(..., metavar='B', min=1, help='The later version.'),
               scope: str = SCOPE):
    """Show what changed between two versions of the rules."""
    api = state.api()
    scope_value = scope_param(api, scope)
    result = api.get(f'/routing/rules/revisions/{first}/diff/{second}', params={'scope': scope_value})
    current = load(api, scope_value)
    names = names_for(working_document(current), current.get('choices'))
    state.out().result(result, lambda out: out.table(
        ['Change', 'What', 'Name', 'Details'],
        [[CHANGE_NAMES.get(item['change'], item['change']), SECTION_NAMES.get(item['section'], item['section']),
          item.get('name'), change_text(item, names)] for item in result.get('changes') or []],
        empty=f'Versions {first} and {second} are the same.'))


@rules.command('restore')
def rules_restore(number: int = typer.Argument(..., metavar='N', min=1, help='The version to start from.'),
                  scope: str = SCOPE):
    """Make an earlier version the draft, so you can check and publish it again."""
    api = state.api()
    result = api.post(f'/routing/rules/revisions/{number}/restore', params={'scope': scope_param(api, scope)}, json={})
    state.out().result(result, lambda out: out.line(
        f"Version {number} is now your draft. Check it with 'faxbot delivery rules check', then publish it."))


# -- try a fax, apply to waiting faxes, export, import --------------------------------------------------

def show_explain(out, result):
    out.line(result.get('sentence') or '')
    routes = result.get('routes') or []
    if routes:
        out.table(['Account', 'Price', 'From', 'Usable', 'Why'],
                  [[item['label'], money_amount(item['quote']) if item.get('quote') else 'Not priced yet',
                    item.get('origin'), 'yes' if item.get('usable') else 'no', item.get('sentence')] for item in routes])
    for hold in result.get('holds') or []:
        if hold != result.get('sentence'):
            out.line(hold)
    if result.get('dial'):
        out.line(result['dial']['sentence'])
    if result.get('page_layout'):
        out.line(f"Pages per sheet: {layout_words(result['page_layout'])}.")
    if result.get('subaddress'):
        out.line(f"The fax asks for subaddress {result['subaddress']} at the recipient's number.")
    trace = result.get('trace') or []
    if trace:
        out.table(['Rules', 'Rule', 'Result', 'Why'],
                  [[(item.get('scope_name') or SCOPE_NAMES.get(str(item.get('scope')).split(':')[0], item.get('scope')))
                    + (f", version {item['revision']}" if item.get('revision') else ''),
                    item.get('rule_name') or item.get('name')
                    or ("The recipient's preferred route" if item.get('kind') == 'preferred' else '-'),
                    STEP_RESULTS.get(item.get('result'), item.get('result')), step_why(item)] for item in trace])


def layout_words(layout):
    """A page layout as the engine names it, in words."""
    return {'as_receiver_allows': 'as the receiving machine allows', 'one_per_sheet': 'one'}.get(layout, layout)


SCOPE_NAMES = {'organization': 'Organization', 'mailbox': 'Mailbox', 'workflow': 'Workflow'}
STEP_RESULTS = {'matched': 'Matched', 'not_matched': 'Did not match', 'unless': 'Matched, but an exception applies',
                'not_reached': 'Not read: an earlier rule chose', 'not_applied': 'Matched, but did not choose'}
STEP_NOTES = {'mandatory': 'A mandatory rule chose instead.', 'overridden': 'A more specific rule chose instead.',
              'excluded': 'Every account it names was left out by a limit.'}


def step_why(step):
    """The first condition that did not match (as its --when field), or why a match did not choose."""
    parts = []
    if step.get('failed'):
        failed = step['failed']
        parts.append(failed if failed.endswith(('.', '!', '?')) else f'Its condition on {failed} did not match.')
    elif step.get('field'):
        block, _, key = step['field'].rpartition('.')
        names = [name for name, (part, field, _) in CONDITION_FIELDS.items()
                 if field == key and (part or '') == block] or (['days'] if step['field'] == 'time' else [])
        parts.append(f"{names[0] if names else step['field']} did not match.")
    if step.get('note'):
        parts.append(STEP_NOTES.get(step['note'], ''))
    return ' '.join(part for part in parts if part) or '-'


def _local_moment(value):
    text = value.strip().replace(' ', 'T')
    if not re.match(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$', text):
        raise CliError("Write the time as 2026-10-07 18:30, in this installation's time zone.")
    return text


@rules.command('explain')
def rules_explain(to: str = typer.Option(..., '--to', metavar='NUMBER', help='The fax number to try.'),
                  pages: int = typer.Option(1, '--pages', min=1, max=1000, help='Pages in the fax.'),
                  size_mb: float = typer.Option(None, '--size-mb', min=0, help='The file size in megabytes.'),
                  as_sender: str = typer.Option(None, '--as', metavar='PERSON',
                                                help='Who sends it: a person or integration, or me (the default).'),
                  mailbox: str = typer.Option(None, '--mailbox', help='The mailbox it is sent from.'),
                  workflow: str = typer.Option(None, '--workflow', metavar='KEY', help='The workflow it is part of.'),
                  urgent: bool = typer.Option(False, '--urgent', help='The fax is marked urgent.'),
                  real_call: bool = typer.Option(False, '--real-call', help='The sender asks for a real call.'),
                  label: list[str] = typer.Option(None, '--label', help='A label the sender puts on the fax.'),
                  at: str = typer.Option(None, '--at', metavar='TIME',
                                         help="When it is sent, in this installation's time zone, such as 2026-10-07 18:30."),
                  draft: bool = typer.Option(False, '--draft', help='Try the draft instead of the published rules.'),
                  revision: int = typer.Option(None, '--revision', min=1, help='Try an earlier version.'),
                  scope: str = SCOPE):
    """Which route a fax would take, and why. Nothing is sent and nothing is saved."""
    if draft and revision:
        raise CliError('Choose --draft or --revision, not both.')
    api = state.api()
    scope_value = scope_param(api, scope)
    current = load(api, scope_value)
    choices = current.get('choices')
    sender = 'me'
    if as_sender and as_sender.strip().casefold() != 'me':
        sender = _resolve_named(choices, 'people', as_sender, 'person or integration', "See 'faxbot admin access users list'.")
    body = {
        'to': to, 'pages': pages, 'size_bytes': int(size_mb * 1_000_000) if size_mb is not None else None,
        'as': sender,
        'mailbox': _resolve_named(choices, 'mailboxes', mailbox, 'mailbox', "See 'faxbot delivery mailboxes list'.")
        if mailbox else None,
        'workflow': workflow, 'urgent': urgent, 'real_call': real_call, 'labels': list(label or []),
        'at': _local_moment(at) if at else None,
        'source': {'revision': revision} if revision else ('draft' if draft else 'active'), 'scope': scope_value,
    }
    result = api.post('/routing/explain', json=body)
    state.out().result(result, lambda out: show_explain(out, result))


@rules.command('apply-to-waiting')
def rules_apply_to_waiting(yes: bool = typer.Option(False, '--yes', help='Do not ask first.')):
    """Send faxes that are still waiting by the current rules. Faxes already sent or being sent stay as they are."""
    if not yes and not typer.confirm('Decide again, by the current rules, every fax that is still waiting to go?',
                                     default=False):
        raise typer.Exit(1)
    result = state.api().post('/routing/rules/apply-to-waiting', json={})
    state.out().result(result, lambda out: out.line(result.get('sentence') or
                                                    f"{result.get('changed', 0)} waiting faxes will go differently."))


@rules.command('export')
def rules_export(scope: str = SCOPE,
                 file: Path = typer.Option(None, '--file', help='Write to this file instead of the screen.')):
    """Print the draft (or the published rules) as JSON, to edit many rules at once."""
    api = state.api()
    document = working_document(load(api, scope_param(api, scope)))
    text = json.dumps(document, indent=2, ensure_ascii=False) + '\n'
    if file:
        file.write_text(text, encoding='utf-8')
        state.out().line(f'Rules written to {file}.')
    else:
        sys.stdout.write(text)


@rules.command('import')
def rules_import(file: str = typer.Argument(..., metavar='FILE', help="A JSON file from 'export', or - for standard "
                                                                     'input.'),
                 scope: str = SCOPE):
    """Replace the draft with rules from a JSON file. Nothing takes effect until you publish."""
    try:
        text = sys.stdin.read() if file == '-' else Path(file).read_text(encoding='utf-8')
        document = json.loads(text)
    except OSError:
        raise CliError(f'Cannot read {file}.') from None
    except ValueError:
        raise CliError(f'{file} is not JSON written by faxbot delivery rules export.') from None
    if not isinstance(document, dict) or document.get('format') != 1:
        raise CliError(f'{file} is not JSON written by faxbot delivery rules export.')
    api = state.api()
    scope_value = scope_param(api, scope)
    current = load(api, scope_value)
    count = len(document.get('limits') or []) + len(document.get('routes') or [])
    save(api, scope_value, current, document, f'Draft replaced with {count} rules from {file}.')


# -- recipient groups and labels, regions, sites, workflows -----------------------------------------------

def _definitions_command(api, scope, change, message):
    scope_value = scope_param(api, scope)
    current = load(api, scope_value)
    document = working_document(current)
    change(document, current.get('choices'))
    save(api, scope_value, current, document, message)


def _key_option(what):
    return typer.Argument(..., metavar='KEY', help=f'A short key for the {what}, such as uk-clinics.')


@lists_app.command('list')
def lists_list(scope: str = SCOPE):
    """List the recipient groups and the labels senders can use."""
    api = state.api()
    document = working_document(load(api, scope_param(api, scope)))
    lists = recipient_lists(document)

    def human(out):
        out.table(['Key', 'Name', 'Numbers', 'Starting with'],
                  [[key, item.get('name'), ', '.join(item.get('numbers') or []) or '-',
                    ', '.join(item.get('prefixes') or []) or '-'] for key, item in lists.items()],
                  empty='No recipient groups yet.')
        out.line('Labels: ' + (', '.join(document_labels(document)) or 'none yet'))
    state.out().result({'lists': lists, 'labels': document_labels(document)}, human)


@lists_app.command('set')
def lists_set(key: str = _key_option('recipient group'),
              name: str = typer.Option(..., '--name', help='Its name, such as "UK clinics".'),
              number: list[str] = typer.Option(None, '--number', help='A fax number in the group (repeat it).'),
              prefix: list[str] = typer.Option(None, '--prefix',
                                               help='Numbers starting with this, such as +4420 (repeat it).'),
              scope: str = SCOPE):
    """Add a recipient group, or replace one."""
    def change(document, _):
        document['lists'] = {**recipient_lists(document),
                             key: {'name': name, 'numbers': list(number or []), 'prefixes': list(prefix or [])}}
    _definitions_command(state.api(), scope, change, f"Recipient group '{name}' saved.")


@lists_app.command('remove')
def lists_remove(key: str = _key_option('recipient group'), scope: str = SCOPE):
    """Remove a recipient group. Rules that name it must change first, or the check says so."""
    def change(document, _):
        if key not in recipient_lists(document):
            raise CliError(f"No recipient group has the key '{key}'.", EXIT_NOT_FOUND)
        del document['lists'][key]
    _definitions_command(state.api(), scope, change, f"Recipient group '{key}' removed.")


@lists_app.command('labels')
def lists_labels(label: list[str] = typer.Argument(None, metavar='LABEL',
                                                   help='Every label senders may choose; none removes them all.'),
                 scope: str = SCOPE):
    """Set the labels senders can put on a fax, such as legal or clinical."""
    labels = [item.strip() for value in label or [] for item in value.split(',') if item.strip()]

    def change(document, _):
        document['labels'] = labels
    _definitions_command(state.api(), scope, change, 'Labels saved: ' + (', '.join(labels) or 'none') + '.')


@regions_app.command('list')
def regions_list(scope: str = SCOPE):
    """List the regions."""
    api = state.api()
    regions = working_document(load(api, scope_param(api, scope))).get('regions') or {}
    state.out().result(regions, lambda out: out.table(
        ['Key', 'Name', 'Countries', 'Starting with'],
        [[key, item.get('name'), ', '.join(country_name(code) for code in item.get('countries') or []) or '-',
          ', '.join(item.get('prefixes') or []) or '-'] for key, item in regions.items()], empty='No regions yet.'))


@regions_app.command('set')
def regions_set(key: str = _key_option('region'),
                name: str = typer.Option(..., '--name', help='Its name, such as "Northern England".'),
                country: list[str] = typer.Option(None, '--country', help='A country code, such as GB (repeat it).'),
                prefix: list[str] = typer.Option(None, '--prefix', help='Numbers starting with this (repeat it).'),
                scope: str = SCOPE):
    """Add a region, or replace one."""
    def change(document, _):
        document.setdefault('regions', {})[key] = {
            'name': name, 'countries': [code.upper() for code in country or []], 'prefixes': list(prefix or [])}
    _definitions_command(state.api(), scope, change, f"Region '{name}' saved.")


@regions_app.command('remove')
def regions_remove(key: str = _key_option('region'), scope: str = SCOPE):
    """Remove a region."""
    def change(document, _):
        if key not in (document.get('regions') or {}):
            raise CliError(f"No region has the key '{key}'.", EXIT_NOT_FOUND)
        del document['regions'][key]
    _definitions_command(state.api(), scope, change, f"Region '{key}' removed.")


@sites_app.command('list')
def sites_list(scope: str = SCOPE):
    """List the sites, with their mailboxes, groups and accounts."""
    api = state.api()
    current = load(api, scope_param(api, scope))
    document = working_document(current)
    names = names_for(document, current.get('choices'))
    accounts = (current.get('choices') or {}).get('accounts') or []
    sites = document.get('sites') or []
    state.out().result(sites, lambda out: out.table(
        ['Key', 'Name', 'Country', 'Time zone', 'Mailboxes', 'Groups', 'Accounts'],
        [[item['key'], item.get('name'), country_name(item['country']) if item.get('country') else '-',
          item.get('time_zone') or '-', ', '.join(names.mailbox(value) for value in item.get('mailboxes') or []) or '-',
          ', '.join(names.group(value) for value in item.get('groups') or []) or '-',
          ', '.join(site_accounts(item, accounts)) or '-'] for item in sites], empty='No sites yet.'))


def site_accounts(site, accounts):
    """A site's accounts: those it lists, then those whose own site names it."""
    labels = {account['key']: account['label'] for account in accounts}
    listed = list(site.get('accounts') or [])
    own = [account['key'] for account in accounts if account.get('site') == site['key'] and account['key'] not in listed]
    return [labels.get(key, key) for key in listed + own]


@sites_app.command('set')
def sites_set(key: str = _key_option('site'),
              name: str = typer.Option(..., '--name', help='Its name, such as "Leeds office".'),
              country: str = typer.Option(None, '--country', help='Its country code, such as GB.'),
              time_zone: str = typer.Option(None, '--time-zone', help='Its time zone, such as Europe/London.'),
              mailbox: list[str] = typer.Option(None, '--mailbox', help='A mailbox that sends from it (repeat it).'),
              group: list[str] = typer.Option(None, '--group', help='A group that sends from it (repeat it).'),
              account: list[str] = typer.Option(None, '--account', metavar='KEY',
                                                help='An account its calls start from (repeat it).'),
              scope: str = SCOPE):
    """Add a site, or replace one. Give an account its site with 'faxbot delivery providers accounts update KEY --site'."""
    def change(document, choices):
        site = {'key': key, 'name': name, 'country': country.upper() if country else None, 'time_zone': time_zone,
                'mailboxes': [_resolve_named(choices, 'mailboxes', value, 'mailbox', "See 'faxbot delivery mailboxes "
                                                                                    "list'.") for value in mailbox or []],
                'groups': [_resolve_named(choices, 'groups', value, 'group', "See 'faxbot admin access groups list'.")
                           for value in group or []],
                'accounts': _accounts(choices, account or [])}
        sites = document.get('sites') or []
        if any(item.get('key') == key for item in sites):
            document['sites'] = [site if item.get('key') == key else item for item in sites]
        else:
            document['sites'] = [*sites, site]
    _definitions_command(state.api(), scope, change, f"Site '{name}' saved.")


@sites_app.command('remove')
def sites_remove(key: str = _key_option('site'), scope: str = SCOPE):
    """Remove a site."""
    def change(document, _):
        sites = document.get('sites') or []
        if not any(item.get('key') == key for item in sites):
            raise CliError(f"No site has the key '{key}'.", EXIT_NOT_FOUND)
        document['sites'] = [item for item in sites if item.get('key') != key]
    _definitions_command(state.api(), scope, change, f"Site '{key}' removed.")


@workflows_app.command('list')
def workflows_list(scope: str = SCOPE):
    """List the workflows."""
    api = state.api()
    current = load(api, scope_param(api, scope))
    document = working_document(current)
    names = names_for(document, current.get('choices'))
    workflows = document.get('workflows') or []
    state.out().result(workflows, lambda out: out.table(
        ['Key', 'Name', 'Mailboxes', 'Labels'],
        [[item['key'], item.get('name'), ', '.join(names.mailbox(value) for value in item.get('mailboxes') or []) or '-',
          ', '.join(item.get('labels') or []) or '-'] for item in workflows], empty='No workflows yet.'))


@workflows_app.command('set')
def workflows_set(key: str = _key_option('workflow'),
                  name: str = typer.Option(..., '--name', help='Its name, such as "Referrals".'),
                  mailbox: list[str] = typer.Option(None, '--mailbox',
                                                    help='A mailbox whose faxes are part of it (repeat it).'),
                  label: list[str] = typer.Option(None, '--label', help='A label that puts a fax in it (repeat it).'),
                  scope: str = SCOPE):
    """Add a workflow, or replace one. A workflow can then have its own rules: --scope workflow:KEY."""
    def change(document, choices):
        workflow = {'key': key, 'name': name, 'labels': list(label or []),
                    'mailboxes': [_resolve_named(choices, 'mailboxes', value, 'mailbox',
                                                 "See 'faxbot delivery mailboxes list'.") for value in mailbox or []]}
        workflows = document.get('workflows') or []
        if any(item.get('key') == key for item in workflows):
            document['workflows'] = [workflow if item.get('key') == key else item for item in workflows]
        else:
            document['workflows'] = [*workflows, workflow]
    _definitions_command(state.api(), scope, change, f"Workflow '{name}' saved.")


@workflows_app.command('remove')
def workflows_remove(key: str = _key_option('workflow'), scope: str = SCOPE):
    """Remove a workflow."""
    def change(document, _):
        workflows = document.get('workflows') or []
        if not any(item.get('key') == key for item in workflows):
            raise CliError(f"No workflow has the key '{key}'.", EXIT_NOT_FOUND)
        document['workflows'] = [item for item in workflows if item.get('key') != key]
    _definitions_command(state.api(), scope, change, f"Workflow '{key}' removed.")


# -- held faxes, approvals and why a fax took its route (faxbot faxes sent) -------------------------------------

def held_rows(holds):
    return [[item['job_id'], item['to_number'], item.get('pages') or '-', item.get('sender_name') or '-',
             local_time(item.get('requested_at')), item['reason']] for item in holds]


HELD_COLUMNS = ['Fax', 'To', 'Pages', 'Sent by', 'Waiting since', 'Why it is waiting']


def held_list():
    """List faxes held by your rules: waiting for approval, for a time window, or for a route the rules allow."""
    result = state.api().get('/routing/holds', params={'state': 'open'})
    state.out().result(result, lambda out: out.table(HELD_COLUMNS, held_rows(result.get('holds') or []),
                                                     empty='No faxes are waiting for you.'))


def _hold_for(api, fax_id):
    holds = api.get('/routing/holds', params={'state': 'open'}).get('holds') or []
    found = [item for item in holds if item['job_id'] == fax_id or item['id'] == fax_id]
    if not found:
        raise CliError(f"Fax {fax_id} is not waiting for you. See 'faxbot faxes sent list --held'.", EXIT_NOT_FOUND)
    return found[0]


def alternate_sentence(dialed, alternate):
    """"Dialed the recipient's approved alternate number ... instead of ..., approved by ... on ...", and who pays."""
    sentence = f"Dialed the recipient's approved alternate number {dialed} instead of {alternate['original_number']}"
    if alternate.get('approved_by'):
        sentence += f", approved by {alternate['approved_by']}"
    if alternate.get('approved_on'):
        sentence += f" on {alternate['approved_on']}"
    if alternate.get('note'):
        sentence += f" (‘{alternate['note']}’)"
    sentence += '.'
    if alternate.get('recipient_pays'):
        sentence += ' The recipient pays for calls to this number.'
    return sentence


def route_command(fax_id: str = typer.Argument(..., metavar='FAX_ID', help='The fax id, from faxbot faxes sent list.')):
    """Why a sent fax took its route: the rule that chose it, and what happened on each attempt."""
    result = state.api().get('/routing/faxes/' + segment(fax_id) + '/route')

    def human(out):
        out.line(result.get('sentence') or '')
        attempts = result.get('attempts') or []
        if attempts:
            out.table(['Attempt', 'Account', 'Number dialed', 'Pages per sheet', 'Estimate', 'What happened'],
                      [[item['number'], item['account_label'], item.get('dialed_number') or '-',
                        layout_words(item['page_layout']) if item.get('page_layout') else '-',
                        item.get('estimate_text') or (f"{money_amount(item['estimate'])} estimate"
                                                      if item.get('estimate') else '-'), item['sentence']]
                       for item in attempts])
        for item in attempts:
            if item.get('alternate') and item.get('dialed_number'):
                out.line(f"Attempt {item['number']}: " + alternate_sentence(item['dialed_number'], item['alternate']))
        if result.get('hold'):
            out.line(result['hold']['reason'])
    state.out().result(result, human)


def _anyway_lines(hold):
    lines = [f"  {option['account']}: {option['label']}, {option['reason']}" for option in hold.get('options') or []]
    return '\n'.join(lines + [f'  {sentence}' for sentence in hold.get('not_offered') or []])


def approve_command(fax_id: str = typer.Argument(..., metavar='FAX_ID', help='The held fax, from faxbot faxes sent list '
                                                                              '--held.'),
                    account: str = typer.Option(None, '--account', metavar='KEY',
                                                help='For a fax no route your rules allow: send it by this account '
                                                     'anyway. Faxbot offers only accounts left out by a cost cap or by '
                                                     'being down or busy.')):
    """Approve a fax your rules held for approval, or send a fax with no allowed route by an account anyway."""
    api = state.api()
    hold = _hold_for(api, fax_id)
    if hold['kind'] == 'window':
        raise CliError(f"Fax {fax_id} is waiting for its time window, not for approval: {hold['reason']}")
    if not hold.get('can_decide', True):
        raise CliError('Someone other than the sender must approve this fax.')
    body = {'version': hold['version']}
    label = None
    if hold['kind'] == 'no_route':
        options = {option['account']: option['label'] for option in hold.get('options') or []}
        if not options:
            raise CliError(f"{hold['reason']} No account can take it now. Run 'faxbot faxes sent check-again {fax_id}' later, or "
                           'change your rules.')
        if account not in options:
            raise CliError(f"{hold['reason']} Choose an account with --account:\n{_anyway_lines(hold)}")
        body['account'], label = account, options[account]
    elif account:
        raise CliError('--account is only for a fax with no route your rules allow.')
    result = api.post('/routing/holds/' + segment(hold['id']) + '/approve', json=body)
    fallback = (f"Approved. The fax to {hold['to_number']} goes by {label}." if label
                else f"Approved. The fax to {hold['to_number']} is no longer held.")
    state.out().result(result, lambda out: out.line(result.get('sentence') or fallback))


def check_again_command(fax_id: str = typer.Argument(..., metavar='FAX_ID', help='The held fax, from faxbot faxes sent list '
                                                                                  '--held.')):
    """Look again for a route your rules allow for a held fax, for when an account may be back."""
    api = state.api()
    hold = _hold_for(api, fax_id)
    result = api.post('/routing/holds/' + segment(hold['id']) + '/check-again', json={'version': hold['version']})
    state.out().result(result, lambda out: out.line(
        result.get('sentence') or f"Faxbot checked the routes for the fax to {hold['to_number']} again."))


def refuse_command(fax_id: str = typer.Argument(..., metavar='FAX_ID', help='The held fax, from faxbot faxes sent list '
                                                                             '--held.'),
                   reason: str = typer.Option(..., '--reason', help='Why, for the sender and the history, such as '
                                                                   '"wrong recipient".')):
    """Refuse a held fax. Nothing is sent, and the fax is marked failed with your reason."""
    api = state.api()
    hold = _hold_for(api, fax_id)
    if not hold.get('can_decide', True):
        raise CliError('Someone other than the sender must decide on this fax.')
    result = api.post('/routing/holds/' + segment(hold['id']) + '/refuse',
                      json={'version': hold['version'], 'reason': reason})
    state.out().result(result, lambda out: out.line(
        result.get('sentence') or f"Refused. Nothing was sent to {hold['to_number']}."))


# -- receiving rules (faxbot delivery numbers add, update and explain) ---------------------------------------------

# The options `faxbot delivery numbers add` and `update` gain; access.py passes them through receiving_options().
NUMBER_ACCOUNT = typer.Option(None, '--account', metavar='KEY', help='Only faxes received on this account.')
NUMBER_FROM = typer.Option(None, '--from', metavar='NUMBER',
                           help='Only faxes from this number; end it with * for every number that starts with it '
                                '(repeat it).')
NUMBER_DAYS = typer.Option(None, '--days', metavar='DAYS', help='Only faxes received on these days, such as mon-fri.')
NUMBER_BETWEEN = typer.Option(None, '--between', metavar='HH:MM-HH:MM',
                              help="Only faxes received between these times, in this installation's time zone.")
NUMBER_EMAIL = typer.Option(None, '--email', metavar='CONNECTOR', help='Email these faxes through this connector.')
NUMBER_NO_EMAIL = typer.Option(False, '--no-email', help='Send no email for these faxes.')
NUMBER_URGENT = typer.Option(None, '--urgent/--not-urgent', help='Mark these faxes urgent.')
NUMBER_KEEP = typer.Option(None, '--keep-days', min=1, metavar='DAYS',
                           help='Remove these faxes from Faxbot after this many days. ' + KEEP_DAYS_NOTE)
NUMBER_POSITION = typer.Option(None, '--position', min=1, metavar='N',
                               help='Its place among your number rules; the first that matches a fax places it.')
NUMBER_ANY = typer.Option(None, '--any-number/--this-number-only', help='Use the rule for faxes to any of your numbers.')
NUMBER_SUBADDRESS = typer.Option(None, '--subaddress', metavar='DIGITS',
                                 help="Only faxes whose sender's machine gives this subaddress, such as a "
                                      "department's 2001. It chooses the mailbox and never gives anyone access.")
NUMBER_SITE = typer.Option(None, '--site', metavar='SITE', help='Only faxes received on an account of this site.')
NUMBER_FORWARDED = typer.Option(None, '--forwarded-from', metavar='NUMBER',
                                help='Only calls forwarded to this number from NUMBER, when the forwarding is '
                                     'verified (add --forwarded-unsigned to take one that is not). "" removes the '
                                     'condition.')
NUMBER_FORWARDED_UNSIGNED = typer.Option(None, '--forwarded-unsigned/--forwarded-signed-only',
                                         help='Also take a forwarding that is not verified: signed with a '
                                              'certificate from no certificate authority you trust, not checked, '
                                              'or unsigned (never one whose signature failed).')


def receiving_options(api, *, account=None, from_numbers=None, days=None, between=None, email=None, no_email=False,
                      urgent=None, keep_days=None, position=None, any_number=None, subaddress=None, site=None,
                      forwarded_from=None, forwarded_unsigned=None):
    """The receiving-rule fields of an /access/inbound-rules body, from the options given."""
    if email and no_email:
        raise CliError('Choose --email CONNECTOR or --no-email, not both.')
    body = {}
    if account:
        body['account_key'] = account
    if subaddress is not None:
        body['subaddress'] = subaddress.strip() or None
    if forwarded_from is not None:
        body['diverted_from'] = forwarded_from.strip() or None
    if forwarded_unsigned is not None:
        body['diversion_unsigned'] = forwarded_unsigned
    if site is not None:
        body['site_key'] = site.strip() or None
    if from_numbers:
        body['from_numbers'] = list(from_numbers)
    if days:
        body['days'] = parse_days(days)
    if between:
        start, end = parse_between(between)
        body['start_minute'] = int(start[:2]) * 60 + int(start[3:])
        body['end_minute'] = int(end[:2]) * 60 + int(end[3:])
    if email:
        from .delivery import _connector
        body['email_connector_id'], body['email_off'] = _connector(api, email)['id'], False
    if no_email:
        body['email_connector_id'], body['email_off'] = None, True
    if urgent is not None:
        body['urgent'] = urgent
    if keep_days is not None:
        body['keep_days'] = keep_days
    if position is not None:
        body['position'] = position
    if any_number is not None:
        body['any_number'] = any_number
    return body


def numbers_explain(to: str = typer.Option(..., '--to', metavar='NUMBER', help='Your number the fax is sent to.'),
                    sender: str = typer.Option(None, '--from', metavar='NUMBER', help='The number it comes from.'),
                    account: str = typer.Option(None, '--account', metavar='KEY', help='The account it arrives on.'),
                    at: str = typer.Option(None, '--at', metavar='TIME',
                                           help="When it arrives, in this installation's time zone, such as "
                                                '2026-10-07 18:30.'),
                    subaddress: str = typer.Option(None, '--subaddress', metavar='DIGITS',
                                                   help="The subaddress the sender's machine gives, if any.")):
    """Which mailbox, email and urgency a received fax would get, and why. Nothing is saved."""
    body = {'to_number': to, 'from_number': sender, 'account_key': account, 'at': _local_moment(at) if at else None}
    if subaddress:
        body['subaddress'] = subaddress
    result = state.api().post('/access/inbound-rules/explain', json=body)
    state.out().result(result, lambda out: out.line(result.get('sentence') or ''))
