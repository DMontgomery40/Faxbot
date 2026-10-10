"""What the California court profile rests on: the rules it applies, court holidays and court days, each with its
source and the date it was read. Nothing here is a legal opinion; it is the rules' own words, applied to dates.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

READ_ON = '2026-10-10'
ZONE = 'America/Los_Angeles'  # California courts' clock: "after 5 p.m." is 5 p.m. there

RULE_SOURCES = (
    {'rule': 'rule 2.301', 'url': 'https://www.courts.ca.gov/cms/rules/index/two/rule2_301', 'read_on': READ_ON,
     'says': 'A transmission record is the document printed by the sending fax machine, stating the receiving '
             'number, the pages sent, the time and date, and any errors.'},
    {'rule': 'rule 2.304', 'url': 'https://www.courts.ca.gov/cms/rules/index/two/rule2_304', 'read_on': READ_ON,
     'says': 'Direct fax filing only to a court whose local rule provides for it, with the MC-005 cover sheet, '
             '"By fax" immediately below each document\'s title, and the transmission record as proof.'},
    {'rule': 'rule 2.305', 'url': 'https://www.courts.ca.gov/cms/rules/index/two/rule2_305', 'read_on': READ_ON,
     'says': 'A signature produced by fax is deemed an original; the sender keeps the signed original.'},
    {'rule': 'rule 2.306', 'url': 'https://courts.ca.gov/cms/rules/index/two/rule2_306', 'read_on': READ_ON,
     'says': 'Service by fax only by written agreement; after 5 p.m. it is deemed served the next court day; time '
             'to act is extended by two court days, with three exceptions.'},
    {'rule': 'form MC-005', 'url': 'https://www.courts.ca.gov/documents/mc005.pdf', 'read_on': READ_ON,
     'says': 'Facsimile Transmission Cover Sheet (Fax Filing), Rev. January 1, 2007, adopted for mandatory use.'},
)
HOLIDAYS_SOURCE = {'url': 'https://www.courts.ca.gov/holidays.htm', 'read_on': READ_ON,
                   'says': 'The California courts will be closed in observance of the following holidays.'}

# The California courts' published holiday schedule, exactly as listed (read 2026-10-10). A year not listed here
# is not published yet: Faxbot then says it cannot count court days, rather than guessing.
HOLIDAYS = {
    2026: (
        (date(2026, 1, 1), "New Year's Day"), (date(2026, 1, 19), 'Dr. Martin Luther King, Jr. Day'),
        (date(2026, 2, 12), "Lincoln's Birthday"), (date(2026, 2, 16), "President's Day"),
        (date(2026, 3, 31), 'Farmworkers Day'), (date(2026, 5, 25), 'Memorial Day'),
        (date(2026, 6, 19), 'Juneteenth'), (date(2026, 7, 3), 'Independence Day'),
        (date(2026, 9, 7), 'Labor Day'), (date(2026, 9, 25), 'Native American Day'),
        (date(2026, 11, 11), 'Veterans Day'), (date(2026, 11, 26), 'Thanksgiving'),
        (date(2026, 11, 27), 'Day After Thanksgiving'), (date(2026, 12, 25), 'Christmas Day'),
    ),
    2027: (
        (date(2027, 1, 1), "New Year's Day"), (date(2027, 1, 18), 'Martin Luther King, Jr., Day'),
        (date(2027, 2, 12), "Lincoln's Birthday"), (date(2027, 2, 15), "Washington's Birthday"),
        (date(2027, 4, 9), 'Farmworkers Day (formerly Cesar Chavez Day)'), (date(2027, 5, 31), 'Memorial Day'),
        (date(2027, 6, 18), 'Juneteenth'), (date(2027, 7, 5), 'Independence Day'),
        (date(2027, 9, 6), 'Labor Day'), (date(2027, 9, 24), 'Native American Day'),
        (date(2027, 11, 11), 'Veterans Day'), (date(2027, 11, 25), 'Thanksgiving Day'),
        (date(2027, 11, 26), 'Day after Thanksgiving'), (date(2027, 12, 24), 'Christmas Day'),
    ),
}

# Deadlines that the two-court-day extension never reaches (rule 2.306(e)).
NOT_EXTENDED = ('a notice of intent to move for a new trial', 'a notice of intent to move to vacate a judgment '
                'under Code of Civil Procedure section 663', 'a notice of appeal')


class CourtCalendarUnknown(ValueError):
    """The court holidays of a year are not published yet, so court days cannot be counted."""

    def __init__(self, year):
        self.year = year
        super().__init__(f"The California courts haven't published their {year} holidays yet, so Faxbot can't count "
                         'court days in that year. Check the court calendar.')


def holiday(day: date):
    """The court holiday's name on ``day``, or None; raises ``CourtCalendarUnknown`` for an unpublished year."""
    if day.year not in HOLIDAYS:
        raise CourtCalendarUnknown(day.year)
    return next((name for listed, name in HOLIDAYS[day.year] if listed == day), None)


