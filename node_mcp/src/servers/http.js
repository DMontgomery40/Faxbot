#!/usr/bin/env node
// Faxbot MCP over Streamable HTTP at /mcp (stateless: 2026-07-28 clients and
// initialize-handshake clients are both served per request).
//
// Every request must carry the caller's own Faxbot API key, as
// "Authorization: Bearer <key>" or "X-API-Key: <key>". With OAuth configured
// (OAUTH_ISSUER and OAUTH_AUDIENCE), the Bearer token is a JWT and its subject
// is mapped to a Faxbot key from MCP_OAUTH_SUBJECT_KEYS_FILE. The key is
// forwarded to Faxbot as X-API-Key. API_KEY is never used by this server.
import { createServer } from 'node:http';
import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
import dotenv from 'dotenv';
import { createRemoteJWKSet, jwtVerify } from 'jose';
import { OAuthError, bearerAuthChallengeResponse, createMcpHandler, verifyBearerToken } from '@modelcontextprotocol/server';
import { hostHeaderValidation, toNodeHandler } from '@modelcontextprotocol/node';
import { SERVER_INFO, buildServer } from '../tools/fax-tools.js';

const MAX_BODY = 16 * 1024 * 1024; // a 10 MB fax is about 13.4 MB as base64 JSON

const list = (value) => String(value || '').split(',').map((item) => item.trim()).filter(Boolean);

function sendJson(res, status, body, headers = {}) {
  res.writeHead(status, { 'Content-Type': 'application/json', ...headers });
  res.end(JSON.stringify(body));
}

function jwtVerifier({ issuer, audience, jwksUrl }) {
  const jwks = createRemoteJWKSet(new URL(jwksUrl || `${issuer}/.well-known/jwks.json`));
  return {
    async verifyAccessToken(token) {
      try {
        const { payload } = await jwtVerify(token, jwks, { issuer, audience });
        return {
          token,
          clientId: String(payload.client_id || payload.azp || payload.sub || ''),
          scopes: String(payload.scope || '').split(' ').filter(Boolean),
          expiresAt: payload.exp,
          extra: { subject: payload.sub },
        };
      } catch (err) {
        throw new OAuthError('invalid_token', err?.message || 'Invalid token');
      }
    },
  };
}

export function createHttpServer({
  baseUrl = process.env.FAX_API_URL || 'http://localhost:8080',
  allowedHosts = list(process.env.MCP_ALLOWED_HOSTS),
  allowedOrigins = list(process.env.MCP_ALLOWED_ORIGINS),
  oauthIssuer = (process.env.OAUTH_ISSUER || '').replace(/\/$/, ''),
  oauthAudience = process.env.OAUTH_AUDIENCE || '',
  oauthJwksUrl = process.env.OAUTH_JWKS_URL || '',
  subjectKeysFile = process.env.MCP_OAUTH_SUBJECT_KEYS_FILE || '',
  resourceUrl = process.env.MCP_RESOURCE_URL || '',
} = {}) {
  const oauth = Boolean(oauthIssuer && oauthAudience);
  const verifier = oauth ? jwtVerifier({ issuer: oauthIssuer, audience: oauthAudience, jwksUrl: oauthJwksUrl }) : null;
  const metadataUrl = oauth && resourceUrl ? `${resourceUrl.replace(/\/$/, '')}/.well-known/oauth-protected-resource` : '';
  const validateHost = allowedHosts.length ? hostHeaderValidation(allowedHosts.map((host) => host.replace(/:(\*|\d+)$/, ''))) : null;
  const handler = createMcpHandler(
    (ctx) => buildServer({ baseUrl, apiKey: ctx.authInfo?.extra?.faxbotApiKey || '', localFiles: false }),
    { maxRequestBodySize: MAX_BODY },
  );
  const serveMcp = toNodeHandler(handler, { maxRequestBodySize: MAX_BODY });

  // Resolve the caller's Faxbot key, or answer the request and return ''.
  async function callerKey(req, res) {
    const authorization = String(req.headers.authorization || '');
    if (!oauth) {
      const key = (/^bearer /i.test(authorization) ? authorization.slice(7).trim() : '') || String(req.headers['x-api-key'] || '').trim();
      if (!key) sendJson(res, 401, { error: 'Unauthorized' }, { 'WWW-Authenticate': 'Bearer' });
      return key;
    }
    let authInfo;
    try {
      authInfo = await verifyBearerToken(authorization, { verifier, resourceMetadataUrl: metadataUrl || undefined });
    } catch (err) {
      const challenge = bearerAuthChallengeResponse(err, { resourceMetadataUrl: metadataUrl || undefined });
      res.writeHead(challenge.status, Object.fromEntries(challenge.headers));
      res.end(await challenge.text());
      return '';
    }
    const keys = subjectKeysFile ? JSON.parse(readFileSync(subjectKeysFile, 'utf8')) : {};
    const key = typeof keys[authInfo.extra.subject] === 'string' ? keys[authInfo.extra.subject] : '';
    if (!key) sendJson(res, 403, { error: 'No Faxbot API key is assigned to this account.' });
    return key;
  }

  return createServer(async (req, res) => {
    try {
      const { pathname } = new URL(req.url || '/', 'http://localhost');
      if (pathname === '/health') return sendJson(res, 200, { status: 'ok', transport: 'streamable-http', server: SERVER_INFO.name, version: SERVER_INFO.version });
      if (metadataUrl && pathname === '/.well-known/oauth-protected-resource') {
        return sendJson(res, 200, { resource: resourceUrl, authorization_servers: [oauthIssuer], bearer_methods_supported: ['header'] });
      }
      if (pathname !== '/mcp') return sendJson(res, 404, { error: 'Not found' });
      if (validateHost && !validateHost(req, res)) return;
      const origin = req.headers.origin;
      if (origin && !allowedOrigins.includes(origin)) return sendJson(res, 403, { error: 'Origin not allowed' });
      const key = await callerKey(req, res);
      if (!key) return;
      req.auth = { token: key, clientId: 'faxbot-api-key', scopes: [], extra: { faxbotApiKey: key } };
      await serveMcp(req, res);
    } catch (err) {
      console.error('MCP HTTP error:', err);
      if (!res.headersSent) sendJson(res, 500, { error: 'Internal server error' });
    }
  });
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  dotenv.config({ quiet: true });
  const port = parseInt(process.env.MCP_HTTP_PORT || '3001', 10);
  createHttpServer().listen(port, () => {
    console.log(`Faxbot MCP Streamable HTTP on http://localhost:${port}/mcp`);
  });
}
