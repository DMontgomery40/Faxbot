"""Which encoded-page variants a decoder reads, as partners announce them (``payload_layouts``).

A partner's Faxbot says, in the capabilities it signs (``direct/crypto.py``), which variants its decoder reads. One
token names one variant exactly: the layout, with ``/<profile>`` for a layout whose pages carry a profile byte
(``pages.py``). A future variant gets a new token, so a token never changes meaning:

- ``grid``, ``runs``, ``picture``: payload format 1, which every Faxbot with encoded pages decodes (codec v1);
- ``enumerative/1``: the enumerative layout's profile 1 (``enumerative.PROFILE``), format 1;
- ``capacity/1``, ``capacity/2``, ``capacity/3``: the capacity layout's profiles (``capacity.PROFILES``), format 2.

The sender's chooser (``decision.choose``) sends a partner only a variant it announced. A partner whose statement
does not say gets the format 1 layouts every version decodes (``BASELINE``), plus capacity pages when you recorded
that its decoder reads them (the number's decoder setting, migration 0071), because a partner built between that
setting and this announcement reads them without saying so. This module imports nothing heavy, so the partner code
can read it without loading the codec.
"""

GRID, RUNS, PICTURE = 'grid', 'runs', 'picture'
ENUMERATIVE_PROFILE = 1
CAPACITY_PROFILES = (1, 2, 3)

# What every Faxbot that has encoded pages decodes (codec v1, e10ba0c4).
BASELINE = frozenset({GRID, RUNS, PICTURE})
# What this release's decoder reads (pages.decode): its signed capabilities announce exactly these.
DECODED = BASELINE | {f'enumerative/{ENUMERATIVE_PROFILE}'} | {f'capacity/{profile}' for profile in CAPACITY_PROFILES}

# How the console and the command line name each variant.
LABELS = {
    GRID: 'grid', RUNS: 'run-coded', PICTURE: 'picture', f'enumerative/{ENUMERATIVE_PROFILE}': 'enumerative profile 1',
    'capacity/1': 'capacity (shortest call)', 'capacity/2': 'capacity (balanced)', 'capacity/3': 'capacity (fewest pages)',
}


def token(layout, profile=None):
    """The variant token of one candidate's pages: ``layout``, with its profile for enumerative and capacity pages."""
    if layout == 'enumerative':
        return f'enumerative/{ENUMERATIVE_PROFILE if profile is None else int(profile)}'
    if layout == 'capacity':
        return f'capacity/{1 if profile is None else int(profile)}'
    return layout


def allowed_for_partner(announced, *, decoder='any'):
    """The variants a partner may be sent: what its newest signed statement announced, or, when it did not say,
    ``BASELINE`` plus capacity pages when you recorded that its decoder reads them."""
    if announced is not None:
        return frozenset(announced) & DECODED
    extra = {f'capacity/{profile}' for profile in CAPACITY_PROFILES} if decoder == 'capacity' else set()
    return BASELINE | extra


def stored(tokens):
    """Variant tokens as the partner table keeps them (``direct_peers``, migration 0082): a sorted JSON list."""
    import json
    return json.dumps(sorted(set(tokens)), separators=(',', ':'))


def described(tokens):
    """The variants in reading order, as the console and the command line name them; unknown tokens are left out."""
    order = [GRID, RUNS, PICTURE, f'enumerative/{ENUMERATIVE_PROFILE}'] + [f'capacity/{p}' for p in CAPACITY_PROFILES]
    return [LABELS[item] for item in order if item in tokens]
