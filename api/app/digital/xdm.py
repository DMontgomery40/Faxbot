"""IHE XDM packages for Direct messages (IHE ITI TF Vol2 ITI-32; ONC XDR and XDM for Direct Messaging).

An XDM package is a zip: ``README.TXT`` and ``INDEX.HTM`` at the root, and
``IHE_XDM/SUBSET01/`` holding ``METADATA.XML`` (an ebRIM 3.0
SubmitObjectsRequest) and the documents. Direct sends exactly one submission
set. Faxbot sends the XDS "limited metadata" a sender without a patient
context can give: for the submission set, its unique and source ids, the
submission time, the intended recipient and the author's Direct address; for
each document, its MIME type, unique id, SHA-1 hash, size and file name
(``URI``), with the format code "MIME type is sufficient"
(``urn:ihe:iti:xds:2017:mimeTypeSufficient``).

``read`` opens a received package and returns its documents, by the
metadata's ``URI`` slots when they can be read, else every file in the
subset folders except the metadata. Paths are checked, so a package cannot
name a file outside itself, and sizes are bounded.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import io
import posixpath
import uuid
import xml.etree.ElementTree as ET
import zipfile


RIM = 'urn:oasis:names:tc:ebxml-regrep:xsd:rim:3.0'
LCM = 'urn:oasis:names:tc:ebxml-regrep:xsd:lcm:3.0'
SUBSET = 'IHE_XDM/SUBSET01'
DOCUMENT_ENTRY = 'urn:uuid:7edca82f-054d-47f2-a032-9b2a5b5186c1'
SUBMISSION_SET = 'urn:uuid:a54d6aa5-d40d-43f9-88c5-b4633d873bdd'
SUBMISSION_SET_LIMITED = 'urn:uuid:5003a9db-8d8d-49e6-bf0c-990e34ac7707'
DOCUMENT_LIMITED = 'urn:uuid:ab9b591b-83ab-4d03-8f5d-f93b1fb92e85'
FORMAT_CODE_SCHEME = 'urn:uuid:a09d5840-386c-46f2-b5ad-9c3699a4309d'
SUBMISSION_UNIQUE_ID = 'urn:uuid:96fdda7c-d067-4183-912e-bf5ee74998a8'
SUBMISSION_SOURCE_ID = 'urn:uuid:554ac39e-e3fe-47fe-b233-965d2a147832'
DOCUMENT_UNIQUE_ID = 'urn:uuid:2e82c1f6-a085-4c72-9da3-8640a32e42ab'
AUTHOR_SCHEME = 'urn:uuid:a7058bb9-b4e4-4307-ba5b-e3f0ab85e12d'
HAS_MEMBER = 'urn:oasis:names:tc:ebxml-regrep:AssociationType:HasMember'
MAX_ENTRIES = 50
MAX_TOTAL = 64 * 1024 * 1024


class XdmError(ValueError):
    """The package could not be read; one plain sentence."""


@dataclass(frozen=True)
class Document:
    name: str
    media_type: str
    data: bytes


def _oid(seed=None):
    value = uuid.uuid5(uuid.NAMESPACE_URL, seed) if seed else uuid.uuid4()
    return f'2.25.{value.int}'


def _slot(parent, name, *values):
    slot = ET.SubElement(parent, f'{{{RIM}}}Slot', name=name)
    holder = ET.SubElement(slot, f'{{{RIM}}}ValueList')
    for value in values:
        ET.SubElement(holder, f'{{{RIM}}}Value').text = value
    return slot


def _name(parent, text):
    name = ET.SubElement(parent, f'{{{RIM}}}Name')
    ET.SubElement(name, f'{{{RIM}}}LocalizedString', value=text)


def _identifier(parent, scheme, object_id, value, label):
    found = ET.SubElement(parent, f'{{{RIM}}}ExternalIdentifier', id=f'urn:uuid:{uuid.uuid4()}',
                          identificationScheme=scheme, registryObject=object_id, value=value)
    _name(found, label)


def metadata(documents, *, sender, recipient, organization, now=None):
    """METADATA.XML for one submission set holding ``documents`` (name, media type, bytes)."""
    now = now or datetime.now(timezone.utc)
    stamp = now.strftime('%Y%m%d%H%M%S')
    ET.register_namespace('lcm', LCM)
    ET.register_namespace('rim', RIM)
    request = ET.Element(f'{{{LCM}}}SubmitObjectsRequest')
    objects = ET.SubElement(request, f'{{{RIM}}}RegistryObjectList')
    package_id = f'urn:uuid:{uuid.uuid4()}'
    for index, (name, media_type, data) in enumerate(documents, start=1):
        entry_id = f'urn:uuid:{uuid.uuid4()}'
        entry = ET.SubElement(objects, f'{{{RIM}}}ExtrinsicObject', id=entry_id, mimeType=media_type,
                              objectType=DOCUMENT_ENTRY, status='urn:oasis:names:tc:ebxml-regrep:StatusType:Approved')
        _slot(entry, 'creationTime', stamp)
        _slot(entry, 'hash', hashlib.sha1(data).hexdigest())  # noqa: S324 - XDS metadata names SHA-1
        _slot(entry, 'size', str(len(data)))
        _slot(entry, 'URI', name)
        _slot(entry, 'languageCode', 'en-US')
        _name(entry, f'Document {index} from {organization}')
        ET.SubElement(entry, f'{{{RIM}}}Classification', id=f'urn:uuid:{uuid.uuid4()}',
                      classificationNode=DOCUMENT_LIMITED, classifiedObject=entry_id)
        code = ET.SubElement(entry, f'{{{RIM}}}Classification', id=f'urn:uuid:{uuid.uuid4()}',
                             classificationScheme=FORMAT_CODE_SCHEME, classifiedObject=entry_id,
                             nodeRepresentation='urn:ihe:iti:xds:2017:mimeTypeSufficient')
        _slot(code, 'codingScheme', '1.3.6.1.4.1.19376.1.2.3')
        _name(code, 'MIME type sufficient')
        _identifier(entry, DOCUMENT_UNIQUE_ID, entry_id, _oid(), 'XDSDocumentEntry.uniqueId')
        association = ET.SubElement(objects, f'{{{RIM}}}Association', id=f'urn:uuid:{uuid.uuid4()}',
                                    associationType=HAS_MEMBER, sourceObject=package_id, targetObject=entry_id)
        _slot(association, 'SubmissionSetStatus', 'Original')
    package = ET.SubElement(objects, f'{{{RIM}}}RegistryPackage', id=package_id)
    _slot(package, 'submissionTime', stamp)
    _slot(package, 'intendedRecipient', f'^^Internet^{recipient}')
    author = ET.SubElement(package, f'{{{RIM}}}Classification', id=f'urn:uuid:{uuid.uuid4()}',
                           classificationScheme=AUTHOR_SCHEME, classifiedObject=package_id, nodeRepresentation='')
    _slot(author, 'authorInstitution', organization)
    _slot(author, 'authorTelecommunication', f'^^Internet^{sender}')
    _name(package, f'Faxbot submission from {organization}')
    ET.SubElement(objects, f'{{{RIM}}}Classification', id=f'urn:uuid:{uuid.uuid4()}',
                  classificationNode=SUBMISSION_SET, classifiedObject=package_id)
    ET.SubElement(objects, f'{{{RIM}}}Classification', id=f'urn:uuid:{uuid.uuid4()}',
                  classificationNode=SUBMISSION_SET_LIMITED, classifiedObject=package_id)
    _identifier(package, SUBMISSION_UNIQUE_ID, package_id, _oid(), 'XDSSubmissionSet.uniqueId')
    _identifier(package, SUBMISSION_SOURCE_ID, package_id, _oid(f'faxbot:{sender}'), 'XDSSubmissionSet.sourceId')
    return ET.tostring(request, encoding='utf-8', xml_declaration=True)


def build(documents, *, sender, recipient, organization, now=None):
    """The XDM zip for ``documents`` (name, media type, bytes); names are DOC0001.PDF style."""
    named = []
    for index, (original, media_type, data) in enumerate(documents, start=1):
        extension = posixpath.splitext(original)[1].upper() or '.BIN'
        named.append((f'DOC{index:04d}{extension}', media_type, data))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('README.TXT', (
            f'This package was sent by Faxbot for {organization} ({sender}) to {recipient}.\r\n'
            'It follows IHE XDM: the documents and their metadata are in IHE_XDM/SUBSET01.\r\n'))
        links = ''.join(f'<li><a href="{SUBSET}/{name}">{name}</a></li>' for name, _, _ in named)
        archive.writestr('INDEX.HTM', f'<html><head><title>Documents from {organization}</title></head><body>'
                                      f'<h1>Documents from {organization}</h1><ul>{links}</ul></body></html>')
        archive.writestr(f'{SUBSET}/METADATA.XML', metadata(named, sender=sender, recipient=recipient,
                                                            organization=organization, now=now))
        for name, _, data in named:
            archive.writestr(f'{SUBSET}/{name}', data)
    return buffer.getvalue()


def _safe(name):
    clean = posixpath.normpath(name.replace('\\', '/'))
    return not (clean.startswith('/') or clean.startswith('..') or '/../' in f'/{clean}/')


def read(data):
    """The documents in an XDM zip, as ``Document`` (name, media type, bytes). Raises XdmError."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise XdmError('The package is not a zip file.') from None
    entries = [item for item in archive.infolist() if not item.is_dir()]
    if len(entries) > MAX_ENTRIES or sum(item.file_size for item in entries) > MAX_TOTAL:
        raise XdmError('The package is too large.')
    if any(not _safe(item.filename) for item in entries):
        raise XdmError('The package names a file outside itself.')
    names = {item.filename.upper(): item.filename for item in entries}
    found = []
    for path_upper, path in sorted(names.items()):
        if not path_upper.startswith('IHE_XDM/') or not path_upper.endswith('/METADATA.XML'):
            continue
        folder = path.rsplit('/', 1)[0]
        listed = _listed(archive.read(path))
        for uri, media_type in listed:
            target = f'{folder}/{uri}'.upper()
            if target in names and _safe(uri):
                found.append(Document(posixpath.basename(names[target]), media_type, archive.read(names[target])))
    if found:
        return found
    for path_upper, path in sorted(names.items()):
        if path_upper.startswith('IHE_XDM/') and not path_upper.endswith('METADATA.XML'):
            found.append(Document(posixpath.basename(path), _guess(path), archive.read(path)))
    if not found:
        raise XdmError('The package holds no documents.')
    return found


def _listed(xml_bytes):
    """[(URI, media type)] from METADATA.XML; [] when it cannot be read."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []
    listed = []
    for entry in root.iter(f'{{{RIM}}}ExtrinsicObject'):
        uri = None
        for slot in entry.findall(f'{{{RIM}}}Slot'):
            if slot.get('name') == 'URI':
                values = [value.text or '' for value in slot.iter(f'{{{RIM}}}Value')]
                uri = ''.join(values).strip() or None
        if uri:
            listed.append((uri, (entry.get('mimeType') or _guess(uri)).lower()))
    return listed


def _guess(name):
    lower = name.lower()
    if lower.endswith('.pdf'):
        return 'application/pdf'
    if lower.endswith('.xml'):
        return 'text/xml'
    if lower.endswith(('.tif', '.tiff')):
        return 'image/tiff'
    return 'application/octet-stream'