def is_court_day(day: date) -> bool:
    """A weekday that is not a published court holiday."""
    return day.weekday() < 5 and holiday(day) is None


def next_court_day(day: date) -> date:
    """The first court day after ``day``."""
    following = day + timedelta(days=1)
    while not is_court_day(following):
        following += timedelta(days=1)
    return following


def add_court_days(day: date, count: int) -> date:
    """``day`` moved forward by ``count`` court days (the extension of rule 2.306(d))."""
    for _ in range(count):
        day = next_court_day(day)
    return day


def _zone():
    from zoneinfo import ZoneInfo
    return ZoneInfo(ZONE)


def local(moment: datetime) -> datetime:
    """A stored UTC moment (naive or aware) on the California courts' clock."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(_zone())


@dataclass(frozen=True)
class Service:
    """When service by fax is complete under rule 2.306(g), with one sentence for each fact."""
    transmitted: datetime        # on the courts' clock
    served_on: date
    moved: bool                  # completed after 5 p.m.: deemed to occur on the next court day
    sentences: tuple[str, ...]


def _day(value: date) -> str:
    return f'{value:%A} {value.day} {value:%B %Y}'


def service(completed_at: datetime) -> Service:
    """When service by fax completed at ``completed_at`` (UTC) is deemed to occur, under rule 2.306(g)."""
    transmitted = local(completed_at)
    day = transmitted.date()
    after_five = transmitted.timetz().replace(tzinfo=None) > time(17, 0)
    clock = f"{transmitted:%I:%M %p}".lstrip('0').replace('AM', 'a.m.').replace('PM', 'p.m.')
    if after_five:
        served = next_court_day(day)
        first = (f'The whole document was sent at {clock} California time on {_day(day)}, after 5 p.m., so service '
                 f'is deemed to occur on the next court day, {_day(served)} (rule 2.306(g)).')
    else:
        served = day
        first = f'The whole document was sent at {clock} California time on {_day(day)} (rule 2.306(g)).'
    sentences = [first]
    if not after_five and not is_court_day(day):
        name = holiday(day)
        sentences.append(f'{_day(day)} is not a court day{f" ({name})" if name else ""}; rule 2.306(g) moves only '
                         'service completed after 5 p.m., so check how your deadline counts it.')
    sentences.append('Any period to act or respond that runs from this service is extended by two court days (rule '
                     f'2.306(d)), except for {", ".join(NOT_EXTENDED[:-1])} and {NOT_EXTENDED[-1]} (rule 2.306(e)).')
    return Service(transmitted, served, after_five, tuple(sentences))


def extended(deadline: date) -> tuple[date, str]:
    """A deadline you worked out from the service date, extended by two court days (rule 2.306(d))."""
    moved = add_court_days(deadline, 2)
    return moved, (f'A deadline of {_day(deadline)} is extended by two court days to {_day(moved)} (rule 2.306(d)), '
                   'unless it is one of the three the rule leaves out.')


# Which California superior courts take direct fax filing (rule 2.304 needs a local rule), as each court's own
# website or current local rules say, read 2026-10-10. Courts are leaving fax: eight have repealed it since 2011,
# five of them since 2019. A court not listed here was not established either way; its own local rule decides.
@dataclass(frozen=True)
class Court:
    county: str
    status: str                 # 'accepts', 'stopped' or 'does_not_accept'
    note: str
    fax: str | None = None
    cutoff: time | None = None  # faxes received after this time are filed the next court day, by the local rule
    source: str = ''
    source_date: str | None = None


ACCEPTS, STOPPED, REFUSES = 'accepts', 'stopped', 'does_not_accept'
COURTS = (
    Court('Plumas', ACCEPTS, 'Civil, probate, family law, juvenile dependency and the Appellate Division; at most 15 '
          'pages. The court asks for a payment card on MC-005, which Faxbot never takes: arrange the fee with the '
          'court.', '(530) 283-6415', time(16, 0), 'https://plumas.courts.ca.gov/forms-filing/e-filing-fax-filing',
          'local rule 1.10, amended 1 July 2026'),
    Court('Lassen', ACCEPTS, 'Civil, probate, family law and the Appellate Division at (530) 251-2115; juvenile '
          'dependency at (530) 251-2126; $1.00 a page; no criminal matters.', '(530) 251-2115', time(16, 0),
          'https://www.lassen.courts.ca.gov/forms-filing/fax-filing'),
    Court('Alpine', ACCEPTS, 'At most 20 pages including attachments; $1.00 a page, paid by check or on the court '
          'website within 24 hours, never by card on the cover sheet.', '(530) 694-2119', time(16, 30),
          'https://www.alpine.courts.ca.gov/system/files/local-rules/local-rules-2024.pdf',
          'local rule 3.8.1, amended 1 July 2024'),
    Court('Mariposa', ACCEPTS, 'Pleadings under rules 2.300 to 2.306, with MC-005 as the first page.',
          '(209) 742-6860', None, 'https://www.mariposa.courts.ca.gov/system/files/localrules.pdf',
          'local rule 2.5, 1 January 2019'),
    Court('Modoc', ACCEPTS, 'Civil, probate and family law, "BY FAX" below the title; faxes after 5:00 p.m. or on a '
          'court holiday are filed the next court day.', '(530) 233-6500', time(17, 0),
          'https://www.modoc.courts.ca.gov/system/files/local-rules/modocrules-07-01-2024.pdf',
          'local rule 2.12, rules of 1 July 2024'),
    Court('Yuba', ACCEPTS, 'Direct fax filing and fax filing agencies; $20 a filing plus $0.50 a page, due by the '
          'next business day.', '(530) 740-1801', None,
          'https://www.yuba.courts.ca.gov/system/files/local-rules/72026-draft-yuba-county-local-rules-final-signed.pdf',
          'local rule 2.5, amended 1 January 2013'),
    Court('Santa Barbara', ACCEPTS, 'Any party, except documents that must be filed electronically (attorneys in civil '
          'and probate, among others); each branch has its own fax number.', '(805) 882-4519', None,
          'https://www.santabarbara.courts.ca.gov/system/files/local-rules/courtrules0125.pdf',
          'local rule 1008, amended 1 July 2018'),
    Court('Del Norte', ACCEPTS, 'Civil, probate and family law; a copy of the fee check goes with the fax. The only '
          'posted local rules date from 2012.', '(707) 465-4005', None,
          'https://www.delnorte.courts.ca.gov/system/files/del-norte-final-rules-nov-2011.pdf',
          'local rule 2.3, effective 1 January 2012'),
    Court('Amador', ACCEPTS, 'Family law, civil and probate, at most 50 pages a document, each sent separately; its '
          'page cites the electronic filing rules, not rule 2.304, and gives no fax number.', None, time(17, 0),
          'https://amador.courts.ca.gov/forms-local-rules/fax-filing'),
    Court('Santa Clara', ACCEPTS, 'Only quarantine and detention petitions in probate (probate local rule 18).',
          '(408) 882-2694', None, 'https://santaclara.courts.ca.gov/system/files/rules/complete.pdf'),
    Court('Siskiyou', ACCEPTS, 'Only later documents in criminal matters; $1.00 a page including the cover.',
          '(530) 572-4015', None,
          'https://www.siskiyou.courts.ca.gov/system/files/local-rules/2026-final-local-rules-court.pdf',
          '2026 local rules, rule 2.24'),
    Court('Orange', STOPPED, 'Local rule 380 was repealed on 1 July 2011.', None, None,
          'https://www.occourts.org/system/files?file=rule380.pdf', 'repealed 1 July 2011'),
    Court('San Luis Obispo', STOPPED, 'Chapter 29, fax filing, was repealed on 1 July 2015.', None, None,
          'https://www.slo.courts.ca.gov/system/files/local-rules/local-rules-be-effective-july-1-2026.pdf',
          'repealed 1 July 2015'),
    Court('Ventura', STOPPED, 'Rule 4.04, facsimile filing, was repealed on 15 March 2018.', None, None,
          'https://ventura.courts.ca.gov/system/files/general/ventura_county_rules_of_court.pdf',
          'repealed 15 March 2018'),
    Court('Mendocino', STOPPED, 'Local rule 1.21, facsimile filing, was repealed on 1 July 2019.', None, None,
          'https://www.mendocino.courts.ca.gov/system/files/local-rules/localrules.pdf', 'repealed 1 July 2019'),
    Court('Mono', STOPPED, 'No longer accepts fax filings from 15 March 2025; it moved to electronic filing.', None,
          None, 'https://www.mono.courts.ca.gov/fax-filing', '15 March 2025'),
    Court('Alameda', STOPPED, 'Rule 1.8 was repealed on 15 November 2024.', None, None,
          'https://www.alameda.courts.ca.gov/system/files/local-rules/01-title-1-20260701.pdf',
          'repealed 15 November 2024'),
    Court('San Bernardino', STOPPED, 'Rescinded local rule 320 on 1 January 2025, ending public fax filing, including '
          'cover sheets with card details.', None, None,
          'https://sanbernardino.courts.ca.gov/news/modified-guidelines-electronic-filing', '2 January 2025'),
    Court('San Benito', STOPPED, 'Rule 10.3, facsimile filings, was repealed on 1 January 2025.', None, None,
          'https://www.sanbenito.courts.ca.gov/system/files/local-rules/'
          '2026-local-rules-superior-court-san-benito-county-redline.pdf', 'repealed 1 January 2025'),
    Court('San Francisco', REFUSES, 'Does not accept direct fax filing under rule 2.304; faxes only through its '
          'electronic filing vendor.', None, None,
          'https://sf.courts.ca.gov/system/files/local-rules/final-proposed-changes-lrsf-effective-july-1-2026-'
          'submitted.pdf', 'local rule 2.7(C), 1 July 2026'),
    Court('Calaveras', REFUSES, "A fax filing agency only, or direct fax with the court's leave.", None, None,
          'https://calaveras.courts.ca.gov/forms-filing/fax-filing'),
    Court('Tuolumne', REFUSES, 'A fax filing agency only.', None, None,
          'https://www.tuolumne.courts.ca.gov/forms-filing/fax-filing'),
    Court('Colusa', REFUSES, 'No direct line for fax filings; a fax filing agency under rule 2.303.', None, None,
          'https://www.colusa.courts.ca.gov/divisions/civil'),
    Court('Sutter', REFUSES, 'No direct line for fax filings; a fax filing agency under rule 2.303.', None, None,
          'https://www.sutter.courts.ca.gov/divisions/civil/civil-faqs'),
    Court('Humboldt', REFUSES, 'Does not accept direct fax filing.', None, None,
          'https://www.humboldt.courts.ca.gov/system/files/local-rules/05-16-2025-local-rules-effective-july-1-2025.pdf',
          'local rule 1.8, 1 July 2025'),
    Court('Kern', REFUSES, 'A fax filing agency only.', None, None,
          'https://www.kern.courts.ca.gov/system/files/general/local-rules-court-effective-7126.pdf',
          'local rule 1.9, 1 July 2026'),
    Court('Kings', REFUSES, 'Does not accept direct facsimile filing.', None, None,
          'https://www.kings.courts.ca.gov/system/files/local-rules/kingscountylocalrules_07-01-26.pdf',
          'local rule 112, 1 July 2026'),
    Court('Shasta', REFUSES, 'Does not accept direct fax filing under rule 2.304.', None, None,
          'https://shasta.courts.ca.gov/system/files/roc/roc_jan26.pdf', 'January 2026 rules'),
    Court('Trinity', REFUSES, 'Does not accept direct fax filing under rule 2.304.', None, None,
          'https://www.trinity.courts.ca.gov/system/files/local_rules.pdf'),
    Court('Tulare', REFUSES, 'Documents may not be directly fax filed; a fax filing agency only.', None, None,
          'https://www.tulare.courts.ca.gov/system/files/local-rules/final-proposed-1-2027-local-rules-comment_0.pdf'),
    Court('El Dorado', REFUSES, 'Only criminal complaints, parole violation petitions and tribal protective orders '
          'by fax.', None, None, 'https://www.eldorado.courts.ca.gov/system/files/local-rules/local-rules.pdf',
          'local rule 4.00.08'),
    Court('Butte', REFUSES, 'Pleadings only through a fax filing agency; one-page informational letters directly.',
          None, None, 'https://www.butte.courts.ca.gov/system/files/local-rules/'
          'local-rules-january-2026-final-edit_san.pdf', 'local rule 13.1'),
)
NOT_ESTABLISHED = ('Fresno', 'Tehama', 'Glenn', 'Madera', 'Napa', 'Stanislaus', 'Contra Costa', 'Marin', 'Sierra',
                   'San Mateo', 'Solano', 'Inyo', 'Merced', 'Imperial', 'Lake', 'Monterey', 'Sonoma', 'Yolo',
                   'Los Angeles', 'San Diego', 'Nevada', 'Riverside', 'Sacramento', 'San Joaquin', 'Placer',
                   'Santa Cruz')
TREND = ('California courts are leaving fax: of the 32 superior courts whose own pages say either way, 11 still take '
         'direct fax filing, most of them small courts and some only for narrow case types, 8 have repealed it since '
         "2011, and 13 refuse it. A court's own local rule decides; check it before you rely on fax filing.")


def court(county):
    """The listed court for a county name (any case, with or without "County"), or None."""
    words = [word for word in str(county or '').split() if word.casefold() != 'county']
    name = ' '.join(words).casefold()
    return next((item for item in COURTS if item.county.casefold() == name), None)


def filed_on(county, received_at: datetime):
    """When a court's local rule files a fax received at ``received_at`` (UTC): (date, sentence), or None when the
    court publishes no cut-off. Rule 2.304 itself sets none."""
    found = court(county)
    if found is None or found.cutoff is None:
        return None
    moment = local(received_at)
    day = moment.date()
    clock = f'{found.cutoff:%I:%M %p}'.lstrip('0').replace('AM', 'a.m.').replace('PM', 'p.m.')
    if moment.timetz().replace(tzinfo=None) > found.cutoff or not is_court_day(day):
        filed = next_court_day(day)
        return filed, (f"{found.county}'s local rule files faxes received after {clock}, or on a day the court is "
                       f'closed, on the next court day: {_day(filed)}.')
    return day, f"Received before {found.county}'s {clock} cut-off on {_day(day)}, a court day."
