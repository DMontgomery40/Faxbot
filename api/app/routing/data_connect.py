"""Japan: NTT Hikari Denwa's Data Connect, priced and described from NTT's own documents (brief 92, RF item 4).

Data Connect (データコネクト) is NTT East and West's bandwidth-reserved data call between two Data
Connect-capable devices, addressed by ordinary 0AB-J numbers, on Hikari Denwa over FLET'S Hikari Next. What is
built here is only what NTT publishes, each fact with its page and the day it was read (10 October 2026):

- **Prices** (``TIERS``, ``charge``): ¥1.1 per 30 seconds up to 64 kbit/s, ¥1.65 above 64 and up to 512 kbit/s,
  ¥2.2 above 512 kbit/s and up to 1 Mbit/s, tax included, on the total bandwidth in use; several Data Connect
  sessions at once cost ¥16.5 per 3 minutes from over 1 up to 2.6 Mbit/s and ¥110 per 3 minutes above 2.6 Mbit/s
  (NTT East's price page, marked "2026年10月時点"; NTT West's Data Connect manual pp. 53-54 lists the same).
  NTT charges for as long as the network holds the bandwidth, not only while data flows (NTT West manual pp.
  53-54), so Faxbot prepares the pages before the call and hangs up as soon as the fax ends. NTT does not say how
  a part of a unit is billed; ``charge`` counts every started unit, the usual reading of a price per 30 seconds,
  and says so. An ordinary call to an ordinary fax number is a voice call: ¥8.8 per 3 minutes to fixed lines
  (the preset's rate card).
- **Restrictions** (``ADVICE``): FAXお知らせメール does not work for faxes received over Data Connect (and the
  service closed to new orders on 31 March 2026 and ends on 31 March 2028); residential Hikari Denwa carries
  fax as voice ("みなし音声"); both ends need Data Connect-capable equipment.
- **T.38** (``T38_SDP``): NTT's interface (音声利用IP通信網サービス 第2種サービス タイプ2, version 13.1) lists
  TTC JT-T38 and allows ``m=image`` for bandwidth-reserved data, but its detailed UNI (詳細版) is not published.
  Faxbot offers T.38 exactly as ITU-T T.38 Annex D describes the SDP, with no NTT-specific value. Not yet run
  against NTT: confirm the T.38 parameters with NTT before use.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal


READ_ON = '2026-10-10'
EAST_PRICES = 'https://business.ntt-east.co.jp/service/hikari_of/charge.html'
WEST_MANUAL = 'https://business.ntt-west.co.jp/service/ipphone/office/pdf/share_06_detaconect.pdf'
SERVICE_PAGE = 'https://flets.com/denwa/option/hd-dataconnect/'
EAST_INTERFACE = 'https://flets.com/pdf/hikari_tel_2_2_1_13.1.pdf'
WEST_INTERFACE = 'https://flets-w.com/opt/hikaridenwa/download/hikari_tel6.5.pdf'
T38_STANDARD = 'https://www.itu.int/rec/T-REC-T.38'
SOURCES = (EAST_PRICES, WEST_MANUAL, SERVICE_PAGE, EAST_INTERFACE, WEST_INTERFACE, T38_STANDARD)
CURRENCY = 'JPY'


@dataclass(frozen=True)
class Tier:
    up_to_kbps: int | None     # None: no upper bound
    yen: Decimal
    seconds: int


# One Data Connect session, by the bandwidth in use (NTT East's price page, tax included).
TIERS = (Tier(64, Decimal('1.1'), 30), Tier(512, Decimal('1.65'), 30), Tier(1000, Decimal('2.2'), 30))
# Several Data Connect sessions at once, by their total bandwidth (the same page's footnote).
TOGETHER = (Tier(2600, Decimal('16.5'), 180), Tier(None, Decimal('110'), 180))
# An ordinary call to a fixed line (加入電話, ひかり電話 and the others listed), tax included.
VOICE = Tier(None, Decimal('8.8'), 180)


def tier(total_kbps) -> Tier:
    """The price unit for the total bandwidth in use."""
    kbps = float(total_kbps)
    if kbps <= 0:
        raise ValueError('Give the bandwidth the session reserves, in kbit/s.')
    for found in TIERS + TOGETHER:
        if found.up_to_kbps is None or kbps <= found.up_to_kbps:
            return found
    raise AssertionError('unreachable')


def units(seconds, unit_seconds) -> int:
    """Started units: NTT prices by the unit and does not say how a part unit is billed, so each started one
    counts (check your bill). Zero seconds is no unit."""
    seconds = Decimal(str(seconds))
    if seconds < 0:
        raise ValueError('A call cannot last less than nothing.')
    return int((seconds / unit_seconds).to_integral_value(rounding=ROUND_CEILING))


def charge(reserved_seconds, total_kbps) -> Decimal:
    """Yen, tax included, for bandwidth held ``reserved_seconds`` (charged while held, even with no data)."""
    found = tier(total_kbps)
    return units(reserved_seconds, found.seconds) * found.yen


def voice_charge(seconds) -> Decimal:
    """Yen, tax included, for an ordinary call of ``seconds`` to a fixed line."""
    return units(seconds, VOICE.seconds) * VOICE.yen


# Each a sentence for the trunk page and the setup advice.
ADVICE = (
    'Data Connect works only between Data Connect-capable devices at both ends, on Hikari Denwa over FLET\'S '
    'Hikari Next; an ordinary fax number is reached by an ordinary voice call.',
    'NTT charges Data Connect for as long as the bandwidth is held, even with no data flowing, so Faxbot prepares '
    'the pages before the call and hangs up as soon as the fax ends.',
    'FAX notification email (FAXお知らせメール) does not work for faxes received over Data Connect: have received '
    'faxes come into Faxbot instead. NTT closed that service to new orders on 31 March 2026 and ends it on 31 March '
    '2028.',
    'Residential Hikari Denwa carries fax as voice, not as T.38.',
)

# What Faxbot's engine offers for T.38, in ITU-T T.38 Annex D's SDP terms (no NTT-specific value).
T38_SDP = (
    ('m=image', 'udptl t38'),
    ('T38FaxVersion', '0'),
    ('T38MaxBitRate', '14400'),
    ('T38FaxRateManagement', 'transferredTCF'),
    ('T38FaxUdpEC', 't38UDPRedundancy'),
)
