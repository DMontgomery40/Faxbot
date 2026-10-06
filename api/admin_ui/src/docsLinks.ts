export const DEFAULT_DOCS_BASE = 'https://docs.faxbot.net/latest/';

const DOCS_PAGES = {
  home: '',
  providers: 'setup/',
  phaxio: 'setup/phaxio/',
  sinch: 'setup/sinch/',
  documo: 'setup/documo/',
  humblefax: 'setup/humblefax/',
  efax: 'setup/efax/',
  signalwire: 'setup/signalwire/',
  freeswitch: 'setup/freeswitch/',
  sip: 'setup/sip-asterisk/',
  inbound: 'inbound/',
  deployment: 'deployment/',
  security: 'security/',
  scripts: 'tools/scripts-and-tests/',
  storage: 'admin-console/settings/#available-controls',
} as const;

type DocsPage = keyof typeof DOCS_PAGES;

function docsBaseUrl(configuredBase?: string): URL {
  let base: URL;
  try {
    base = new URL(configuredBase?.trim() || DEFAULT_DOCS_BASE);
    if (base.protocol !== 'https:' && base.protocol !== 'http:') {
      return new URL(DEFAULT_DOCS_BASE);
    }
  } catch {
    return new URL(DEFAULT_DOCS_BASE);
  }
  if (base.origin === 'https://docs.faxbot.net' && base.pathname === '/') {
    base.pathname = '/latest/';
  }
  base.pathname = `${base.pathname.replace(/\/+$/, '')}/`;
  return base;
}

export function docsLink(page: DocsPage, configuredBase?: string): string {
  // Relative routes retain an operator's directory or exact version prefix.
  return new URL(DOCS_PAGES[page], docsBaseUrl(configuredBase)).href;
}

const CURATED_PATHS: Record<string, DocsPage> = {
  'setup/phaxio': 'phaxio',
  'setup/sinch': 'sinch',
  'setup/documo': 'documo',
  'setup/humblefax': 'humblefax',
  'setup/efax': 'efax',
  'setup/signalwire': 'signalwire',
  'setup/freeswitch': 'freeswitch',
  'setup/sip-asterisk': 'sip',
  'admin-console/settings#available-controls': 'storage',
  'providers/phaxio': 'phaxio',
  'providers/sinch': 'sinch',
  'providers/asterisk': 'sip',
  'storage/local': 'storage',
  'storage/s3': 'storage',
  'backends/phaxio-setup.html': 'phaxio',
  'backends/sinch-setup.html': 'sinch',
  'backends/signalwire-setup.html': 'signalwire',
  'backends/freeswitch-setup.html': 'freeswitch',
  'backends/sip-setup.html': 'sip',
};

export function curatedDocsLink(link: string | undefined, configuredBase?: string): string | undefined {
  if (!link) return link;
  let url: URL;
  try {
    url = new URL(link);
  } catch {
    return link;
  }
  let path: string;
  if (url.origin === 'https://docs.faxbot.net') {
    path = url.pathname.replace(/^\/(?:latest\/)?/, '');
  } else if (url.origin === 'https://dmontgomery40.github.io' && url.pathname.startsWith('/Faxbot/')) {
    path = url.pathname.slice('/Faxbot/'.length);
  } else {
    return link;
  }
  // Only known curated links are rebased; unknown/versioned/vendor URLs stay intact.
  const key = `${path.replace(/\/+$/, '')}${url.hash}`;
  const page = Object.prototype.hasOwnProperty.call(CURATED_PATHS, key) ? CURATED_PATHS[key] : undefined;
  return page && !url.search ? docsLink(page, configuredBase) : link;
}
