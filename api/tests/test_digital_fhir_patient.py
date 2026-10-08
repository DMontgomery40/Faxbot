"""The patient a fax is about, on a FHIR route (digital/patient.py, digital/fhir.py): US Core's subject, looked up
by medical record number or confirmed by $match when the recipient's server needs it, the route skipped (and the
fax sent by fax) when the fax lacks what that server needs, and the details never written anywhere but beside the
fax's document.

Everything runs through the real delivery worker, planner, routed transport, digital route and FHIR sender; only
the FHIR server's HTTP is a stand-in. Synthetic names, numbers and record numbers only; nothing contacts a real
FHIR server or EHR.
"""
import asyncio
from datetime import date
import json
import logging
import stat
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
import sqlalchemy as sa

from api.app.config_profiles import ConfigurationDocument
from api.app.digital import accounts as digital_accounts
from api.app.digital import fhir
from api.app.digital import patient as fax_patient
from api.app.digital.routes import candidates
from api.app.digital.text import route_key
from api.app.routing import holds, route_view
from api.app.routing.transport import DirectRefused
from api.tests.test_digital_fhir import DEST, FakeFhir, queue, send, world  # noqa: F401 - world is a fixture


ISSUER = 'urn:oid:2.16.840.1.113883.19.5'
PATIENT = fax_patient.Patient('MRN-5550199', ISSUER, 'Zyxwvut', 'Quillon', '1961-07-23')
SECRETS = ('MRN-5550199', 'Zyxwvut', 'Quillon', '1961-07-23', '19610723', '07/23/1961')


class PatientFhir(FakeFhir):
    """The FHIR stand-in, also answering patient searches and $match; ``echo`` makes its refusals repeat the
    patient, as real servers' error text may."""

    def __init__(self):
        super().__init__()
        self.patients = {(ISSUER, 'MRN-5550199'): 'pat-123'}
        self.lookups = []
        self.grade = 'certain'
        self.lookup_status = 200
        self.echo = False

    def send(self, method, url, headers, data):
        path = urlsplit(url).path
        echoed = {'resourceType': 'OperationOutcome', 'issue': [{'severity': 'error', 'code': 'invalid', 'diagnostics':
                  'Patient MRN-5550199 (Zyxwvut, Quillon, born 1961-07-23) is not known here'}]}
        if path in ('/r4/Patient/_search', '/r4/Patient/$match'):
            assert method == 'POST' and headers['Authorization'].startswith('Bearer synthetic-access-')
            self.lookups.append(SimpleNamespace(path=path, url=url, headers=headers, body=data))
            if self.lookup_status != 200:
                return self._json(self.lookup_status, echoed)
            if path.endswith('_search'):
                assert headers['Content-Type'] == 'application/x-www-form-urlencoded'
                system, _, number = parse_qs(data.decode())['identifier'][0].rpartition('|')
                found = self.patients.get((system, number))
                entries = [{'resource': {'resourceType': 'Patient', 'id': found}, 'search': {'mode': 'match'}}]
            else:
                parameters = json.loads(data)
                assert parameters['resourceType'] == 'Parameters'
                patient = next(item['resource'] for item in parameters['parameter'] if item['name'] == 'resource')
                identifier = patient['identifier'][0]
                found = self.patients.get((identifier.get('system'), identifier['value']))
                entries = [{'resource': {'resourceType': 'Patient', 'id': found}, 'search': {
                    'mode': 'match', 'score': 0.97,
                    'extension': [{'url': fhir.MATCH_GRADE, 'valueCode': self.grade}]}}]
            return self._json(200, {'resourceType': 'Bundle', 'type': 'searchset',
                                    'entry': entries if found else []})
        if self.echo and method == 'POST' and path == '/r4/DocumentReference':
            self.posts.append((headers, json.loads(data)))
            return self._json(422, echoed)
        return super().send(method, url, headers, data)


@pytest.fixture
def clinic(world):  # noqa: F811 - the imported fixture
    server = PatientFhir()
    server.jwks = world.server.jwks
    world.server = server
    world.transport = fhir.Transport(send=server.send)
    return world


def configure(clinic, **settings):
    snapshot = clinic.configuration.read()
    documents = digital_accounts.patched(snapshot.active.values, 'fhir-hospital', {'settings': settings})
    clinic.configuration.apply(snapshot, snapshot.desired.values, restart_required=False, actor='test',
                               accounts=ConfigurationDocument(documents))


