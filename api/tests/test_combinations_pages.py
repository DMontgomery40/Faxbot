"""Combinations, group 1: packing x fax-friendly shading x blank trimming, with encoded pages off (brief 104).

Each mechanism alone, every pair and the three together, made to apply by real inputs only: what the receiving
machine said it accepts (unlimited length, no error correction), the trunk's per-minute rate card, and your page
settings (long pages and blank space on the SIP trunk, fax-friendly shading). Nothing in the layout chooser is
replaced. Each attempt goes through the attempt-time hook (``pages.sending.prepare``) on a real installation's
schema, and what applied is read back from the attempt's own records (``fax_page_changes``,
``fax_friendly_pages``), never from what the case asked for.

The invariant, checked on every case against the fax image Faxbot drew at acceptance:

- every page's content survives: each original page comes back (split from long pages when packed) with every row
  it had, except inside the shaded box when shading applied, and down to its last mark when trimmed;
- only the permitted regions change: the shaded box (screened or whitened) and the white rows a trim leaves out;
- page order and count are right;
- the header band (the rows the fax engine prints its header line over) stays white on every page sent;
- the fax's own PDF and fax image are never changed: the attempt's pages are a file of their own.

Refused combinations are recorded as impossible, with the path the fax took instead read back from its records.
SQLite and PostgreSQL. Synthetic documents and numbers; nothing is dialed.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from api.app import conversion
from api.app.config_values import ConfigurationValues
from api.app.pages import capability, friendly, packing, sending, unpack, views
from api.app.routing import predict
from api.app.routing.costs import RateCard, RateTerms, parse_amount
from api.app.routing.destinations import LOCAL, DestinationClass
from api.app.routing.store import RouteStore
from api.tests.combinations_support import (  # noqa: F401 - fixtures
    ATTEMPT, JOB, NOW, PEER, Case, applied, database, difference_box, digests, ink, inside, installation, needs_gs,
    record, row_bytes, statement_pdf, white_rows)

GROUP = '1 packing x shading x trim'
INVARIANT = ('content survives; only the shaded box and trimmed white rows change; page order and count; header '
             'band white; original kept')
TRUNK = SimpleNamespace(provider_id='sip', manifest=None, traits={'requires_tiff': True})
SINCH = SimpleNamespace(provider_id='sinch', manifest=None, traits={})
CALLER = '+13035550100'
# Synthetic trunk settings, as tests/test_joint_optimizer.py sets them up.
ENVIRONMENT = {'FAX_BACKEND': 'sip', 'SIP_TRUNK_PRESET': 'telnyx', 'SIP_TRUNK_AUTH': 'ip',
               'SIP_TRUNK_CALLER_ID': CALLER, 'SIP_TRUNK_DIDS': CALLER, 'SIP_FAX_FINE': 'true'}
# The trunk bills by the second here, so every second the pages save is money (a synthetic price, no carrier's).
PER_SECOND = RateCard(None, 'sip', 'outbound', 'Synthetic trunk', 'USD', parse_amount('0.005'), 0, 0, 1, 0, None,
                      NOW)
PER_PAGE = RateCard(None, 'sinch', 'outbound', 'Synthetic page service', 'USD', 0, parse_amount('0.03'), 0, 60, 0,
                    None, NOW)
MARGIN = 6  # pixels around the shaded box that the screen and the halftone may differ in


@pytest.fixture(scope='module')
def drawn(tmp_path_factory):
    """The synthetic statement (5 pages, a light-gray box on each, blank lower halves) and the fax image Ghostscript
    draws from it at acceptance, made once for the module."""
    if shutil.which('gs') is None:
        pytest.skip('Ghostscript draws the PDF pages, as acceptance does')
    folder = tmp_path_factory.mktemp('statement')
    document = statement_pdf(folder / 'statement.pdf', pages=5)
    conversion.pdf_to_tiff(str(document.pdf), str(folder / 'statement.tiff'))
    return SimpleNamespace(document=document, tiff=folder / 'statement.tiff')


@contextmanager
def priced(*cards):
    """The real shared predictor (routing/predict.py) on these synthetic rate cards, nothing learned."""
    facts = {card.provider_id: predict.RouteFacts(card.provider_id, card.label, DestinationClass(
        LOCAL, 'US', '+1', PEER), RateTerms(card)) for card in cards}
    with predict.facts_source(lambda route, destination, now=None: facts[route]):
        yield


@dataclass
class Sent:
    prepared: object
    applied: object
    before: dict
    after: dict
    originals: list
    pages: list          # each original page as the call carries it (split back from long pages when packed)
    sheets: list         # the pages on the call
    view: dict | None


def send(engine, folder, drawn, *, pack=False, trim=False, shading='never', whiten=False, ecm=0, fine=True,
         configuration=TRUNK, number=PEER, environment=None, before_send=None):
    """One attempt of the statement to ``number`` by ``configuration``'s route, with the page settings asked for.
    ``before_send(engine, values)`` sets anything else first (a recipient's choice, a pin)."""
    folder = Path(folder)
    folder.mkdir(exist_ok=True)
    image = configuration.provider_id == 'sip'
    pdf, tiff = folder / f'{JOB}.pdf', folder / f'{JOB}.tiff'
    shutil.copyfile(drawn.document.pdf, pdf)
    shutil.copyfile(drawn.tiff, tiff)
    RouteStore(engine, sip_preset=lambda: '').replace_cards([PER_SECOND, PER_PAGE])
    records = capability.records_for(engine)
    records.record_observation(number, source='d' * 32, engine='hylafax',
                               values={'max_length': 'unlimited', 'ecm': ecm, 'fine': 1, 'scan_ms': 10}, now=NOW)
    records.set_route_settings('sip', long_pages=pack, trim_blank=trim)
    if configuration.provider_id != 'sip':
        records.set_route_settings(configuration.provider_id, long_pages=pack)
    values = ConfigurationValues.from_environment({
        **ENVIRONMENT, 'FAX_DATA_DIR': str(folder), 'SIP_FAX_FINE': 'true' if fine else 'false',
        'FAX_FRIENDLY_DOCUMENTS': shading, 'FAX_FRIENDLY_WHITEN': 'true' if whiten else 'false',
        **(environment or {})})
    if before_send is not None:
        before_send(engine, values)
    before = digests(pdf, tiff)
    with priced(PER_SECOND, PER_PAGE):
        prepared = sending.prepare(engine, values, configuration,
                                   SimpleNamespace(job_id=JOB, attempt_id=ATTEMPT, members=()),
                                   {'to_number': number}, pdf, tiff if image else None, now=NOW)
    originals = list(conversion.read_fax_frames(str(tiff)))
    sheets = originals
    if prepared is not None and prepared.tiff is not None:
        sheets = list(conversion.read_fax_frames(prepared.tiff))
    split = unpack.split_frames(sheets) if sheets is not originals else None
    return Sent(prepared, applied(engine, JOB, ATTEMPT), before, digests(pdf, tiff), originals,
                list(split) if split else sheets, sheets, views.sent_view(engine, JOB, str(folder)))


def assert_invariant(sent, drawn, applied_mechanisms):
    """The group's invariant on one attempt (see the module's docstring)."""
    shade = drawn.document.shade
    # The original document is kept, and a changed attempt has a file of its own beside it.
    assert sent.after == sent.before
    if sent.prepared is not None and sent.prepared.tiff is not None:
        assert Path(sent.prepared.tiff).name == f'{sending.PREFIX}{JOB}-{ATTEMPT}.tiff'
    # Page order and count: one sent page per original (split back from long pages when packed).
    assert len(sent.pages) == len(sent.originals) == drawn.document.pages
    if 'dense' in applied_mechanisms:
        assert len(sent.sheets) < len(sent.originals)
    else:
        assert len(sent.sheets) == len(sent.originals)
    shaded = applied_mechanisms & {'screened', 'whitened'}
    for number, (original, page) in enumerate(zip(sent.originals, sent.pages), start=1):
        assert page.width == original.width, f'page {number} changed width'
        kept = page.height
        assert kept <= original.height
        if 'trim' in applied_mechanisms:
            assert kept < original.height, f'page {number} kept its blank bottom'
        else:
            assert kept == original.height, f'page {number} lost rows'
        # Rows left out are white: nothing but blank paper was trimmed.
        assert white_rows(original, kept, original.height), f'page {number} lost a mark below row {kept}'
        top = original.crop((0, 0, original.width, kept))
        changed = difference_box(top, page)
        if shaded:
            # Only the shaded box changes; every pixel outside it is exactly as acceptance drew it.
            assert changed is not None and inside(changed, shade, MARGIN), f'page {number} changed at {changed}'
            if 'screened' in shaded:
                assert ink(page, shade) > 0  # the shading is kept, as a pattern
            else:
                assert ink(page, shade) == 0  # light shading made white
        else:
            assert changed is None, f'page {number} changed at {changed}'
        # The page is its own: its title rows match its own original (order).
        assert row_bytes(page)[:400] == row_bytes(original)[:400]
    # The header band: the rows the fax engine prints its header line over are white on every page on the call.
    for sheet in sent.sheets:
        assert white_rows(sheet, 0, packing.TOP_ROWS)


COMBINATIONS = [
    (),
    ('dense',),
    ('screened',),
    ('trim',),
    ('dense', 'screened'),
    ('dense', 'trim'),
    ('screened', 'trim'),
    ('dense', 'screened', 'trim'),
    # The opt-in: light areas made white instead of screened.
    ('whitened',),
    ('dense', 'whitened'),
    ('trim', 'whitened'),
    ('dense', 'trim', 'whitened'),
]


@needs_gs
@pytest.mark.parametrize('requested', COMBINATIONS, ids=lambda combination: '+'.join(combination) or 'none')
def test_each_mechanism_alone_every_pair_and_all_three_keep_every_page_intact(installation, tmp_path, drawn,
                                                                              requested):
    asked = set(requested)
    shading = 'always' if asked & {'screened', 'whitened'} else 'never'
    sent = send(installation, tmp_path / 'fax', drawn, pack='dense' in asked, trim='trim' in asked, shading=shading,
                whiten='whitened' in asked)
    found = sent.applied.mechanisms
    # What applied is exactly what was asked for, read back from the attempt's records.
    assert found == asked
    assert_invariant(sent, drawn, found)
    if not asked:
        assert sending.unchanged(sent.prepared) and sent.applied.change is None and sent.applied.friendly is None
    else:
        # The Sent detail says each change once, and nothing that did not apply.
        sentences = ' '.join(sent.view['sentences'])
        assert ('long page' in sentences) == ('dense' in found)
        assert ('Blank space' in sentences) == ('trim' in found)
        assert ('fax-friendly pattern' in sentences) == ('screened' in found)
        assert ('made white' in sentences) == ('whitened' in found)
    if 'dense' in found:
        assert sent.applied.change['sent_pages'] == len(sent.sheets) < sent.applied.change['original_pages'] == 5
    record(Case(GROUP, 'combination', tuple(sorted(asked)), tuple(sorted(found)), INVARIANT))


@needs_gs
def test_screening_by_default_on_a_trunk_billed_by_time_combines_with_packing_and_trimming(installation, tmp_path,
                                                                                           drawn):
    """The default setting ("where it saves time"), not Always: on a trunk billed by the second, the screened pages
    cost less, so they go, packed and trimmed."""
    sent = send(installation, tmp_path / 'fax', drawn, pack=True, trim=True, shading='where_it_saves')
    assert sent.applied.mechanisms == {'dense', 'screened', 'trim'}
    assert_invariant(sent, drawn, sent.applied.mechanisms)
    record(Case(GROUP, 'default shading on a per-second trunk', ('dense', 'screened', 'trim'),
                tuple(sorted(sent.applied.mechanisms)), INVARIANT))


# Combinations the product refuses: what it did instead ----------------------------------------------------------

@needs_gs
def test_trimming_is_refused_on_a_cloud_route_and_the_pages_go_packed_with_their_blank_bottoms(installation,
                                                                                              tmp_path, drawn):
    records = capability.records_for(installation)
    with pytest.raises(ValueError) as refused:
        records.set_route_settings('sinch', trim_blank=True)
    assert str(refused.value) == 'Blank space is left out on your phone line only.'
    # Asked anyway (trimming on for the installation, a machine without error correction): a cloud route sends the
    # pages as a PDF, packed when long pages are on there, never trimmed.
    sent = send(installation, tmp_path / 'fax', drawn, pack=True, trim=True, configuration=SINCH)
    assert sent.applied.mechanisms == {'dense'}
    assert sent.applied.change['trimmed_pages'] is None and sent.prepared.tiff is None
    assert conversion.validate_pdf(sent.prepared.pdf) == sent.applied.change['sent_pages'] == 2
    assert sent.after == sent.before
    record(Case(GROUP, 'trim on a cloud route', ('dense', 'trim'), tuple(sorted(sent.applied.mechanisms)),
                'original kept; nothing trimmed', 'impossible',
                alternate='refused: "Blank space is left out on your phone line only."; sent packed as a PDF with '
                          'its blank bottoms'))


@needs_gs
def test_a_machine_with_error_correction_gets_no_trim_and_the_rest_still_applies(installation, tmp_path, drawn):
    sent = send(installation, tmp_path / 'fax', drawn, pack=True, trim=True, shading='always', ecm=1)
    assert sent.applied.mechanisms == {'dense', 'screened'}
    assert_invariant(sent, drawn, sent.applied.mechanisms)
    assert not any('Blank space' in sentence for sentence in sent.view['sentences'])
    record(Case(GROUP, 'trim to a machine with error correction', ('dense', 'screened', 'trim'),
                tuple(sorted(sent.applied.mechanisms)), INVARIANT, 'impossible',
                alternate='packed and screened; blank bottoms kept (a white row costs almost nothing with ECM)'))


@needs_gs
def test_a_trunk_that_sends_standard_resolution_neither_packs_nor_trims_fine_pages(installation, tmp_path, drawn):
    """The SSL Fax engine would draw fine pages again at standard resolution, so their rows cannot be counted on:
    nothing is packed or trimmed; shading still applies."""
    sent = send(installation, tmp_path / 'fax', drawn, pack=True, trim=True, shading='always', fine=False)
    assert 'dense' not in sent.applied.mechanisms and 'trim' not in sent.applied.mechanisms
    assert sent.applied.mechanisms == {'screened'}
    assert_invariant(sent, drawn, sent.applied.mechanisms)
    record(Case(GROUP, 'pack and trim on a standard-resolution trunk', ('dense', 'screened', 'trim'),
                tuple(sorted(sent.applied.mechanisms)), INVARIANT, 'impossible',
                alternate='one page per sheet at full length; shading only'))


@needs_gs
def test_the_recipients_never_for_shading_beats_always_and_the_pages_still_pack_and_trim(installation, tmp_path,
                                                                                         drawn):
    def never(engine, values):
        friendly.set_recipient_choice(engine, PEER, 'never', actor='synthetic')
    sent = send(installation, tmp_path / 'fax', drawn, pack=True, trim=True, shading='always', before_send=never)
    assert sent.applied.mechanisms == {'dense', 'trim'}
    assert_invariant(sent, drawn, sent.applied.mechanisms)
    record(Case(GROUP, "recipient's never for shading under Always", ('dense', 'screened', 'trim'),
                tuple(sorted(sent.applied.mechanisms)), INVARIANT, 'impossible',
                alternate='packed and trimmed; shading as drawn'))


@needs_gs
def test_the_recipients_never_for_long_pages_keeps_one_page_per_sheet_screened_and_trimmed(installation, tmp_path,
                                                                                           drawn):
    def never(engine, values):
        capability.records_for(engine).set_recipient_settings(PEER, packing='never')
    sent = send(installation, tmp_path / 'fax', drawn, pack=True, trim=True, shading='always', before_send=never)
    assert sent.applied.mechanisms == {'screened', 'trim'}
    assert_invariant(sent, drawn, sent.applied.mechanisms)
    record(Case(GROUP, "recipient's never for long pages", ('dense', 'screened', 'trim'),
                tuple(sorted(sent.applied.mechanisms)), INVARIANT, 'impossible',
                alternate='one page per sheet, screened and trimmed'))


@needs_gs
def test_whitening_never_applies_while_the_setting_for_your_documents_is_never(installation, tmp_path, drawn):
    sent = send(installation, tmp_path / 'fax', drawn, pack=True, trim=True, shading='never', whiten=True)
    assert sent.applied.mechanisms == {'dense', 'trim'}
    assert_invariant(sent, drawn, sent.applied.mechanisms)
    record(Case(GROUP, 'whitening with shading at Never', ('dense', 'trim', 'whitened'),
                tuple(sorted(sent.applied.mechanisms)), INVARIANT, 'impossible',
                alternate='packed and trimmed; light areas as drawn'))


@needs_gs
def test_a_registered_senders_recipient_gets_the_pages_exactly_as_they_are(installation, tmp_path, drawn):
    """A pinned recipient's copy is binding (routing/sender_pins.py): no packing, trimming or shading, whatever the
    settings say; the pages go as they are."""
    from api.app.routing import sender_pins

    def pin(engine, values):
        sender_pins.record(engine, values, PEER, account='sip', caller_id=CALLER)
    sent = send(installation, tmp_path / 'fax', drawn, pack=True, trim=True, shading='always', before_send=pin)
    assert sent.prepared is None and sent.applied.mechanisms == frozenset()
    assert sent.applied.change is None and sent.applied.friendly is None
    assert_invariant(sent, drawn, frozenset())
    record(Case(GROUP, 'registered sender (pinned recipient)', ('dense', 'screened', 'trim'), (),
                INVARIANT, 'impossible', alternate='the pages as they are (the binding copy is the document itself)'))
