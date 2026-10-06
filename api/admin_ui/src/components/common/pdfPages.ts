// How many pages a PDF has, read from its page objects before it is sent, so a cost
// estimate can be for this document. Null when the file is not a PDF or its page
// objects are compressed out of sight; the estimate then gives the price per unit.
export async function countPdfPages(file: File): Promise<number | null> {
  if (file.type !== 'application/pdf' && !/\.pdf$/i.test(file.name)) return null;
  try {
    const text = new TextDecoder('latin1').decode(await readBytes(file));
    const count = (text.match(/\/Type\s*\/Page(?![A-Za-z])/g) ?? []).length;
    return count > 0 ? count : null;
  } catch {
    return null;
  }
}

function readBytes(file: File): Promise<ArrayBuffer> {
  if (typeof file.arrayBuffer === 'function') return file.arrayBuffer();
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result as ArrayBuffer);
    reader.onerror = () => reject(reader.error);
    reader.readAsArrayBuffer(file);
  });
}
