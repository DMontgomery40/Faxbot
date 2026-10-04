// The Setup Wizard's one-page test fax: a small PDF made here, so a fax
// machine shows a large "Faxbot test page" heading, when it was sent and
// through which provider. It carries no personal data.

export interface TestPageDetails {
  sentAt: string;
  // The installation's name, when one is set.
  installation?: string | null;
  provider: string;
  destination: string;
}

// PDF text strings here are plain ASCII; anything else becomes '?'.
function pdfText(value: string): string {
  return value.replace(/[^\x20-\x7e]/g, '?').replace(/[\\()]/g, (character) => `\\${character}`);
}

export function testPageLines(details: TestPageDetails): string[] {
  return [
    `Sent ${details.sentAt}`,
    ...(details.installation?.trim() ? [`From ${details.installation.trim()}`] : []),
    `Sent through ${details.provider} to ${details.destination}`,
    'This page checks that sending works. No reply is needed.',
  ];
}

// A complete one-page US Letter PDF: a 44 pt heading and 16 pt lines.
export function testPagePdf(details: TestPageDetails): string {
  const lines = testPageLines(details);
  const content = [
    'BT', '/F2 44 Tf', '72 640 Td', `(${pdfText('Faxbot test page')}) Tj`, 'ET',
    'BT', '/F1 16 Tf', '72 580 Td', '22 TL',
    ...lines.flatMap((line, index) => (index === 0 ? [`(${pdfText(line)}) Tj`] : ['T*', `(${pdfText(line)}) Tj`])),
    'ET',
    // A frame, so the page reads as deliberate on paper.
    '3 w', '54 54 504 684 re', 'S',
  ].join('\n');
  const objects = [
    '<< /Type /Catalog /Pages 2 0 R >>',
    '<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
    '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R /F2 6 0 R >> >> >>',
    `<< /Length ${content.length} >>\nstream\n${content}\nendstream`,
    '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
    '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>',
  ];
  let pdf = '%PDF-1.4\n';
  const offsets: number[] = [];
  objects.forEach((body, index) => {
    offsets.push(pdf.length);
    pdf += `${index + 1} 0 obj\n${body}\nendobj\n`;
  });
  const xref = pdf.length;
  pdf += `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n`;
  pdf += offsets.map((offset) => `${String(offset).padStart(10, '0')} 00000 n \n`).join('');
  pdf += `trailer\n<< /Size ${objects.length + 1} /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`;
  return pdf;
}
