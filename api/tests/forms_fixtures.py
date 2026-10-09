"""Synthetic registered forms for the forms tests: an SVG template with every field type, and a fillable PDF."""
import io
import json

from api.app.forms import model
from api.app.forms.raster import Bitmap


SVG = b'''<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="8.5in" height="11in" viewBox="0 0 612 792">
  <title>Synthetic referral</title>
  <text x="72" y="60" font-size="18">Synthetic referral form</text>
  <rect x="72" y="80" width="468" height="2" fill="black"/>
  <text x="72" y="118" font-size="10">Patient</text>
  <rect x="160" y="100" width="300" height="24" fill="none" stroke="black" stroke-width="1"/>
  <text x="72" y="158" font-size="10">Born</text>
  <rect x="160" y="140" width="140" height="24" fill="none" stroke="black"/>
  <text x="72" y="198" font-size="10">Urgent</text>
  <rect x="160" y="182" width="18" height="18" fill="none" stroke="black"/>
  <text x="72" y="238" font-size="10">Clinic</text>
  <rect x="160" y="220" width="200" height="24" fill="none" stroke="black"/>
  <text x="72" y="278" font-size="10">Amount</text>
  <rect x="160" y="260" width="140" height="24" fill="none" stroke="black"/>
  <text x="72" y="318" font-size="10">Notes</text>
  <rect x="160" y="300" width="380" height="80" fill="#dddddd" stroke="black"/>
  <path d="M 160 470 L 460 470" stroke="black" stroke-width="1" fill="none"/>
  <text x="72" y="468" font-size="10">Signature</text>
  <circle cx="520" cy="60" r="14" fill="none" stroke="black" stroke-width="2"/>
</svg>
'''

POSITIONS = {'fields': [
    {'name': 'patient', 'label': 'Patient name', 'type': 'text', 'x': 160, 'y': 100, 'width': 300, 'height': 24,
     'required': True, 'font_size': 11},
    {'name': 'born', 'label': 'Date of birth', 'type': 'date', 'x': 160, 'y': 140, 'width': 140, 'height': 24},
    {'name': 'urgent', 'label': 'Urgent', 'type': 'checkbox', 'x': 160, 'y': 182, 'width': 18, 'height': 18},
    {'name': 'clinic', 'label': 'Clinic', 'type': 'choice', 'x': 160, 'y': 220, 'width': 200, 'height': 24,
     'options': ['North', 'South']},
    {'name': 'amount', 'label': 'Amount', 'type': 'number', 'x': 160, 'y': 260, 'width': 140, 'height': 24,
     'decimals': 2},
    {'name': 'notes', 'label': 'Notes', 'type': 'text', 'x': 160, 'y': 300, 'width': 380, 'height': 80,
     'multiline': True},
    {'name': 'signature', 'label': 'Signature', 'type': 'signature', 'x': 160, 'y': 420, 'width': 300,
     'height': 48},
]}


def signature():
    """A synthetic scribble, already bilevel, as a signature value."""
    picture = Bitmap(90, 30)
    for x in range(90):
        y = 15 + ((x * 7) % 13) - 6
        picture.rectangle(x, y, 1, 3)
    return model.signature_value(90, 30, picture.packed())


VALUES = {'patient': 'Ána Müller-Øster', 'born': '1980-02-29', 'urgent': True, 'clinic': 'South',
          'amount': '1,234.5', 'notes': 'Synthetic note that is long enough to wrap onto a second line.\nNo PHI.',
          'signature': signature()}


def positions_json():
    return json.dumps(POSITIONS).encode('ascii')


def fillable_pdf(*, prefilled='PREFILLED'):
    """A fillable PDF with text, checkbox, choice, radio, date, number and signature fields."""
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import DictionaryObject, NameObject, NumberObject, TextStringObject, ArrayObject, FloatObject
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=letter, invariant=1)
    pdf.setFont('Helvetica-Bold', 16)
    pdf.drawString(72, 720, 'Synthetic fillable form')
    pdf.setFont('Helvetica', 11)
    pdf.drawString(72, 680, 'Patient')
    pdf.acroForm.textfield(name='patient', x=180, y=672, width=300, height=20, value=prefilled, fontSize=11)
    pdf.drawString(72, 640, 'Urgent')
    pdf.acroForm.checkbox(name='urgent', x=180, y=636, size=16)
    pdf.drawString(72, 600, 'Clinic')
    pdf.acroForm.choice(name='clinic', x=180, y=594, width=200, height=20, options=['North', 'South'], value='North')
    pdf.drawString(72, 560, 'Visit')
    pdf.acroForm.radio(name='visit', value='new', x=180, y=556, size=14, selected=False)
    pdf.acroForm.radio(name='visit', value='followup', x=260, y=556, size=14, selected=False)
    pdf.drawString(72, 520, 'Born')
    pdf.acroForm.textfield(name='born', x=180, y=512, width=120, height=20, fontSize=10)
    pdf.drawString(72, 480, 'Amount')
    pdf.acroForm.textfield(name='amount', x=180, y=472, width=120, height=20, fontSize=10)
    pdf.showPage()
    pdf.save()
    # Date and number formats live in the fields' format scripts; add them as Acrobat does.
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(buffer.getvalue())))
    page = writer.pages[0]
    for reference in page['/Annots']:
        annotation = reference.get_object()
        name = str(annotation.get('/T', ''))
        script = {'born': 'AFDate_FormatEx("dd/mm/yyyy");', 'amount': 'AFNumber_Format(2, 0, 0, 0, "", true);'}.get(name)
        if script:
            annotation[NameObject('/AA')] = DictionaryObject({NameObject('/F'): DictionaryObject({
                NameObject('/S'): NameObject('/JavaScript'), NameObject('/JS'): TextStringObject(script)})})
    # A signature field, as a signing tool adds one.
    signature_field = DictionaryObject({
        NameObject('/Type'): NameObject('/Annot'), NameObject('/Subtype'): NameObject('/Widget'),
        NameObject('/FT'): NameObject('/Sig'), NameObject('/T'): TextStringObject('signed'),
        NameObject('/Rect'): ArrayObject([FloatObject(180), FloatObject(400), FloatObject(400), FloatObject(440)]),
        NameObject('/F'): NumberObject(4)})
    page['/Annots'].append(writer._add_object(signature_field))
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()
