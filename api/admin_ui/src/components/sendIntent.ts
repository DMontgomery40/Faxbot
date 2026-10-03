// The send in progress, kept in this tab's session storage so a reload during
// an upload does not lose its Idempotency-Key: sending the same document to
// the same number again reuses the key, and the server answers with the fax it
// already accepted instead of sending a second one.
//
// Only a fingerprint of the number and document is stored, never the number or
// file name themselves. If two different sends ever shared a fingerprint, the
// server refuses the reused key for the different fax, so nothing is sent twice.

const STORAGE_KEY = 'faxbot_pending_send';
export const PENDING_SEND_LIFETIME_MS = 24 * 60 * 60 * 1000;

export interface PendingSend {
  key: string;
  fingerprint: string;
  queueOnly: boolean;
  maxFileSizeBytes: number;
  createdAt: number;
}

// A small non-cryptographic hash (cyrb53), enough to recognize the same send.
function hash(text: string): string {
  let h1 = 0xdeadbeef;
  let h2 = 0x41c6ce57;
  for (let index = 0; index < text.length; index += 1) {
    const code = text.charCodeAt(index);
    h1 = Math.imul(h1 ^ code, 2654435761);
    h2 = Math.imul(h2 ^ code, 1597334677);
  }
  h1 = Math.imul(h1 ^ (h1 >>> 16), 2246822507) ^ Math.imul(h2 ^ (h2 >>> 13), 3266489909);
  h2 = Math.imul(h2 ^ (h2 >>> 16), 2246822507) ^ Math.imul(h1 ^ (h1 >>> 13), 3266489909);
  return (4294967296 * (2097151 & h2) + (h1 >>> 0)).toString(16);
}

export function sendFingerprint(destination: string, file: File): string {
  return hash([destination, file.name, file.size, file.lastModified, file.type].join('\u0000'));
}

function storage(): Storage | null {
  try {
    return window.sessionStorage;
  } catch {
    return null;
  }
}

export function clearPendingSend(): void {
  try { storage()?.removeItem(STORAGE_KEY); } catch { /* storage unavailable */ }
}

export function savePendingSend(pending: PendingSend): void {
  try { storage()?.setItem(STORAGE_KEY, JSON.stringify(pending)); } catch { /* storage unavailable or full */ }
}

// The stored send for this fingerprint, if it is less than a day old.
export function loadPendingSend(fingerprint: string, now = Date.now()): PendingSend | null {
  let pending: Partial<PendingSend> | null = null;
  try {
    const text = storage()?.getItem(STORAGE_KEY);
    pending = text ? JSON.parse(text) : null;
  } catch {
    return null;
  }
  if (!pending || typeof pending.key !== 'string' || typeof pending.fingerprint !== 'string'
      || typeof pending.queueOnly !== 'boolean' || !Number.isSafeInteger(pending.maxFileSizeBytes)
      || !Number.isFinite(pending.createdAt)) {
    return null;
  }
  if (now - (pending.createdAt as number) > PENDING_SEND_LIFETIME_MS) {
    clearPendingSend();
    return null;
  }
  return pending.fingerprint === fingerprint ? pending as PendingSend : null;
}