def queue_with(clinic, patient):
    job = queue(clinic)
    if patient is not None:
        fax_patient.write(clinic.data, job, patient)
    return job


def account(clinic):
    return digital_accounts.digital_account(clinic.values(), 'fhir-hospital')


def fhir_key(clinic):
    return route_key('fhir', clinic.address['id'])


def everything_stored(engine):
    """Every row of every table, as text: patient details must appear in none of them."""
    text = []
    with engine.connect() as connection:
        for name in sa.inspect(connection).get_table_names():
            table = sa.Table(name, sa.MetaData(), autoload_with=connection)
            text.extend(repr(tuple(row)) for row in connection.execute(sa.select(table)))
    return '\n'.join(text)


# -- the details given with a fax ---------------------------------------------------------------------------------

def test_the_details_are_checked_without_repeating_them():
    assert fax_patient.parse(None, '', ' ', None, None) is None
    parsed = fax_patient.parse(' MRN-5550199 ', '2.16.840.1.113883.19.5', 'Zyxwvut', 'Quillon', '1961-07-23')
    assert parsed == PATIENT  # a bare OID becomes urn:oid:<oid>
    assert repr(parsed) == str(parsed) == 'Patient(<withheld>)'
    for fields, sentence in [
        ((None, None, 'Zyxwvut', 'Quillon', None),
         "Give the patient's medical record number with the other patient details."),
        (('MRN-5550199', None, None, None, '23/07/1961'),
         "Write the patient's birth date as a day, such as 1980-04-30."),
        (('MRN-5550199', None, None, None, '2999-01-01'),
         "The patient's birth date must be a past day, such as 1980-04-30."),
        (('MRN-5550199', 'not a system', None, None, None),
         "Write the medical record number's system as a web address or an OID, such as "
         'urn:oid:2.16.840.1.113883.19.5.'),
        (('MRN<5550199>', None, None, None, None),
         "The patient's medical record number is too long or has characters Faxbot cannot use."),
    ]:
        with pytest.raises(fax_patient.PatientError) as refused:
            fax_patient.parse(*fields, today=date(2026, 10, 8))
        assert str(refused.value) == sentence


def test_the_details_are_kept_once_beside_the_document_for_faxbot_alone(tmp_path):
    job = 'a' * 32
    path = fax_patient.write(tmp_path, job, PATIENT)
    assert path == tmp_path / f'{job}.patient.json'
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert fax_patient.read(tmp_path, job) == PATIENT
    with pytest.raises(FileExistsError):  # written once, never replaced
        fax_patient.write(tmp_path, job, fax_patient.Patient('MRN-OTHER'))
    assert fax_patient.read(tmp_path, 'b' * 32) is None and fax_patient.read(tmp_path, '../escape') is None
    (tmp_path / ('c' * 32 + '.patient.json')).write_text('{not json')
    with pytest.raises(fax_patient.PatientUnreadable):
        fax_patient.read(tmp_path, 'c' * 32)
    (tmp_path / ('d' * 32 + '.patient.json')).symlink_to(path)
    with pytest.raises(fax_patient.PatientUnreadable):
        fax_patient.read(tmp_path, 'd' * 32)
    fax_patient.remove(tmp_path, job)
    assert not path.exists()


# -- optional: the patient goes as its record number -----------------------------------------------------------

def test_us_core_category_and_type_go_with_every_document_and_no_patient_without_one(clinic):
    queue_with(clinic, None)
    asyncio.run(send(clinic))
    (_, resource), = clinic.server.posts
    assert 'subject' not in resource
    assert resource['category'] == [{'coding': [{
        'system': 'http://hl7.org/fhir/us/core/CodeSystem/us-core-documentreference-category',
        'code': 'clinical-note', 'display': 'Clinical Note'}]}]
    assert resource['type'] == {'coding': [{'system': 'http://terminology.hl7.org/CodeSystem/v3-NullFlavor',
                                            'code': 'UNK', 'display': 'unknown'}]}
    configure(clinic, document_type='34133-9')
    queue_with(clinic, None)
    asyncio.run(send(clinic))
    assert clinic.server.posts[1][1]['type'] == {'coding': [{'system': 'http://loinc.org', 'code': '34133-9'}]}


