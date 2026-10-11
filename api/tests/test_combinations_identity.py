"""Combinations, group 3: mailbox caller identity x caller-origin price x header notice and cover policy x approval
(brief 104).

The invariant:

- the identity sent is the identity priced: the caller ID the real Originate fields carry for a fax's mailbox is the
  caller ID whose rate row priced its attempt, and its station ID is the same reply number;
- required covers and notices survive: a recipient that needs a cover keeps it, and the notice band on every page
  comes through packing, shading and trimming pixel for pixel, under a header strip that stays clear;
- the approval's hash matches the transmitted content: what the call sends is made from the document the approval
  was bound to, and a document changed after it was held cannot be approved;
- the original is kept: the sender's whole upload beside the fax, and the fax's own files unchanged by sending.

Identity and approval run through the real outbound worker (route choice, page preparation, the real Originate
fields through a stand-in AMI that dials nothing). The notice is drawn by ``header_notice.render`` exactly as
acceptance draws it. Cover policy runs through the real POST /fax. The rate deck is the synthetic Twilio-layout deck
of tests/test_origin_classes.py; every number and price is synthetic. SQLite and PostgreSQL.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from api.app import conversion
from api.app.config_profiles import ProviderConfiguration
from api.app.pages import packing
from api.app.routing import origin_classes, reply_number
from api.app.routing.continuation import BAND_POINTS, CLEAR_POINTS
from api.tests.combinations_support import (  # noqa: F401 - fixtures
    FINE_DPI, PAGE_POINTS, SHADE_BOX_POINTS, Case, database, difference_box, digests, ink, inside, installation,
    needs_gs, record, row_bytes, statement_pdf, to_pixels, white_rows)
from api.tests.test_origin_classes import AT_LANDLINE, US_CALLER, rows as deck_rows
from api.tests.test_reply_number import client  # noqa: F401 - fixture
from api.tests.test_rules_delivery import BASE, BEN, accept, holds, installation as rules_installation, publish, rule

GROUP = '3 caller identity x origin price x notice and cover x approval'
INVARIANT = ('identity sent = identity priced; required cover and notice survive; approval hash = transmitted '
             'content; original kept')
NOTICE = 'Confidential: for the addressee only. If this reached you in error, call 303-555-0101.'
FRONT = '+13035550142'  # the organization's reply number: one of the trunk's numbers, not confirmed with the carrier
ENVIRONMENT = {**BASE, 'FAX_BACKEND': 'sip', 'FAX_OUTBOUND_ROUTES': '', 'SIP_TRUNK_PRESET': 'telnyx',
               'SIP_TRUNK_AUTH': 'ip', 'SIP_TRUNK_HOST': 'sip.telnyx.com', 'SIP_TRUNK_CALLER_ID': US_CALLER,
               'SIP_TRUNK_DIDS': f'{US_CALLER},{FRONT}', 'FAX_REPLY_NUMBER': FRONT,
               'FAX_REPLY_NUMBERS': f'billing={US_CALLER}', 'INBOUND_ENABLED': 'true', 'FAX_DEFAULT_COUNTRY': 'US',
               'FAX_FRIENDLY_DOCUMENTS': 'never'}


AUSTRIA = rule('r-austria', {'cheapest_reliable': ['sip']}, {'destination': {'countries': ['AT']}})


# The worker installation ------------------------------------------------------------------------------------------

class Ami:
    """A connected stand-in for Asterisk that dials nothing: for each call it keeps the real Originate fields
    (``ami.originate_fields_for``, as ``AMIClient.originate_sendfax`` builds them) and what the call would send."""

    def __init__(self, values):
        self._connected = asyncio.Event()
        self._connected.set()
        self.values, self.calls = values, []

    async def originate_sendfax(self, job_id, dest, tiff_path, *, attempt_id=None, call=None, trunk=None):
        from api.app.ami import originate_fields_for
        fields = originate_fields_for(self.values(), job_id, dest, tiff_path, attempt_id=attempt_id, call=call,
                                      trunk=trunk)
        variables = dict(item.split('=', 1) for item in fields['Variable'].split(',') if '=' in item)
        self.calls.append({'job_id': job_id, 'to': dest, 'tiff': tiff_path, 'attempt_id': attempt_id,
                           'caller_id': fields['CallerID'],
                           'station': base64.b64decode(variables['FAXSTATION64']).decode(),
                           'sha256': hashlib.sha256(Path(tiff_path).read_bytes()).hexdigest()})


class _Frame:
    def __init__(self, revision):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


@pytest.fixture
def trunk(database, tmp_path, monkeypatch):  # noqa: F811 - the fixture
    """One SIP trunk with two numbers: the organization's reply number (not confirmed) and the Billing mailbox's
    (confirmed on the account). A carrier rate deck prices Austria by caller ID. Sending is on; nothing is dialed."""
    from api.app import ami, config
    from api.app.routing.seed import load_cards
    from api.app.routing.store import RouteStore
    env = rules_installation(database, tmp_path, ENVIRONMENT,
                             outbound=ProviderConfiguration('sip', traits={'requires_tiff': True}))
    env.routes = RouteStore(database, sip_preset=lambda: 'telnyx')
    env.routes.seed_cards(load_cards())
    origin_classes.import_deck(database, 'sip-telnyx', deck_rows())
    origin_classes.record_eligibility(database, 'sip', US_CALLER, bought_here=False, evidence='Synthetic order')
    # Which number reaches which mailbox (the Numbers rules), given directly as tests/test_origin_classes.py does.
    monkeypatch.setattr(reply_number, 'mailbox_routes', lambda engine, values: [
        reply_number.Route(US_CALLER, 'billing', 'Billing'), reply_number.Route(FRONT, 'front', 'Front desk')])
    # The call reads the fax's mailbox and reply number from the installation's database.
    monkeypatch.setattr(ami, '_database', lambda: database)
    monkeypatch.setattr(config, 'configuration_values', lambda: env.configuration.read().active.values)
    monkeypatch.setattr('api.app.sip_trunk.trunk_loaded', lambda values, key: True)
    env.ami = Ami(lambda: env.configuration.read().active.values)
    env.runtime = SimpleNamespace(frame=_Frame)
    # A rule that names Austria, so Faxbot may dial there (routing/guard.py).
    publish(env, {'format': 1, 'routes': [AUSTRIA]})
    return env


def transport(env):
    from api.app.outbound_transport import CapturedTransport
    from api.app.routing.transport import RoutedTransport
    return RoutedTransport(CapturedTransport(env.delivery, env.runtime, ami=env.ami), direct=None,
                           route_store=env.routes)


@pytest.fixture(scope='module')
def statement(tmp_path_factory):
    """A two-page synthetic statement and its fax image, drawn once."""
    if shutil.which('gs') is None:
        pytest.skip('Ghostscript draws the PDF pages, as acceptance does')
    folder = tmp_path_factory.mktemp('statement')
    document = statement_pdf(folder / 'statement.pdf', pages=2, shaded=False)
    conversion.pdf_to_tiff(str(document.pdf), str(folder / 'statement.tiff'))
    return SimpleNamespace(document=document, tiff=folder / 'statement.tiff')


def fax(env, drawn, *, mailbox=None, approval_bound=False):
    """Accept a fax to the Austrian landline from ``mailbox`` and leave its files as acceptance does."""
    from api.app.routing.holds import document_sha256
    job_pdf = env.tmp / 'incoming.pdf'
    shutil.copyfile(drawn.document.pdf, job_pdf)
    job = accept(env, to=AT_LANDLINE, pages=drawn.document.pages, mailbox=mailbox,
                 document_sha256=document_sha256(job_pdf) if approval_bound else None)
    job_pdf.replace(env.tmp / f'{job}.pdf')
    shutil.copyfile(drawn.tiff, env.tmp / f'{job}.tiff')
    return job, digests(env.tmp / f'{job}.pdf', env.tmp / f'{job}.tiff')


def accounts_priced(monkeypatch):
    """Every ``pages.sending.Account`` page preparation builds while the worker runs, as it built it."""
    from api.app.pages import sending
    built, original = [], sending.account_for

    def spy(*args, **kwargs):
        found = original(*args, **kwargs)
        built.append(found)
        return found
    monkeypatch.setattr(sending, 'account_for', spy)
    return built


async def step(env):
    from api.app.outbound_worker import OutboundWorker
    return await OutboundWorker(env.delivery, transport(env)).step()


# Identity sent = identity priced -----------------------------------------------------------------------------------

@needs_gs
@pytest.mark.asyncio
@pytest.mark.parametrize('mailbox, caller, eligibility', [('billing', US_CALLER, 'non_surcharged'),
                                                         (None, FRONT, 'unconfirmed')],
                         ids=['billing mailbox', 'no mailbox'])
async def test_the_caller_id_a_call_carries_is_the_one_its_attempt_was_priced_by(trunk, statement, mailbox, caller,
                                                                                eligibility, monkeypatch):
    from api.app.pages import sending
    from api.app.routing.pricing import price
    priced = accounts_priced(monkeypatch)
    job, originals = fax(trunk, statement, mailbox=mailbox)
    assert await step(trunk) is True
    [call] = trunk.ami.calls
    # What the call presents: the mailbox's reply number (else the organization's), as caller ID and station ID.
    assert (call['caller_id'], call['station']) == (caller, caller)
    # What page preparation priced this attempt's pages with, as it ran: the same caller ID, and its own rate row.
    [account] = [found for found in priced if found.key == 'sip']
    assert (account.caller_id, account.mailbox_id) == (caller, mailbox)
    assert account.facts.origin == f'caller:{eligibility}'
    values = trunk.configuration.read().active.values
    expected = price(trunk.routes, values, 'sip', AT_LANDLINE, statement.document.pages, provider='sip',
                     mailbox_id=mailbox)
    other = price(trunk.routes, values, 'sip', AT_LANDLINE, statement.document.pages, provider='sip',
                  mailbox_id=None if mailbox else 'billing')
    assert account.facts.terms.card.per_minute_micros == expected.rate_card.per_minute_micros
    assert expected.origin == account.facts.origin and expected.micros != other.micros  # the identity decides it
    assert sending.Account.fingerprint(account)  # the identity is part of what a measured selection is held to
    # The original is kept: the fax's own files are unchanged, and the call sent its own fax image.
    assert digests(trunk.tmp / f'{job}.pdf', trunk.tmp / f'{job}.tiff') == originals
    assert call['sha256'] == originals[f'{job}.tiff']
    record(Case(GROUP, f'caller identity: {mailbox or "no mailbox"}', ('mailbox reply number', 'origin price'),
                (f'caller:{caller}', f'priced:{eligibility}'), INVARIANT))


# The approval's hash matches the transmitted content --------------------------------------------------------------

APPROVAL = {'format': 1, 'limits': [rule('l-approve', {'hold_for_approval': {'separate_approver': True}})],
            'routes': [AUSTRIA]}


@needs_gs
@pytest.mark.asyncio
async def test_an_approved_fax_from_a_mailbox_sends_the_document_it_was_approved_for_with_that_mailboxs_identity(
        trunk, statement):
    from api.app.routing import envelope as envelopes
    from api.app.routing.holds import HoldStore, digest_for, document_sha256
    publish(trunk, APPROVAL)
    job, originals = fax(trunk, statement, mailbox='billing', approval_bound=True)
    [hold] = holds(trunk, job)
    assert hold['kind'] == 'approval' and hold['bound_digest']
    assert await step(trunk) is False and trunk.ami.calls == []  # nothing goes while it is held
    HoldStore(trunk.delivery).approve(hold['id'], version=hold['version'], actor=BEN, actor_name='Ben Example')
    assert await step(trunk) is True
    [call] = trunk.ami.calls
    # The approval's hash is the hash of the document the call's pages were made from, now.
    pinned = envelopes.load(trunk.engine, job)
    assert digest_for(pinned, job, AT_LANDLINE, document_sha256(trunk.tmp / f'{job}.pdf')) == hold['bound_digest']
    assert call['sha256'] == originals[f'{job}.tiff']
    assert digests(trunk.tmp / f'{job}.pdf', trunk.tmp / f'{job}.tiff') == originals
    assert (call['caller_id'], call['station']) == (US_CALLER, US_CALLER)
    record(Case(GROUP, 'approval x mailbox identity', ('approval', 'mailbox reply number', 'origin price'),
                ('approved', f'caller:{US_CALLER}'), INVARIANT))


@needs_gs
@pytest.mark.asyncio
async def test_a_document_changed_while_held_cannot_be_approved_and_nothing_is_sent(trunk, statement):
    from api.app.routing.holds import HoldConflict, HoldStore
    publish(trunk, APPROVAL)
    job, _ = fax(trunk, statement, mailbox='billing', approval_bound=True)
    [hold] = holds(trunk, job)
    pdf = trunk.tmp / f'{job}.pdf'
    pdf.write_bytes(pdf.read_bytes() + b'\n% changed after it was held\n')
    with pytest.raises(HoldConflict) as refused:
        HoldStore(trunk.delivery).approve(hold['id'], version=hold['version'], actor=BEN)
    assert str(refused.value) == ('The fax changed after it was held, so this approval cannot apply. Refuse it and '
                                  'send it again.')
    assert await step(trunk) is False and trunk.ami.calls == []
    record(Case(GROUP, 'approval of a document changed while held', ('approval', 'changed document'), (),
                INVARIANT, 'impossible', alternate='refused: "Refuse it and send it again."; nothing sent'))


@needs_gs
@pytest.mark.asyncio
@pytest.mark.xfail(strict=True, reason='defect C3: a document changed after approval is sent under that approval')
async def test_a_document_changed_after_its_approval_is_not_sent_under_it(trunk, statement):
    """The approval binds to the document (README, sending rules: "An approval binds to the document, the number and
    the rules' versions"). If the fax's document and image are replaced after it was approved and before it is sent,
    the call must not send them under that approval. Nothing checks again at dispatch today. Faxbot itself never
    replaces an accepted document (``codec.send.restore_original_image`` rewrites a fax image only from the fax's own
    PDF), so this is the check that would catch anything that did."""
    from api.app.routing.holds import HoldStore
    publish(trunk, APPROVAL)
    job, originals = fax(trunk, statement, mailbox='billing', approval_bound=True)
    [hold] = holds(trunk, job)
    HoldStore(trunk.delivery).approve(hold['id'], version=hold['version'], actor=BEN)
    other = statement_pdf(trunk.tmp / 'other.pdf', pages=2, shaded=False, label='Another synthetic document')
    shutil.copyfile(other.pdf, trunk.tmp / f'{job}.pdf')
    conversion.pdf_to_tiff(str(other.pdf), str(trunk.tmp / f'{job}.tiff'))
    assert digests(trunk.tmp / f'{job}.tiff')[f'{job}.tiff'] != originals[f'{job}.tiff']
    await step(trunk)
    assert [call['sha256'] for call in trunk.ami.calls] in ([], [originals[f'{job}.tiff']])


# Notices and covers survive -----------------------------------------------------------------------------------------

def noticed_statement(folder):
    """The five-page shaded statement with the notice drawn on every page, exactly as acceptance draws it, and its
    fax image; returns the ``drawn`` the group 1 helpers take, with the shaded box where the scaled page puts it."""
    from api.app import header_notice
    plain = statement_pdf(folder / 'plain.pdf', pages=5)
    rendered, count = header_notice.render(plain.pdf.read_bytes(), NOTICE)
    assert count == 5
    (folder / 'noticed.pdf').write_bytes(rendered)
    conversion.pdf_to_tiff(str(folder / 'noticed.pdf'), str(folder / 'noticed.tiff'))
    width, height = PAGE_POINTS
    scale = max(0.5, (height - BAND_POINTS) / height)
    left, bottom, right, top = SHADE_BOX_POINTS
    shift = width * (1 - scale) / 2
    box = to_pixels((left * scale + shift, bottom * scale, right * scale + shift, top * scale))
    document = SimpleNamespace(pdf=folder / 'noticed.pdf', pages=5, shade=box)
    return SimpleNamespace(document=document, tiff=folder / 'noticed.tiff')


@pytest.fixture(scope='module')
def noticed(tmp_path_factory):
    if shutil.which('gs') is None:
        pytest.skip('Ghostscript draws the PDF pages, as acceptance does')
    return noticed_statement(tmp_path_factory.mktemp('noticed'))


CLEAR_ROWS = int(CLEAR_POINTS * FINE_DPI[1] / 72) - 2  # the strip the engine prints its header line in
BAND_ROWS = int(BAND_POINTS * FINE_DPI[1] / 72)


@needs_gs
@pytest.mark.parametrize('requested', [(), ('dense',), ('screened',), ('trim',), ('dense', 'screened', 'trim')],
                         ids=lambda combination: '+'.join(combination) or 'none')
def test_the_notice_on_every_page_survives_packing_shading_and_trimming(installation, tmp_path, noticed, requested):
    from api.tests.test_combinations_pages import send
    asked = set(requested)
    sent = send(installation, tmp_path / 'fax', noticed, pack='dense' in asked, trim='trim' in asked,
                shading='always' if 'screened' in asked else 'never')
    found = sent.applied.mechanisms
    assert found == asked
    assert sent.after == sent.before  # the fax's own files are kept
    assert len(sent.pages) == len(sent.originals) == 5
    for number, (original, page) in enumerate(zip(sent.originals, sent.pages), start=1):
        # The notice band comes through pixel for pixel, with the notice in it, under a clear header strip.
        assert row_bytes(page)[CLEAR_ROWS:BAND_ROWS] == row_bytes(original)[CLEAR_ROWS:BAND_ROWS], number
        assert ink(page, (0, CLEAR_ROWS, page.width, BAND_ROWS)) > 200
        assert white_rows(page, 0, CLEAR_ROWS)
        # Below it, only the shaded box changed (when screened) and only white rows were left out (when trimmed).
        assert white_rows(original, page.height, original.height)
        changed = difference_box(original.crop((0, 0, original.width, page.height)), page)
        if 'screened' in found:
            assert changed is not None and inside(changed, noticed.document.shade, 6), (number, changed)
        else:
            assert changed is None, (number, changed)
    if 'dense' in found:
        for sheet in sent.sheets:
            assert white_rows(sheet, 0, packing.TOP_ROWS)
    else:
        assert all(white_rows(sheet, 0, CLEAR_ROWS) for sheet in sent.sheets)
    record(Case(GROUP, 'notice x ' + ('+'.join(requested) or 'no page change'), ('notice',) + tuple(requested),
                ('notice',) + tuple(sorted(found)), INVARIANT))


def _document(*titles):
    from api.tests.test_header_notice import document
    return document(*titles)


def test_a_recipient_that_needs_its_cover_keeps_it_under_the_notice_and_its_approval_binds_to_the_noticed_pages(
        client):
    """Through the real POST /fax: a notice for every page, a recipient marked as needing a cover sheet, a sender who
    asked for the cover to go as the notice, and a rule that holds every fax for approval. The cover is kept, every
    page carries the notice, the sender's whole document is kept beside the fax, and the approval is bound to the
    noticed document the call would send."""
    from app.main import app
    from app.routing import envelope
    from app.routing.holds import HoldStore, digest_for, document_sha256
    from app.rules.store import RuleStore
    from pypdf import PdfReader
    from api.tests.test_header_notice import TO, _data_dir, _send, _set, _view, texts
    engine = app.state.configuration_runtime.manager.store.engine
    rules = RuleStore(engine)
    draft = rules.save_draft('organization', '', APPROVAL, expected_version=0)
    rules.publish('organization', '', expected_active_revision=None, expected_draft_version=draft['version'])
    _set(client, '/header-notice', {'notice': NOTICE})
    _set(client, f'/header-notice/recipients/{TO}', {'needs_cover': True})
    sent = _send(client, _document('Cover sheet', 'Referral letter'), cover_in_header='true')
    assert sent.status_code == 202, sent.text
    job = sent.json()['id']
    root = _data_dir()
    # The cover is kept, and the notice is on both pages.
    assert sent.json()['pages'] == 2 and len(PdfReader(str(root / f'{job}.pdf')).pages) == 2
    words = ' '.join(texts((root / f'{job}.pdf').read_bytes()))
    assert 'Cover sheet' in words and words.count('Confidential: for the addressee only.') == 2
    view = _view(client, job)
    assert (view['cover'], view['sent_pages']) == ('kept', 2)
    # The sender's whole document is kept beside the fax.
    assert len(PdfReader(str(root / f'{job}.upload.pdf')).pages) == 2
    # The approval is bound to the noticed document: the pages the call sends.
    from app.outbound_store import OutboundStore
    store = OutboundStore(app.state.configuration_runtime.manager.store)
    [hold] = HoldStore(store).holds(state='open', job_id=job)
    assert hold['kind'] == 'approval'
    pinned = envelope.load(engine, job)
    assert digest_for(pinned, job, TO, document_sha256(root / f'{job}.pdf')) == hold['bound_digest']
    assert digest_for(pinned, job, TO, document_sha256(root / f'{job}.upload.pdf')) != hold['bound_digest']
    record(Case(GROUP, 'cover required x cover-as-notice x approval (POST /fax)',
                ('notice', 'cover in header', 'needs cover', 'approval'), ('cover kept', 'notice', 'approval bound'),
                INVARIANT, 'impossible', alternate='the cover is kept (the recipient needs it); its notice also goes '
                                                   'in the header of every page'))
