// The SDK versions the API & SDKs quickstart installs. A test keeps these equal to the
// packages' own metadata (sdks/node/package.json and sdks/python/setup.py), which the
// console build cannot read: it is built from api/admin_ui alone.
export const SDK_VERSIONS = { node: '1.1.0', python: '1.1.0' } as const;
