// Invoice record addresses share the shell's hash routing, including its old alias.
const pages = new Set(['#/savings/invoices', '#/costs/invoices']);

export function invoiceAt(address: string): string | null {
  const [page, query = ''] = address.split('?');
  return pages.has(page) ? new URLSearchParams(query).get('invoice') || null : null;
}

export function invoiceAddress(id: string | null, address = '#/savings/invoices'): string {
  const [page, query = ''] = address.split('?');
  const params = new URLSearchParams(pages.has(page) ? query : '');
  if (id) params.set('invoice', id);
  else params.delete('invoice');
  const suffix = params.toString();
  return `#/savings/invoices${suffix ? `?${suffix}` : ''}`;
}