def test_a_patient_given_with_the_fax_goes_as_its_record_number_with_the_clients_system(clinic):
    configure(clinic, patient_record_system='2.16.840.1.113883.19.999')
    assert account(clinic).setting('patient_record_system') == 'urn:oid:2.16.840.1.113883.19.999'
    job = queue_with(clinic, fax_patient.Patient('MRN-5550199', None, 'Zyxwvut', 'Quillon'))
    conventional = asyncio.run(send(clinic))
    assert conventional.submissions == 0 and clinic.server.lookups == []
    (_, resource), = clinic.server.posts
    assert resource['subject'] == {'identifier': {'system': 'urn:oid:2.16.840.1.113883.19.999',
                                                  'value': 'MRN-5550199'}, 'display': 'Quillon Zyxwvut'}
    assert clinic.server.tokens[0][2] == 'system/DocumentReference.c'  # no patient lookup, no more access
    assert clinic.store.for_job(job)[0]['state'] == 'delivered'


# -- required: looked up by record number ------------------------------------------------------------------------

def test_a_server_that_needs_the_patient_gets_it_looked_up_by_record_number(clinic):
    configure(clinic, patient='required')
    job = queue_with(clinic, PATIENT)
    assert asyncio.run(send(clinic)).submissions == 0
    (lookup,) = clinic.server.lookups
    assert lookup.path == '/r4/Patient/_search' and 'MRN' not in lookup.url  # never in an address
    assert parse_qs(lookup.body.decode()) == {'identifier': [f'{ISSUER}|MRN-5550199']}
    assert clinic.server.tokens[0][2] == 'system/DocumentReference.c system/Patient.rs'
    (_, resource), = clinic.server.posts
    assert resource['subject'] == {'reference': 'Patient/pat-123', 'display': 'Quillon Zyxwvut'}
    assert clinic.store.for_job(job)[0]['state'] == 'delivered'


def test_an_older_server_is_asked_for_patient_access_in_its_own_form(clinic):
    configure(clinic, patient='required', scope='system/DocumentReference.write')
    queue_with(clinic, PATIENT)
    asyncio.run(send(clinic))
    assert clinic.server.tokens[0][2] == 'system/DocumentReference.write system/Patient.read'


@pytest.mark.parametrize('case, sentence', [
    ('unknown', 'The FHIR server did not find exactly one patient for these details; nothing was sent.'),
    ('forbidden', 'The FHIR server did not let Faxbot look up the patient; nothing was sent. Let the FHIR client '
                  'read patients on that server.'),
])
def test_a_patient_the_server_does_not_find_sends_nothing_and_the_fax_goes_by_fax(clinic, case, sentence):
    configure(clinic, patient='required')
    if case == 'forbidden':
        clinic.server.lookup_status = 403
    job = queue_with(clinic, PATIENT if case == 'forbidden' else fax_patient.Patient('MRN-5550000', ISSUER))
    conventional = asyncio.run(send(clinic))
    assert conventional.submissions == 1 and clinic.server.posts == []
    (message,) = clinic.store.for_job(job)
    assert message['state'] == 'refused' and message['detail'] == sentence


@pytest.mark.parametrize('mode, patient', [
    ('required', None),
    ('matched', None),
    ('matched', fax_patient.Patient('MRN-5550199', ISSUER, 'Zyxwvut', 'Quillon')),  # no birth date
])
def test_a_fax_without_what_the_server_needs_skips_the_route_and_goes_by_fax(clinic, mode, patient):
    configure(clinic, patient=mode)
    job = queue_with(clinic, patient)
    values = clinic.values()
    found, skipped = candidates(clinic.engine, values, DEST, 2, job_id=job)
    assert found == [] and skipped == [(fhir_key(clinic), 'needs_patient')]
    # A preview (no fax yet) still offers the route.
    assert [item.key for item in candidates(clinic.engine, values, DEST, 2)[0]] == [fhir_key(clinic)]
    conventional = asyncio.run(send(clinic))
    assert conventional.submissions == 1
    assert clinic.server.tokens == [] and clinic.server.posts == [] and clinic.store.for_job(job) == []


