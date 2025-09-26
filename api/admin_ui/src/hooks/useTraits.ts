import { useState, useEffect, useCallback } from 'react';
import type { ProvidersInfo } from '../api/types';

interface TraitsHook {
  loading: boolean;
  error: string | null;
  active: { outbound: string; inbound: string } | null;
  registry: Record<string, { id?: string; kind?: string; traits?: Record<string, any> }> | null;
  hasTrait: (direction: 'outbound' | 'inbound', key: string) => boolean;
  traitValue: (direction: 'outbound' | 'inbound', key: string) => any;
  getWebhookUrl: (direction: 'inbound' | 'outbound') => string | null;
  getSamplePayload: (direction: 'inbound' | 'outbound') => string;
  getProviderHeaders: (direction: 'inbound' | 'outbound', secret?: string) => string[];
  refresh: () => Promise<void>;
  outboundTraits: Record<string, any> | null;
  inboundTraits: Record<string, any> | null;
}

// Global cache to avoid multiple API calls
let globalProvidersCache: ProvidersInfo | null = null;
let globalCachePromise: Promise<ProvidersInfo> | null = null;

export function useTraits(): TraitsHook {
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [providers, setProviders] = useState<ProvidersInfo | null>(globalProvidersCache);

  const fetchProviders = useCallback(async (): Promise<ProvidersInfo> => {
    // Return existing promise if one is in flight
    if (globalCachePromise) {
      return globalCachePromise;
    }

    // Return cached data if available
    if (globalProvidersCache) {
      return globalProvidersCache;
    }

    // Create new fetch promise
    globalCachePromise = (async () => {
      try {
        const response = await fetch('/admin/providers', {
          headers: {
            'X-API-Key': localStorage.getItem('faxbot_admin_key') || '',
            'Content-Type': 'application/json',
          },
        });

        if (!response.ok) {
          throw new Error(`Failed to fetch providers: ${response.status}`);
        }

        const data = await response.json();
        globalProvidersCache = data;
        return data;
      } catch (err) {
        globalCachePromise = null; // Clear promise on error so we can retry
        throw err;
      } finally {
        globalCachePromise = null; // Clear promise when done
      }
    })();

    return globalCachePromise;
  }, []);

  const refresh = useCallback(async () => {
    try {
      setError(null);
      setLoading(true);
      
      // Clear cache to force fresh fetch
      globalProvidersCache = null;
      globalCachePromise = null;
      
      const data = await fetchProviders();
      setProviders(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to fetch provider traits');
    } finally {
      setLoading(false);
    }
  }, [fetchProviders]);

  // Initial load
  useEffect(() => {
    const load = async () => {
      try {
        setError(null);
        const data = await fetchProviders();
        setProviders(data);
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Failed to fetch provider traits');
      } finally {
        setLoading(false);
      }
    };

    load();
  }, [fetchProviders]);

  // Dot-path getter
  const dotGet = (obj: any, path: string) => {
    try {
      return path.split('.').reduce((acc: any, k: string) => (acc && acc[k] !== undefined ? acc[k] : undefined), obj);
    } catch {
      return undefined;
    }
  };

  const hasTrait = useCallback((direction: 'outbound' | 'inbound', key: string): boolean => {
    if (!providers?.active || !providers?.registry) return false;
    const activeProvider = providers.active[direction];
    if (!activeProvider) return false;
    const providerInfo = providers.registry[activeProvider];
    if (!providerInfo?.traits) return false;
    return dotGet(providerInfo.traits, key) !== undefined;
  }, [providers]);

  const traitValue = useCallback((direction: 'outbound' | 'inbound', key: string): any => {
    if (!providers?.active || !providers?.registry) return undefined;
    const activeProvider = providers.active[direction];
    if (!activeProvider) return undefined;
    const providerInfo = providers.registry[activeProvider];
    if (!providerInfo?.traits) return undefined;
    return dotGet(providerInfo.traits, key);
  }, [providers]);

  const getWebhookUrl = useCallback((direction: 'inbound' | 'outbound'): string | null => {
    if (!providers?.active) return null;
    const providerId = providers.active[direction];
    if (!providerId) return null;
    
    const baseUrl = window.location.origin;
    const webhookPath = traitValue(direction, 'webhook.path') || traitValue(direction, 'webhook_path');
    if (webhookPath) {
      return `${baseUrl}${webhookPath}`;
    }
    
    // Fallback for known providers (should be moved to traits)
    const webhookPaths: Record<string, string> = {
      phaxio: '/phaxio-inbound',
      sinch: '/sinch-inbound', 
      signalwire: '/signalwire-callback',
      documo: '/documo-callback',
    };
    
    const path = webhookPaths[providerId];
    return path ? `${baseUrl}${path}` : null;
  }, [providers, traitValue]);

  const getSamplePayload = useCallback((direction: 'inbound' | 'outbound'): string => {
    if (!providers?.active) return '{}';
    const providerId = providers.active[direction];
    if (!providerId) return '{}';
    
    const samplePayload = traitValue(direction, 'sample_payload');
    if (samplePayload) {
      return JSON.stringify(samplePayload, null, 2);
    }
    
    // Fallback samples (should be moved to traits)
    const samples: Record<string, any> = {
      phaxio: {
        direction: 'received',
        fax: {
          id: 12345,
          num_pages: 3,
          cost: 7,
          direction: 'received',
          status: 'success',
          is_test: false,
          requested_at: new Date().toISOString(),
          completed_at: new Date().toISOString(),
          recipients: [{ number: '+15551234567', status: 'success' }],
          caller_id: '+15559876543',
          from_number: '+15559876543',
          to_number: '+15551234567'
        }
      },
      sinch: {
        eventType: 'INBOUND_FAX_COMPLETED',
        faxId: 'fax_01234567',
        to: '+15551234567',
        from: '+15559876543',
        status: 'COMPLETED',
        timestamp: new Date().toISOString()
      },
      signalwire: {
        FaxSid: 'FX12345',
        From: '+15559876543',
        To: '+15551234567',
        Status: 'received',
        NumPages: '3'
      }
    };
    
    const sample = samples[providerId];
    return sample ? JSON.stringify(sample, null, 2) : '{}';
  }, [providers, traitValue]);

  const getProviderHeaders = useCallback((direction: 'inbound' | 'outbound', secret?: string): string[] => {
    if (!providers?.active) return [];
    const providerId = providers.active[direction];
    if (!providerId) return [];
    
    const headers: string[] = ['-H "Content-Type: application/json"'];
    const verification = traitValue(direction, 'webhook.verification') as string | undefined;
    const verifyHeader = (traitValue(direction, 'webhook.verify_header') || traitValue(direction, 'verify_header') || traitValue(direction, 'signature_header')) as string | undefined;

    if (verification === 'basic_auth') {
      // Placeholder Basic auth; operator should replace with real creds
      headers.push('-H "Authorization: Basic <base64(user:pass)>"');
    } else if (verification === 'hmac_sha256') {
      if (verifyHeader && secret) headers.push(`-H "${verifyHeader}: <calculated_hmac>"`);
    } else if (verifyHeader && secret) {
      headers.push(`-H "${verifyHeader}: <signature>"`);
    } else {
      // Fallback for known providers (should be removed once traits are consistent)
      const fallback: Record<string, string> = { phaxio: 'X-Phaxio-Signature', sinch: 'Authorization' };
      const hdr = fallback[providerId];
      if (hdr && (verification === 'basic_auth')) headers.push('-H "Authorization: Basic <base64(user:pass)>"');
      else if (hdr && secret) headers.push(`-H "${hdr}: <signature>"`);
    }
    
    return headers;
  }, [providers, traitValue]);

  const outboundId = providers?.active?.['outbound'] as string | undefined;
  const inboundId = providers?.active?.['inbound'] as string | undefined;

  return {
    loading,
    error,
    active: providers?.active || null,
    registry: providers?.registry || null,
    hasTrait,
    traitValue,
    getWebhookUrl,
    getSamplePayload,
    getProviderHeaders,
    refresh,
    outboundTraits: outboundId && providers?.registry?.[outboundId]?.traits || null,
    inboundTraits: inboundId && providers?.registry?.[inboundId]?.traits || null,
  };
}

// Re-export provider utilities for convenience
export { getProviderDisplayName, getProviderIcon, getProviderColor, getProviderCategory } from '../utils/providerIcons';
