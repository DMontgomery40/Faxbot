"""The patient a fax is about, for FHIR routes only: the medical record number with the system it belongs to, and
the name and birth date a recipient may need to confirm the patient.

Why: US Core 9.0.0 (STU 9) makes ``DocumentReference.subject`` required (1..1, a Reference to a US Core Patient),
and its "Writing Clinical Notes" page says clients SHALL supply ``status``, ``type``, ``category``, ``subject``,
``content`` and ``content.attachment.contentType`` (hl7.org/fhir/us/core, read 2026-10-08). A fax carries no
patient, so the sender gives one with the fax (``POST /fax``, ``faxbot send``, Send a fax). A FHIR client says how
its server takes a patient (``digital/accounts.py``, ``patient``): ``optional`` adds the patient when the fax has
one; ``required`` needs the medical record number and looks the patient up by it; ``matched`` also needs the name
and birth date, and asks the server's ``Patient/$match`` (FHIR R4) for one certain match. The B2B minimum the
Interoperable Digital Identity and Patient Matching IG (2.0.0, STU 2) names is first and last name, birth date
and an enterprise identifier, which is what ``matched`` needs.

The details are document content. They are kept in one write-once file beside the fax's document
(``<job>.patient.json`` next to ``<job>.pdf`` in the data folder), so they have the document's access, its
backups and its lifetime; no database row, audit entry, log line, fingerprint or sentence holds them (the
request fingerprint folds them into its one-way hash only). A ``Patient`` withholds its values from ``repr``, and
every error names the field, never what was typed.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import json
import os
from pathlib import Path
import re

from .accounts import PATIENT_MODES


SUFFIX = '.patient.json'
MODES = PATIENT_MODES          # optional | required | matched
MAX_NAME = 100
MAX_NUMBER = 64
MAX_SYSTEM = 255
_JOB = re.compile(r'[a-f0-9]{32}')
_OID = re.compile(r'[0-2](\.(0|[1-9][0-9]*))+')
_SYSTEM = re.compile(r'(urn:oid:[0-2](\.(0|[1-9][0-9]*))+|urn:uuid:[0-9a-fA-F-]{36}|https?://[^\s|]{3,250})')


class PatientError(ValueError):
    """The patient details given with a fax cannot be used; one sentence that names the field, never its value."""


class PatientUnreadable(RuntimeError):
    """The fax's patient file exists but cannot be read."""


@dataclass(frozen=True, repr=False)
class Patient:
    record_number: str
    record_system: str | None = None
    family_name: str | None = None
    given_name: str | None = None
    birth_date: str | None = None          # YYYY-MM-DD

    def __repr__(self):
        return 'Patient(<withheld>)'

    __str__ = __repr__

    def as_dict(self):
        return {'record_number': self.record_number, 'record_system': self.record_system,
                'family_name': self.family_name, 'given_name': self.given_name, 'birth_date': self.birth_date}

    def canonical(self):
        """The details as one canonical text, only for the request fingerprint's one-way hash."""
        return json.dumps(self.as_dict(), sort_keys=True, separators=(',', ':'), ensure_ascii=True)

    def missing_for(self, mode):
        """Whether a server taking patients in ``mode`` needs more than these details."""
        if mode == 'matched':
            return not (self.record_number and self.family_name and self.given_name and self.birth_date)
        return not self.record_number

    def values(self):
        """Every value, for tests that prove none of them leaks."""
        return tuple(value for value in self.as_dict().values() if value)


def mode_of(account):
    """How the FHIR client's server takes a patient: one of ``MODES`` (``optional`` when not set)."""
    value = account.setting('patient') if account is not None else None
    return value if value in MODES else 'optional'


def normalized_system(text):
    """The record number's system as a URI: a bare OID becomes ``urn:oid:<oid>``. Raises PatientError."""
    text = (text or '').strip()
    if not text:
        return None
    if _OID.fullmatch(text):
        text = 'urn:oid:' + text
    if len(text) > MAX_SYSTEM or not _SYSTEM.fullmatch(text):
        raise PatientError("Write the medical record number's system as a web address or an OID, such as "
                           'urn:oid:2.16.840.1.113883.19.5.')
    return text


def _text(value, limit, label):
    text = ' '.join((value or '').split())
    if not text:
        return None
    if len(text) > limit or any(ord(char) < 32 or char in '<>{}|' for char in text):
        raise PatientError(f"The patient's {label} is too long or has characters Faxbot cannot use.")
    return text


def parse(record_number=None, record_system=None, family_name=None, given_name=None, birth_date=None, *,
          today=None):
    """The patient given with a fax, or None when no field was given. Raises PatientError (one sentence)."""
    if not any((value or '').strip() for value in (record_number, record_system, family_name, given_name,
                                                    birth_date)):
        return None
    number = _text(record_number, MAX_NUMBER, 'medical record number')
    if number is None:
        raise PatientError("Give the patient's medical record number with the other patient details.")
    day = (birth_date or '').strip() or None
    if day is not None:
        try:
            born = date.fromisoformat(day)
        except ValueError:
            raise PatientError("Write the patient's birth date as a day, such as 1980-04-30.") from None
        if len(day) != 10 or born > (today or date.today()) or born.year < 1880:
            raise PatientError("The patient's birth date must be a past day, such as 1980-04-30.")
        day = born.isoformat()
    return Patient(number, normalized_system(record_system), _text(family_name, MAX_NAME, 'family name'),
                   _text(given_name, MAX_NAME, 'given name'), day)


def path_for(data_dir, job_id):
    if not _JOB.fullmatch(str(job_id or '')):
        raise ValueError('Unknown fax.')
    return Path(data_dir) / (job_id + SUFFIX)


def write(data_dir, job_id, patient):
    """Keep the details beside the fax's document, once (never replaced) and readable only by Faxbot."""
    target = path_for(data_dir, job_id)
    body = json.dumps(patient.as_dict(), separators=(',', ':')).encode('utf-8')
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        os.write(descriptor, body)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return target


def remove(data_dir, job_id):
    """Remove the details of a fax that was not accepted (its document is removed with them)."""
    try:
        path_for(data_dir, job_id).unlink()
    except (FileNotFoundError, ValueError):
        pass


def read(data_dir, job_id):
    """The fax's patient, or None when it has none. Raises PatientUnreadable."""
    try:
        target = path_for(data_dir, job_id)
    except ValueError:
        return None
    if target.is_symlink():
        raise PatientUnreadable()
    try:
        raw = target.read_bytes()
    except FileNotFoundError:
        return None
    except OSError:
        raise PatientUnreadable() from None
    try:
        found = json.loads(raw)
        return Patient(**{name: found.get(name) for name in ('record_number', 'record_system', 'family_name',
                                                              'given_name', 'birth_date')})
    except (ValueError, TypeError, AttributeError):
        raise PatientUnreadable() from None
