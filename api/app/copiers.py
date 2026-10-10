"""Copiers that fax over the network (research N22): which makers document SIP fax with T.38, and what to set.

A copier with an IP fax option can send its faxes to Faxbot over your network instead of an analog line, and
keep its Fax button and address book. Only makers whose own documents show SIP with T.38 are listed with steps;
the others are listed as "not found" so nobody assumes it (Canon's I-fax is T.37 email and "cannot communicate
with IP fax machines that comply with the T.38 standard"). Everything here was read on 2026-10-10.

Faxbot does not yet take a copier's call as a fax to send on (a copier extension that hands its pages to Faxbot's
routing): that part is still to be built, so the steps stop at what each copier documents. A copier that sends by
email (Sharp's Direct SMTP, T.37 internet fax) reaches Faxbot through an email connector instead, whose copier
senders rule admits it (intake/sources/mail.py).
"""
from __future__ import annotations

from dataclasses import dataclass


SIP_T38, NOT_FOUND = 'sip_t38', 'not_found'
READ_ON = '2026-10-10'


@dataclass(frozen=True)
class Copier:
    id: str
    label: str
    verdict: str                    # SIP_T38 when the maker documents SIP fax with T.38, else NOT_FOUND
    summary: str
    steps: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()


COPIERS = (
    Copier('ricoh-im', 'Ricoh IM series (IP-Fax)', SIP_T38,
           'Ricoh documents IP-Fax over SIP or H.323, compliant with T.38, sent through a gateway or a SIP server.',
           steps=(
               'Fax Settings, Detailed Initial Settings, IP-Fax Settings, IP-Fax Use Settings: turn on Enable SIP '
               '(it is off by default).',
               'SIP Settings: enter the SIP user name; set SIP Digest Authentication only if your SIP server asks for '
               'it.',
               'Register/Change/Delete Gateway: add a gateway with a Prefix, Select Protocol SIP and Gateway Address '
               'Faxbot\'s address, so ordinary fax numbers are sent to Faxbot.',
               'Send to an ordinary fax number as usual (Ricoh\'s example: 0312345678); the gateway prefix chooses '
               'the route.',
           ),
           sources=('https://support.ricoh.com/services/device/ccmanual/IM_550-re/en-GB/fax/int/ipfax.htm',)),
    Copier('konica-minolta-bizhub', 'Konica Minolta bizhub (IP fax, SIP)', SIP_T38,
           'Konica Minolta documents IP fax over SIP with T.38 as an option: "To use the IP fax (SIP) function, an '
           'option is required."',
           steps=(
               'IP-FAX(T38) Function Settings: ON (it is OFF by default).',
               'IP-FAX(T38) Detail Setting: keep Port Number1 10000 and Timeout 60 seconds unless your network needs '
               'others.',
               'SIP Basic Setting: User ID and Domain Name, Transport Mode UDP and Port Number 5060.',
               'SIP Server Setting: Faxbot\'s address (the fields are on Konica Minolta\'s linked page).',
           ),
           sources=('https://manuals.konicaminolta.eu/bizhub-751i/EN/contents/WC_08_03.html',)),
    Copier('xerox-workcentre-foip', 'Xerox WorkCentre (Fax over IP)', SIP_T38,
           'Xerox documents Fax over IP with SIP and T.38 as a purchasable option on WorkCentre 5325/5330/5335 and '
           '7120/7125, including "WorkCentre to SIP Fax Server to a SIP Device".',
           steps=('Buy and install the Fax over IP option, then point its SIP fax server at Faxbot\'s address.',),
           sources=('https://support.xerox.com/en-us/article/KB0216504',)),
    Copier('canon-imagerunner', 'Canon imageRUNNER ADVANCE', NOT_FOUND,
           'Canon\'s I-fax is T.37 internet fax by email and "cannot communicate with IP fax machines that comply with '
           'the T.38 standard"; use an email connector for it.',
           sources=('https://oip.manual.canon/USRMA-0099-zz-CS-enUS/contents/1T0002183917.html',)),
    Copier('sharp-mx', 'Sharp MX and BP series', NOT_FOUND,
           'Sharp documents Internet Fax by email, with a Direct SMTP mode that sends straight to an address you '
           'enter; no SIP fax was found. Use an email connector with its copier senders rule.',
           sources=('https://global.sharp/restricted/print/manuals/3/bp71m65/us/contents_09-04_001.html',)),
    Copier('kyocera', 'Kyocera TASKalfa and ECOSYS', NOT_FOUND, 'No SIP fax with T.38 was found in Kyocera\'s '
           'documents.'),
)


def catalog():
    """What the console and the command line list: each copier with its verdict, steps and sources."""
    return [{'id': item.id, 'label': item.label, 'sip_t38': item.verdict == SIP_T38, 'summary': item.summary,
             'steps': list(item.steps), 'sources': [{'url': url, 'read_on': READ_ON} for url in item.sources]}
            for item in COPIERS]