def test_the_skipped_route_is_said_in_one_sentence():
    key = 'fhir:' + 'e' * 32
    choice = {'skipped': json.dumps({'skipped': [{'account': key, 'why': 'needs_patient'}]}), 'place': 1}
    sentence = route_view.attempt_sentence('Phaxio', choice, None, None, {'phase': 'success'},
                                           label=lambda _: 'Synthetic Hospital (FHIR)')
    assert ("Synthetic Hospital (FHIR) was skipped: its server needs the patient's details, which this fax does "
            'not have.') in sentence
    assert holds.no_route_sentence(lambda _: 'Synthetic Hospital (FHIR)', [(key, 'needs_patient')]) == (
        "No account your rules allow can send this fax now: Synthetic Hospital (FHIR) needs the patient's details, "
        'which this fax does not have. It waits for you in Sent; nothing was sent.')


def test_the_sender_refuses_a_fax_without_the_patient_if_the_setting_changed_after_planning(clinic):
    from api.app.digital.fhir import FhirSender
    configure(clinic, patient='required')
    sender = FhirSender(clinic.store, account(clinic), transport=clinic.transport)
    claim = SimpleNamespace(job_id='f' * 32, attempt_id='0' * 32)
    with pytest.raises(DirectRefused, match="needs the patient's medical record number, and this fax has none"):
        sender.prepare(claim=claim, job={'pages': 1}, view={'id': clinic.address['id'], 'address': 'x'},
                       document=b'%PDF-1.4', values=clinic.values())
    assert clinic.store.for_job('f' * 32) == []


# -- matched: the server confirms one patient --------------------------------------------------------------------

def test_a_server_that_matches_patients_confirms_one_from_number_name_and_birth_date(clinic):
    configure(clinic, patient='matched')
    job = queue_with(clinic, PATIENT)
    assert asyncio.run(send(clinic)).submissions == 0
    (lookup,) = clinic.server.lookups
    assert lookup.path == '/r4/Patient/$match' and lookup.headers['Content-Type'] == fhir.FHIR_JSON
    assert json.loads(lookup.body) == {'resourceType': 'Parameters', 'parameter': [
        {'name': 'resource', 'resource': {
            'resourceType': 'Patient', 'identifier': [{'system': ISSUER, 'value': 'MRN-5550199'}],
            'name': [{'family': 'Zyxwvut', 'given': ['Quillon']}], 'birthDate': '1961-07-23'}},
        {'name': 'onlyCertainMatches', 'valueBoolean': True}]}
    assert clinic.server.posts[0][1]['subject']['reference'] == 'Patient/pat-123'
    assert clinic.store.for_job(job)[0]['state'] == 'delivered'


def test_a_match_that_is_not_certain_sends_nothing_and_the_fax_goes_by_fax(clinic):
    configure(clinic, patient='matched')
    clinic.server.grade = 'possible'
    job = queue_with(clinic, PATIENT)
    assert asyncio.run(send(clinic)).submissions == 1 and clinic.server.posts == []
    assert clinic.store.for_job(job)[0]['detail'] == (
        'The FHIR server could not confirm one patient for these details; nothing was sent.')


# -- never written anywhere but beside the document --------------------------------------------------------------

@pytest.mark.parametrize('mode, refusal', [
    ('optional', 'document'), ('required', 'document'), ('matched', 'document'),
    ('required', 'lookup'), ('matched', 'lookup'),
])
def test_patient_details_are_never_logged_or_stored(clinic, caplog, mode, refusal):
    """The server's refusal (of the document, or of the patient lookup) repeats the patient, as real servers' text
    may; nothing Faxbot logs or stores repeats it."""
    caplog.set_level(logging.DEBUG)
    configure(clinic, patient=mode)
    if refusal == 'document':
        clinic.server.echo = True
    else:
        clinic.server.lookup_status = 403
    job = queue_with(clinic, PATIENT)
    assert asyncio.run(send(clinic)).submissions == 1
    (message,) = clinic.store.for_job(job)
    assert message['state'] == 'refused'
    if refusal == 'document':
        assert message['detail'] == 'The FHIR server refused the document (422).'
        assert clinic.server.posts[0][1]['subject']  # the document itself did carry the patient
    stored = everything_stored(clinic.engine)
    logged = caplog.text + ''.join(repr(record.args) for record in caplog.records)
    for value in SECRETS:
        assert value not in stored and value not in logged
