// Recipient addresses preserve international '+' signs through URLSearchParams.
export function recipientAddress(number: string): string {
  return `#/recipients/list?${new URLSearchParams({ recipient: number }).toString()}`;
}

export function recipientFromAddress(hash: string): string | null {
  const [path, query = ''] = hash.split('?');
  return path === '#/recipients/list' ? new URLSearchParams(query).get('recipient') || null : null;
}
