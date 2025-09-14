# Faxbot Frontend Phases 4-8 - Complete Detailed Implementation

This contains the full detailed implementations for Phases 4-8 that should be part of the main frontend_plan.md

## Phase 4: Admin Interface (API Key Management) - COMPLETE IMPLEMENTATION

**Duration:** 1 week  
**Goal:** Complete API key lifecycle management with security best practices

### Step 4.1: Create API Key Modal - FULL IMPLEMENTATION

**Complete Create API Key Modal** (`src/components/features/CreateApiKeyModal.tsx`):
```typescript
import React, { useState } from 'react';
import { Dialog, Transition } from '@headlessui/react';
import { Fragment } from 'react';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { z } from 'zod';
import { useMutation } from '@tanstack/react-query';
import toast from 'react-hot-toast';
import { useAuthStore } from '../../store/authStore';
import { CreateApiKeyRequest, CreateApiKeyResponse } from '../../lib/types';
import { XMarkIcon, ClipboardIcon } from '@heroicons/react/24/outline';

const createKeySchema = z.object({
  name: z.string().min(1, 'Name is required').max(100, 'Name too long'),
  owner: z.string().max(100, 'Owner too long').optional(),
  scopes: z.array(z.string()).min(1, 'At least one scope is required'),
  expires_at: z.string().optional(),
  note: z.string().max(500, 'Note too long').optional(),
});

type CreateKeyForm = z.infer<typeof createKeySchema>;

interface CreateApiKeyModalProps {
  isOpen: boolean;
  onClose: () => void;
  onSuccess: () => void;
}

const availableScopes = [
  { value: 'fax:send', label: 'Send Faxes', description: 'Can send fax documents' },
  { value: 'fax:read', label: 'Read Fax Status', description: 'Can check fax job status' },
  { value: 'inbound:list', label: 'List Inbound', description: 'Can list received faxes' },
  { value: 'inbound:read', label: 'Read Inbound', description: 'Can view and download received faxes' },
  { value: 'keys:manage', label: 'Manage Keys', description: 'Can create, revoke, and rotate API keys (admin)' },
];

const CreateApiKeyModal: React.FC<CreateApiKeyModalProps> = ({
  isOpen,
  onClose,
  onSuccess,
}) => {
  const apiClient = useAuthStore((state) => state.apiClient);
  const [createdKey, setCreatedKey] = useState<CreateApiKeyResponse | null>(null);
  const [showToken, setShowToken] = useState(false);

  const {
    register,
    handleSubmit,
    formState: { errors },
    watch,
    setValue,
    reset,
  } = useForm<CreateKeyForm>({
    resolver: zodResolver(createKeySchema),
    defaultValues: {
      scopes: ['fax:send', 'fax:read'],
    },
  });

  const selectedScopes = watch('scopes') || [];

  const createKeyMutation = useMutation({
    mutationFn: async (data: CreateKeyForm) => {
      if (!apiClient) throw new Error('Not authenticated');
      
      const payload: CreateApiKeyRequest = {
        name: data.name,
        owner: data.owner || undefined,
        scopes: data.scopes,
        expires_at: data.expires_at || undefined,
        note: data.note || undefined,
      };
      
      return apiClient.createApiKey(payload);
    },
    onSuccess: (data) => {
      setCreatedKey(data);
      setShowToken(true);
      toast.success('API key created successfully!');
    },
    onError: (error: any) => {
      console.error('Create key error:', error);
      if (error.response?.status === 401) {
        toast.error('Admin access required');
      } else {
        toast.error('Failed to create API key');
      }
    },
  });

  const handleScopeChange = (scope: string, checked: boolean) => {
    const current = selectedScopes;
    if (checked) {
      setValue('scopes', [...current, scope]);
    } else {
      setValue('scopes', current.filter(s => s !== scope));
    }
  };

  const copyToClipboard = async () => {
    if (createdKey?.token) {
      try {
        await navigator.clipboard.writeText(createdKey.token);
        toast.success('Token copied to clipboard');
      } catch (error) {
        toast.error('Failed to copy token');
      }
    }
  };

  const handleClose = () => {
    if (createdKey) {
      onSuccess();
      setCreatedKey(null);
      setShowToken(false);
      reset();
    }
    onClose();
  };

  const onSubmit = (data: CreateKeyForm) => {
    createKeyMutation.mutate(data);
  };

  return (
    <Transition appear show={isOpen} as={Fragment}>
      <Dialog as="div" className="relative z-10" onClose={handleClose}>
        <Transition.Child
          as={Fragment}
          enter="ease-out duration-300"
          enterFrom="opacity-0"
          enterTo="opacity-100"
          leave="ease-in duration-200"
          leaveFrom="opacity-100"
          leaveTo="opacity-0"
        >
          <div className="fixed inset-0 bg-black bg-opacity-25" />
        </Transition.Child>

        <div className="fixed inset-0 overflow-y-auto">
          <div className="flex min-h-full items-center justify-center p-4 text-center">
            <Transition.Child
              as={Fragment}
              enter="ease-out duration-300"
              enterFrom="opacity-0 scale-95"
              enterTo="opacity-100 scale-100"
              leave="ease-in duration-200"
              leaveFrom="opacity-100 scale-100"
              leaveTo="opacity-0 scale-95"
            >
              <Dialog.Panel className="w-full max-w-2xl transform overflow-hidden rounded-2xl bg-white text-left align-middle shadow-xl transition-all">
                <div className="flex justify-between items-center px-6 py-4 border-b border-gray-200">
                  <Dialog.Title as="h3" className="text-lg font-medium text-gray-900">
                    {showToken ? 'API Key Created' : 'Create API Key'}
                  </Dialog.Title>
                  <button
                    onClick={handleClose}
                    className="text-gray-400 hover:text-gray-600"
                  >
                    <XMarkIcon className="h-6 w-6" />
                  </button>
                </div>

                {showToken && createdKey ? (
                  // Show created token
                  <div className="px-6 py-6">
                    <div className="bg-green-50 border border-green-200 rounded-lg p-4 mb-6">
                      <h4 className="text-sm font-medium text-green-800 mb-2">
                        ✅ API Key Created Successfully
                      </h4>
                      <p className="text-sm text-green-700">
                        Save this token now - you won't be able to see it again!
                      </p>
                    </div>

                    <div className="space-y-4">
                      <div>
                        <label className="block text-sm font-medium text-gray-700">
                          Key ID
                        </label>
                        <p className="mt-1 text-sm text-gray-900 font-mono">
                          {createdKey.key_id}
                        </p>
                      </div>

                      <div>
                        <label className="block text-sm font-medium text-gray-700">
                          API Token (save this now!)
                        </label>
                        <div className="mt-1 flex">
                          <input
                            type="text"
                            value={createdKey.token}
                            readOnly
                            className="flex-1 font-mono text-sm border-gray-300 rounded-l-md focus:ring-primary-500 focus:border-primary-500"
                          />
                          <button
                            onClick={copyToClipboard}
                            className="inline-flex items-center px-3 py-2 border border-l-0 border-gray-300 rounded-r-md bg-gray-50 text-gray-500 hover:bg-gray-100"
                          >
                            <ClipboardIcon className="h-4 w-4" />
                          </button>
                        </div>
                      </div>

                      <div>
                        <label className="block text-sm font-medium text-gray-700">
                          Scopes
                        </label>
                        <div className="mt-1 flex flex-wrap gap-2">
                          {createdKey.scopes.map((scope) => (
                            <span
                              key={scope}
                              className="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-primary-100 text-primary-800"
                            >
                              {scope}
                            </span>
                          ))}
                        </div>
                      </div>
                    </div>

                    <div className="mt-6 flex justify-end">
                      <button onClick={handleClose} className="btn-primary">
                        Done
                      </button>
                    </div>
                  </div>
                ) : (
                  // Create key form
                  <form onSubmit={handleSubmit(onSubmit)} className="px-6 py-6">
                    <div className="space-y-6">
                      {/* Basic Info */}
                      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                        <div>
                          <label className="block text-sm font-medium text-gray-700">
                            Name *
                          </label>
                          <input
                            {...register('name')}
                            type="text"
                            className="input mt-1"
                            placeholder="Development Key"
                          />
                          {errors.name && (
                            <p className="mt-1 text-sm text-red-600">{errors.name.message}</p>
                          )}
                        </div>

                        <div>
                          <label className="block text-sm font-medium text-gray-700">
                            Owner
                          </label>
                          <input
                            {...register('owner')}
                            type="text"
                            className="input mt-1"
                            placeholder="john@example.com"
                          />
                          {errors.owner && (
                            <p className="mt-1 text-sm text-red-600">{errors.owner.message}</p>
                          )}
                        </div>
                      </div>

                      {/* Scopes */}
                      <div>
                        <label className="block text-sm font-medium text-gray-700 mb-3">
                          Permissions *
                        </label>
                        <div className="space-y-3">
                          {availableScopes.map((scope) => (
                            <div key={scope.value} className="flex items-start">
                              <div className="flex items-center h-5">
                                <input
                                  type="checkbox"
                                  checked={selectedScopes.includes(scope.value)}
                                  onChange={(e) => handleScopeChange(scope.value, e.target.checked)}
                                  className="focus:ring-primary-500 h-4 w-4 text-primary-600 border-gray-300 rounded"
                                />
                              </div>
                              <div className="ml-3 text-sm">
                                <label className="font-medium text-gray-700">
                                  {scope.label}
                                </label>
                                <p className="text-gray-500">{scope.description}</p>
                              </div>
                            </div>
                          ))}
                        </div>
                        {errors.scopes && (
                          <p className="mt-1 text-sm text-red-600">{errors.scopes.message}</p>
                        )}
                      </div>

                      {/* Expiration */}
                      <div>
                        <label className="block text-sm font-medium text-gray-700">
                          Expiration (optional)
                        </label>
                        <input
                          {...register('expires_at')}
                          type="datetime-local"
                          className="input mt-1"
                        />
                        <p className="mt-1 text-xs text-gray-500">
                          Leave empty for no expiration
                        </p>
                      </div>

                      {/* Note */}
                      <div>
                        <label className="block text-sm font-medium text-gray-700">
                          Note (optional)
                        </label>
                        <textarea
                          {...register('note')}
                          rows={3}
                          className="input mt-1"
                          placeholder="Purpose or additional information about this key"
                        />
                        {errors.note && (
                          <p className="mt-1 text-sm text-red-600">{errors.note.message}</p>
                        )}
                      </div>
                    </div>

                    <div className="mt-6 flex justify-end space-x-3">
                      <button
                        type="button"
                        onClick={onClose}
                        className="btn-secondary"
                      >
                        Cancel
                      </button>
                      <button
                        type="submit"
                        disabled={createKeyMutation.isPending}
                        className="btn-primary"
                      >
                        {createKeyMutation.isPending ? 'Creating...' : 'Create Key'}
                      </button>
                    </div>
                  </form>
                )}
              </Dialog.Panel>
            </Transition.Child>
          </div>
        </div>
      </Dialog>
    </Transition>
  );
};

export default CreateApiKeyModal;
```

### Step 4.2: API Keys List Component - FULL IMPLEMENTATION

**Complete API Keys List** (`src/components/features/ApiKeysList.tsx`):
```typescript
import React from 'react';
import { ApiKeyMeta } from '../../lib/types';
import { 
  TrashIcon, 
  ArrowPathIcon, 
  ClockIcon,
  CheckCircleIcon,
  XCircleIcon 
} from '@heroicons/react/24/outline';

interface ApiKeysListProps {
  apiKeys: ApiKeyMeta[];
  isLoading: boolean;
  onRevoke: (keyId: string) => void;
  onRotate: (keyId: string) => void;
  isRevoking: boolean;
  isRotating: boolean;
}

const ApiKeysList: React.FC<ApiKeysListProps> = ({
  apiKeys,
  isLoading,
  onRevoke,
  onRotate,
  isRevoking,
  isRotating,
}) => {
  const formatDate = (dateString?: string) => {
    if (!dateString) return 'Never';
    return new Date(dateString).toLocaleDateString('en-US', {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    });
  };

  const getKeyStatus = (key: ApiKeyMeta) => {
    if (key.revoked_at) {
      return { status: 'revoked', color: 'text-red-600', icon: XCircleIcon };
    }
    if (key.expires_at && new Date(key.expires_at) < new Date()) {
      return { status: 'expired', color: 'text-red-600', icon: ClockIcon };
    }
    return { status: 'active', color: 'text-green-600', icon: CheckCircleIcon };
  };

  if (isLoading) {
    return (
      <div className="p-6">
        <div className="animate-pulse space-y-4">
          {[1, 2, 3].map((i) => (
            <div key={i} className="flex items-center space-x-4">
              <div className="w-12 h-12 bg-gray-200 rounded"></div>
              <div className="flex-1 space-y-2">
                <div className="h-4 bg-gray-200 rounded w-3/4"></div>
                <div className="h-3 bg-gray-200 rounded w-1/2"></div>
              </div>
              <div className="w-20 h-8 bg-gray-200 rounded"></div>
            </div>
          ))}
        </div>
      </div>
    );
  }

  if (apiKeys.length === 0) {
    return (
      <div className="p-6 text-center">
        <div className="max-w-md mx-auto">
          <svg className="mx-auto h-12 w-12 text-gray-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 7a2 2 0 012 2m0 0a2 2 0 012 2m-2-2h-6m6 0v6a2 2 0 01-2 2H9a2 2 0 01-2-2V9a2 2 0 012-2h2m0 0V7a2 2 0 012-2m0 0a2 2 0 012 2" />
          </svg>
          <h3 className="mt-2 text-sm font-medium text-gray-900">No API keys</h3>
          <p className="mt-1 text-sm text-gray-500">
            Create your first API key to get started.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="overflow-hidden">
      <table className="min-w-full divide-y divide-gray-200">
        <thead className="bg-gray-50">
          <tr>
            <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
              Key Details
            </th>
            <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
              Scopes
            </th>
            <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
              Status
            </th>
            <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
              Last Used
            </th>
            <th className="px-6 py-3 text-right text-xs font-medium text-gray-500 uppercase tracking-wider">
              Actions
            </th>
          </tr>
        </thead>
        <tbody className="bg-white divide-y divide-gray-200">
          {apiKeys.map((key) => {
            const status = getKeyStatus(key);
            const StatusIcon = status.icon;
            
            return (
              <tr key={key.key_id} className="hover:bg-gray-50">
                <td className="px-6 py-4 whitespace-nowrap">
                  <div>
                    <div className="text-sm font-medium text-gray-900">
                      {key.name || 'Unnamed Key'}
                    </div>
                    <div className="text-sm text-gray-500">
                      ID: {key.key_id}
                    </div>
                    {key.owner && (
                      <div className="text-sm text-gray-500">
                        Owner: {key.owner}
                      </div>
                    )}
                  </div>
                </td>
                
                <td className="px-6 py-4">
                  <div className="flex flex-wrap gap-1">
                    {key.scopes.map((scope) => (
                      <span
                        key={scope}
                        className="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-gray-100 text-gray-800"
                      >
                        {scope}
                      </span>
                    ))}
                  </div>
                </td>
                
                <td className="px-6 py-4 whitespace-nowrap">
                  <div className="flex items-center">
                    <StatusIcon className={`h-4 w-4 mr-2 ${status.color}`} />
                    <span className={`text-sm capitalize ${status.color}`}>
                      {status.status}
                    </span>
                  </div>
                  {key.expires_at && (
                    <div className="text-xs text-gray-500">
                      Expires: {formatDate(key.expires_at)}
                    </div>
                  )}
                </td>
                
                <td className="px-6 py-4 whitespace-nowrap text-sm text-gray-500">
                  {formatDate(key.last_used_at)}
                </td>
                
                <td className="px-6 py-4 whitespace-nowrap text-right text-sm font-medium">
                  {!key.revoked_at && (
                    <div className="flex items-center justify-end space-x-2">
                      <button
                        onClick={() => onRotate(key.key_id)}
                        disabled={isRotating}
                        className="text-primary-600 hover:text-primary-900"
                        title="Rotate key (generate new secret)"
                      >
                        <ArrowPathIcon className="h-4 w-4" />
                      </button>
                      <button
                        onClick={() => onRevoke(key.key_id)}
                        disabled={isRevoking}
                        className="text-red-600 hover:text-red-900"
                        title="Revoke key"
                      >
                        <TrashIcon className="h-4 w-4" />
                      </button>
                    </div>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
};

export default ApiKeysList;
```

### Step 4.3: Complete Admin Page Implementation

**Full Admin Page** (`src/pages/admin/AdminPage.tsx`):
```typescript
import React, { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useAuthStore } from '../../store/authStore';
import toast from 'react-hot-toast';
import CreateApiKeyModal from '../../components/features/CreateApiKeyModal';
import ApiKeysList from '../../components/features/ApiKeysList';
import { PlusIcon, ExclamationTriangleIcon } from '@heroicons/react/24/outline';

const AdminPage: React.FC = () => {
  const apiClient = useAuthStore((state) => state.apiClient);
  const queryClient = useQueryClient();
  const [showCreateModal, setShowCreateModal] = useState(false);

  const { data: apiKeys, isLoading } = useQuery({
    queryKey: ['api-keys'],
    queryFn: async () => {
      if (!apiClient) throw new Error('Not authenticated');
      return apiClient.listApiKeys();
    },
    enabled: !!apiClient,
  });

  const revokeKeyMutation = useMutation({
    mutationFn: async (keyId: string) => {
      if (!apiClient) throw new Error('Not authenticated');
      return apiClient.revokeApiKey(keyId);
    },
    onSuccess: () => {
      toast.success('API key revoked successfully');
      queryClient.invalidateQueries({ queryKey: ['api-keys'] });
    },
    onError: (error: any) => {
      console.error('Revoke key error:', error);
      if (error.response?.status === 404) {
        toast.error('API key not found');
      } else {
        toast.error('Failed to revoke API key');
      }
    },
  });

  const rotateKeyMutation = useMutation({
    mutationFn: async (keyId: string) => {
      if (!apiClient) throw new Error('Not authenticated');
      return apiClient.rotateApiKey(keyId);
    },
    onSuccess: (data) => {
      toast.success('API key rotated successfully');
      
      // Show the new token securely
      const copyToClipboard = async () => {
        try {
          await navigator.clipboard.writeText(data.token);
          toast.success('New token copied to clipboard');
        } catch (error) {
          toast.error('Failed to copy token');
        }
      };
      
      const userWantsCopy = window.confirm(
        `New API token generated for key ${data.key_id}:\n\n${data.token}\n\nClick OK to copy to clipboard. This is the only time you'll see the full token!`
      );
      
      if (userWantsCopy) {
        copyToClipboard();
      }
      
      queryClient.invalidateQueries({ queryKey: ['api-keys'] });
    },
    onError: (error: any) => {
      console.error('Rotate key error:', error);
      if (error.response?.status === 404) {
        toast.error('API key not found');
      } else {
        toast.error('Failed to rotate API key');
      }
    },
  });

  const handleRevoke = (keyId: string) => {
    const key = apiKeys?.find(k => k.key_id === keyId);
    const keyName = key?.name || `Key ${keyId}`;
    
    if (window.confirm(`Are you sure you want to revoke "${keyName}"?\n\nThis action cannot be undone and will immediately disable the key.`)) {
      revokeKeyMutation.mutate(keyId);
    }
  };

  const handleRotate = (keyId: string) => {
    const key = apiKeys?.find(k => k.key_id === keyId);
    const keyName = key?.name || `Key ${keyId}`;
    
    if (window.confirm(`Are you sure you want to rotate "${keyName}"?\n\nThe old token will stop working immediately and a new one will be generated.`)) {
      rotateKeyMutation.mutate(keyId);
    }
  };

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex justify-between items-center">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">API Keys</h1>
          <p className="mt-1 text-sm text-gray-600">
            Manage API keys for accessing the Faxbot API
          </p>
        </div>
        <button
          onClick={() => setShowCreateModal(true)}
          className="btn-primary"
        >
          <PlusIcon className="h-5 w-5 mr-2" />
          Create API Key
        </button>
      </div>

      {/* Security Notice */}
      <div className="bg-yellow-50 border border-yellow-200 rounded-lg p-4">
        <div className="flex">
          <div className="flex-shrink-0">
            <ExclamationTriangleIcon className="h-5 w-5 text-yellow-400" />
          </div>
          <div className="ml-3">
            <h3 className="text-sm font-medium text-yellow-800">
              Security Best Practices
            </h3>
            <div className="mt-2 text-sm text-yellow-700">
              <ul className="list-disc pl-5 space-y-1">
                <li>API keys are only shown once when created or rotated</li>
                <li>Store keys securely and never share them</li>
                <li>Use specific scopes to limit key permissions</li>
                <li>Rotate keys regularly for production systems</li>
                <li>Revoke unused or compromised keys immediately</li>
              </ul>
            </div>
          </div>
        </div>
      </div>

      {/* API Keys Table */}
      <div className="bg-white shadow rounded-lg overflow-hidden">
        <div className="px-6 py-4 border-b border-gray-200">
          <div className="flex justify-between items-center">
            <h2 className="text-lg font-medium text-gray-900">
              Active API Keys ({apiKeys?.length || 0})
            </h2>
            {apiKeys && apiKeys.length > 0 && (
              <div className="text-sm text-gray-500">
                {apiKeys.filter(k => !k.revoked_at).length} active, {apiKeys.filter(k => k.revoked_at).length} revoked
              </div>
            )}
          </div>
        </div>
        
        <ApiKeysList
          apiKeys={apiKeys || []}
          isLoading={isLoading}
          onRevoke={handleRevoke}
          onRotate={handleRotate}
          isRevoking={revokeKeyMutation.isPending}
          isRotating={rotateKeyMutation.isPending}
        />
      </div>

      {/* Usage Guidelines */}
      <div className="bg-blue-50 border border-blue-200 rounded-lg p-4">
        <h3 className="text-sm font-medium text-blue-800 mb-2">
          Using API Keys
        </h3>
        <div className="text-sm text-blue-700 space-y-2">
          <p>
            <strong>In your application:</strong> Include the API key in the <code>X-API-Key</code> header
          </p>
          <p>
            <strong>With SDKs:</strong> Pass the key to the FaxbotClient constructor or set the <code>API_KEY</code> environment variable
          </p>
          <p>
            <strong>With MCP servers:</strong> Set the <code>API_KEY</code> environment variable for the MCP process
          </p>
        </div>
      </div>

      {/* Create API Key Modal */}
      <CreateApiKeyModal
        isOpen={showCreateModal}
        onClose={() => setShowCreateModal(false)}
        onSuccess={() => {
          setShowCreateModal(false);
          queryClient.invalidateQueries({ queryKey: ['api-keys'] });
        }}
      />
    </div>
  );
};

export default AdminPage;
```

## Phase 5: Inbound Fax Management - COMPLETE IMPLEMENTATION

**Duration:** 1 week  
**Goal:** List, view, and download inbound faxes with proper permissions

### Step 5.1: Complete Inbound Page

**Full Inbound Page** (`src/pages/inbound/InboundPage.tsx`):
```typescript
import React, { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useAuthStore } from '../../store/authStore';
import InboundFaxesList from '../../components/features/InboundFaxesList';
import InboundFaxModal from '../../components/features/InboundFaxModal';
import { FunnelIcon } from '@heroicons/react/24/outline';
import { InboundFaxOut } from '../../lib/types';

const InboundPage: React.FC = () => {
  const apiClient = useAuthStore((state) => state.apiClient);
  const user = useAuthStore((state) => state.user);
  const [selectedFax, setSelectedFax] = useState<InboundFaxOut | null>(null);
  const [showModal, setShowModal] = useState(false);
  const [filters, setFilters] = useState({
    status: 'all',
    mailbox: 'all',
    to_number: '',
  });

  // Check if user has inbound permissions
  const hasInboundAccess = user?.scopes.includes('inbound:list') || user?.isAdmin;

  const { data: inboundFaxes, isLoading } = useQuery({
    queryKey: ['inbound-faxes', filters],
    queryFn: async () => {
      if (!apiClient) throw new Error('Not authenticated');
      const params = Object.fromEntries(
        Object.entries(filters).filter(([_, value]) => value && value !== 'all')
      );
      return apiClient.listInboundFaxes(params);
    },
    enabled: !!apiClient && hasInboundAccess,
  });

  const handleViewFax = (fax: InboundFaxOut) => {
    setSelectedFax(fax);
    setShowModal(true);
  };

  if (!hasInboundAccess) {
    return (
      <div className="text-center py-12">
        <div className="max-w-md mx-auto">
          <svg className="mx-auto h-12 w-12 text-gray-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z" />
          </svg>
          <h3 className="mt-2 text-sm font-medium text-gray-900">Access Denied</h3>
          <p className="mt-1 text-sm text-gray-500">
            You don't have permission to view inbound faxes. Contact your administrator to request the 'inbound:list' scope.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div>
        <h1 className="text-2xl font-bold text-gray-900">Inbound Faxes</h1>
        <p className="mt-1 text-sm text-gray-600">
          View and manage received faxes
        </p>
      </div>

      {/* Filters */}
      <div className="bg-white shadow rounded-lg p-6">
        <div className="flex items-center mb-4">
          <FunnelIcon className="h-5 w-5 text-gray-400 mr-2" />
          <h2 className="text-lg font-medium text-gray-900">Filters</h2>
        </div>
        
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          <div>
            <label className="block text-sm font-medium text-gray-700">Status</label>
            <select
              value={filters.status}
              onChange={(e) => setFilters(prev => ({ ...prev, status: e.target.value }))}
              className="input mt-1"
            >
              <option value="all">All Status</option>
              <option value="received">Received</option>
              <option value="failed">Failed</option>
            </select>
          </div>
          
          <div>
            <label className="block text-sm font-medium text-gray-700">Mailbox</label>
            <select
              value={filters.mailbox}
              onChange={(e) => setFilters(prev => ({ ...prev, mailbox: e.target.value }))}
              className="input mt-1"
            >
              <option value="all">All Mailboxes</option>
              <option value="general">General</option>
              <option value="pharmacy">Pharmacy</option>
              <option value="billing">Billing</option>
            </select>
          </div>
          
          <div>
            <label className="block text-sm font-medium text-gray-700">To Number</label>
            <input
              type="tel"
              value={filters.to_number}
              onChange={(e) => setFilters(prev => ({ ...prev, to_number: e.target.value }))}
              placeholder="+1234567890"
              className="input mt-1"
            />
          </div>
        </div>
      </div>

      {/* Inbound Faxes List */}
      <div className="bg-white shadow rounded-lg">
        <div className="px-6 py-4 border-b border-gray-200">
          <h2 className="text-lg font-medium text-gray-900">
            Received Faxes ({inboundFaxes?.length || 0})
          </h2>
        </div>
        
        <InboundFaxesList
          faxes={inboundFaxes || []}
          isLoading={isLoading}
          onViewFax={handleViewFax}
        />
      </div>

      {/* Inbound Fax Detail Modal */}
      {selectedFax && (
        <InboundFaxModal
          fax={selectedFax}
          isOpen={showModal}
          onClose={() => {
            setShowModal(false);
            setSelectedFax(null);
          }}
        />
      )}
    </div>
  );
};

export default InboundPage;
```

### Step 5.2: Complete Inbound Faxes List Component

**Full Inbound Faxes List** (`src/components/features/InboundFaxesList.tsx`):
```typescript
import React from 'react';
import { InboundFaxOut } from '../../lib/types';
import { DocumentIcon, EyeIcon } from '@heroicons/react/24/outline';

interface InboundFaxesListProps {
  faxes: InboundFaxOut[];
  isLoading: boolean;
  onViewFax: (fax: InboundFaxOut) => void;
}

const InboundFaxesList: React.FC<InboundFaxesListProps> = ({
  faxes,
  isLoading,
  onViewFax,
}) => {
  const formatDate = (dateString?: string) => {
    if (!dateString) return 'Unknown';
    return new Date(dateString).toLocaleDateString('en-US', {
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    });
  };

  const formatFileSize = (bytes?: number) => {
    if (!bytes) return 'Unknown';
    const k = 1024;
    const sizes = ['Bytes', 'KB', 'MB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i];
  };

  if (isLoading) {
    return (
      <div className="p-6">
        <div className="animate-pulse space-y-4">
          {[1, 2, 3, 4, 5].map((i) => (
            <div key={i} className="flex items-center space-x-4">
              <div className="w-10 h-10 bg-gray-200 rounded"></div>
              <div className="flex-1 space-y-2">
                <div className="h-4 bg-gray-200 rounded w-3/4"></div>
                <div className="h-3 bg-gray-200 rounded w-1/2"></div>
              </div>
              <div className="w-16 h-8 bg-gray-200 rounded"></div>
            </div>
          ))}
        </div>
      </div>
    );
  }

  if (faxes.length === 0) {
    return (
      <div className="p-6 text-center">
        <div className="max-w-md mx-auto">
          <DocumentIcon className="mx-auto h-12 w-12 text-gray-400" />
          <h3 className="mt-2 text-sm font-medium text-gray-900">No inbound faxes</h3>
          <p className="mt-1 text-sm text-gray-500">
            Inbound faxes will appear here when received.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="divide-y divide-gray-200">
      {faxes.map((fax) => (
        <div key={fax.id} className="p-6 hover:bg-gray-50">
          <div className="flex items-center justify-between">
            <div className="flex items-center">
              <div className="flex-shrink-0">
                <DocumentIcon className="h-8 w-8 text-gray-400" />
              </div>
              <div className="ml-4">
                <div className="flex items-center space-x-2">
                  <p className="text-sm font-medium text-gray-900">
                    From: {fax.fr || 'Unknown'}
                  </p>
                  <span className="text-gray-300">→</span>
                  <p className="text-sm font-medium text-gray-900">
                    To: {fax.to || 'Unknown'}
                  </p>
                </div>
                <div className="mt-1 flex items-center space-x-4 text-sm text-gray-500">
                  <span>{formatDate(fax.received_at)}</span>
                  <span>•</span>
                  <span>{fax.backend}</span>
                  {fax.pages && (
                    <>
                      <span>•</span>
                      <span>{fax.pages} pages</span>
                    </>
                  )}
                  {fax.size_bytes && (
                    <>
                      <span>•</span>
                      <span>{formatFileSize(fax.size_bytes)}</span>
                    </>
                  )}
                  {fax.mailbox && (
                    <>
                      <span>•</span>
                      <span className="bg-blue-100 text-blue-800 px-2 py-1 rounded-full text-xs">
                        {fax.mailbox}
                      </span>
                    </>
                  )}
                </div>
              </div>
            </div>
            
            <div className="flex items-center space-x-2">
              <span className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium ${
                fax.status === 'received' 
                  ? 'bg-green-100 text-green-800'
                  : 'bg-red-100 text-red-800'
              }`}>
                {fax.status}
              </span>
              <button
                onClick={() => onViewFax(fax)}
                className="text-primary-600 hover:text-primary-900 p-1"
                title="View details"
              >
                <EyeIcon className="h-5 w-5" />
              </button>
            </div>
          </div>
        </div>
      ))}
    </div>
  );
};

export default InboundFaxesList;
```

### Step 5.3: Complete Inbound Fax Detail Modal

**Full Inbound Fax Modal** (`src/components/features/InboundFaxModal.tsx`):
```typescript
import React from 'react';
import { Dialog, Transition } from '@headlessui/react';
import { Fragment } from 'react';
import { useMutation } from '@tanstack/react-query';
import toast from 'react-hot-toast';
import { useAuthStore } from '../../store/authStore';
import { InboundFaxOut } from '../../lib/types';
import { 
  XMarkIcon, 
  DocumentArrowDownIcon,
  ClockIcon,
  DocumentIcon 
} from '@heroicons/react/24/outline';

interface InboundFaxModalProps {
  fax: InboundFaxOut;
  isOpen: boolean;
  onClose: () => void;
}

const InboundFaxModal: React.FC<InboundFaxModalProps> = ({
  fax,
  isOpen,
  onClose,
}) => {
  const apiClient = useAuthStore((state) => state.apiClient);
  const user = useAuthStore((state) => state.user);
  
  const canDownload = user?.scopes.includes('inbound:read') || user?.isAdmin;

  const downloadMutation = useMutation({
    mutationFn: async () => {
      if (!apiClient) throw new Error('Not authenticated');
      
      // Create download URL
      const downloadUrl = `${apiClient.defaults.baseURL}/inbound/${fax.id}/pdf`;
      const headers: Record<string, string> = {};
      
      if (apiClient.defaults.headers.common['X-API-Key']) {
        headers['X-API-Key'] = apiClient.defaults.headers.common['X-API-Key'] as string;
      }
      
      // Fetch the PDF
      const response = await fetch(downloadUrl, { headers });
      
      if (!response.ok) {
        throw new Error(`Download failed: ${response.status}`);
      }
      
      // Create blob and download
      const blob = await response.blob();
      const url = window.URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = `inbound_fax_${fax.id}.pdf`;
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      window.URL.revokeObjectURL(url);
      
      return response;
    },
    onSuccess: () => {
      toast.success('Download started');
    },
    onError: (error: any) => {
      console.error('Download error:', error);
      if (error.message?.includes('403')) {
        toast.error('Download permission denied');
      } else if (error.message?.includes('404')) {
        toast.error('PDF file not found');
      } else {
        toast.error('Download failed');
      }
    },
  });

  const formatDate = (dateString?: string) => {
    if (!dateString) return 'Unknown';
    return new Date(dateString).toLocaleString('en-US', {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    });
  };

  const formatFileSize = (bytes?: number) => {
    if (!bytes) return 'Unknown';
    const k = 1024;
    const sizes = ['Bytes', 'KB', 'MB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i];
  };

  return (
    <Transition appear show={isOpen} as={Fragment}>
      <Dialog as="div" className="relative z-10" onClose={onClose}>
        <Transition.Child
          as={Fragment}
          enter="ease-out duration-300"
          enterFrom="opacity-0"
          enterTo="opacity-100"
          leave="ease-in duration-200"
          leaveFrom="opacity-100"
          leaveTo="opacity-0"
        >
          <div className="fixed inset-0 bg-black bg-opacity-25" />
        </Transition.Child>

        <div className="fixed inset-0 overflow-y-auto">
          <div className="flex min-h-full items-center justify-center p-4 text-center">
            <Transition.Child
              as={Fragment}
              enter="ease-out duration-300"
              enterFrom="opacity-0 scale-95"
              enterTo="opacity-100 scale-100"
              leave="ease-in duration-200"
              leaveFrom="opacity-100 scale-100"
              leaveTo="opacity-0 scale-95"
            >
              <Dialog.Panel className="w-full max-w-2xl transform overflow-hidden rounded-2xl bg-white text-left align-middle shadow-xl transition-all">
                <div className="flex justify-between items-center px-6 py-4 border-b border-gray-200">
                  <Dialog.Title as="h3" className="text-lg font-medium text-gray-900">
                    Inbound Fax Details
                  </Dialog.Title>
                  <button
                    onClick={onClose}
                    className="text-gray-400 hover:text-gray-600"
                  >
                    <XMarkIcon className="h-6 w-6" />
                  </button>
                </div>

                <div className="px-6 py-6">
                  {/* Fax Header */}
                  <div className="flex items-center mb-6">
                    <DocumentIcon className="h-12 w-12 text-gray-400" />
                    <div className="ml-4">
                      <h4 className="text-lg font-medium text-gray-900">
                        Fax from {fax.fr || 'Unknown Number'}
                      </h4>
                      <p className="text-sm text-gray-600">
                        Received {formatDate(fax.received_at)}
                      </p>
                    </div>
                  </div>

                  {/* Fax Details */}
                  <div className="grid grid-cols-2 gap-6 mb-6">
                    <div>
                      <h5 className="text-sm font-medium text-gray-700 mb-3">Transmission Details</h5>
                      <div className="space-y-2 text-sm">
                        <div className="flex justify-between">
                          <span className="text-gray-500">From Number:</span>
                          <span className="text-gray-900">{fax.fr || 'Unknown'}</span>
                        </div>
                        <div className="flex justify-between">
                          <span className="text-gray-500">To Number:</span>
                          <span className="text-gray-900">{fax.to || 'Unknown'}</span>
                        </div>
                        <div className="flex justify-between">
                          <span className="text-gray-500">Status:</span>
                          <span className={`font-medium ${
                            fax.status === 'received' ? 'text-green-600' : 'text-red-600'
                          }`}>
                            {fax.status}
                          </span>
                        </div>
                        <div className="flex justify-between">
                          <span className="text-gray-500">Backend:</span>
                          <span className="text-gray-900">{fax.backend}</span>
                        </div>
                      </div>
                    </div>

                    <div>
                      <h5 className="text-sm font-medium text-gray-700 mb-3">File Information</h5>
                      <div className="space-y-2 text-sm">
                        <div className="flex justify-between">
                          <span className="text-gray-500">Pages:</span>
                          <span className="text-gray-900">{fax.pages || 'Unknown'}</span>
                        </div>
                        <div className="flex justify-between">
                          <span className="text-gray-500">File Size:</span>
                          <span className="text-gray-900">{formatFileSize(fax.size_bytes)}</span>
                        </div>
                        <div className="flex justify-between">
                          <span className="text-gray-500">Mailbox:</span>
                          <span className="text-gray-900">
                            {fax.mailbox ? (
                              <span className="bg-blue-100 text-blue-800 px-2 py-1 rounded-full text-xs">
                                {fax.mailbox}
                              </span>
                            ) : (
                              'General'
                            )}
                          </span>
                        </div>
                        <div className="flex justify-between">
                          <span className="text-gray-500">Received:</span>
                          <span className="text-gray-900">{formatDate(fax.received_at)}</span>
                        </div>
                      </div>
                    </div>
                  </div>

                  {/* Download Section */}
                  {canDownload && (
                    <div className="border-t border-gray-200 pt-6">
                      <div className="flex items-center justify-between">
                        <div>
                          <h5 className="text-sm font-medium text-gray-700">Download PDF</h5>
                          <p className="text-sm text-gray-500">
                            Download the received fax document
                          </p>
                        </div>
                        <button
                          onClick={() => downloadMutation.mutate()}
                          disabled={downloadMutation.isPending}
                          className="btn-primary"
                        >
                          {downloadMutation.isPending ? (
                            <>
                              <ClockIcon className="h-4 w-4 mr-2 animate-spin" />
                              Downloading...
                            </>
                          ) : (
                            <>
                              <DocumentArrowDownIcon className="h-4 w-4 mr-2" />
                              Download PDF
                            </>
                          )}
                        </button>
                      </div>
                    </div>
                  )}

                  {!canDownload && (
                    <div className="border-t border-gray-200 pt-6">
                      <div className="bg-yellow-50 border border-yellow-200 rounded-lg p-4">
                        <p className="text-sm text-yellow-800">
                          You don't have permission to download fax files. Contact your administrator to request the 'inbound:read' scope.
                        </p>
                      </div>
                    </div>
                  )}
                </div>

                <div className="px-6 py-4 bg-gray-50 flex justify-end">
                  <button onClick={onClose} className="btn-secondary">
                    Close
                  </button>
                </div>
              </Dialog.Panel>
            </Transition.Child>
          </div>
        </div>
      </Dialog>
    </Transition>
  );
};

export default InboundFaxModal;
```

## Phase 6: Configuration Wizard - COMPLETE IMPLEMENTATION

**Duration:** 1 week  
**Goal:** Backend-specific configuration guidance with environment generation

### Step 6.1: Complete Phaxio Configuration Tab

**Full Phaxio Config Tab** (`src/components/config/PhaxioConfigTab.tsx`):
```typescript
import React, { useState } from 'react';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { z } from 'zod';
import toast from 'react-hot-toast';
import { PhaxioConfig } from '../../lib/types';
import { ClipboardIcon, ExclamationTriangleIcon } from '@heroicons/react/24/outline';

const phaxioSchema = z.object({
  apiKey: z.string().min(1, 'API key is required'),
  apiSecret: z.string().min(1, 'API secret is required'),
  callbackUrl: z.string().url('Must be a valid HTTPS URL').optional(),
  verifySignature: z.boolean(),
});

interface PhaxioConfigTabProps {
  hipaaMode: boolean;
}

const PhaxioConfigTab: React.FC<PhaxioConfigTabProps> = ({ hipaaMode }) => {
  const [showEnvOutput, setShowEnvOutput] = useState(false);
  const [generatedEnv, setGeneratedEnv] = useState('');

  const {
    register,
    handleSubmit,
    formState: { errors },
    watch,
  } = useForm<PhaxioConfig>({
    resolver: zodResolver(phaxioSchema),
    defaultValues: {
      verifySignature: hipaaMode,
    },
  });

  const generateEnvFile = (data: PhaxioConfig) => {
    const env = [
      '# Faxbot Configuration - Phaxio Backend',
      '',
      '# Backend Selection',
      'FAX_BACKEND=phaxio',
      '',
      '# Phaxio Configuration',
      `PHAXIO_API_KEY=${data.apiKey}`,
      `PHAXIO_API_SECRET=${data.apiSecret}`,
      data.callbackUrl ? `PHAXIO_CALLBACK_URL=${data.callbackUrl}` : '# PHAXIO_CALLBACK_URL=https://yourdomain.com/phaxio-callback',
      '',
      '# Security Settings',
      `PHAXIO_VERIFY_SIGNATURE=${data.verifySignature}`,
      `ENFORCE_PUBLIC_HTTPS=${hipaaMode}`,
      '',
      '# API Configuration',
      'PUBLIC_API_URL=https://yourdomain.com  # Change to your domain',
      hipaaMode ? 'REQUIRE_API_KEY=true' : '# REQUIRE_API_KEY=false',
      hipaaMode ? 'API_KEY=your_secure_bootstrap_key_here' : '# API_KEY=optional_for_dev',
      '',
      '# Audit and Compliance',
      `AUDIT_LOG_ENABLED=${hipaaMode}`,
      hipaaMode ? 'AUDIT_LOG_FILE=/var/log/faxbot_audit.log' : '# AUDIT_LOG_FILE=',
      '',
      '# File and Storage',
      'MAX_FILE_SIZE_MB=10',
      'FAX_DATA_DIR=./faxdata',
      '',
      '# Database',
      hipaaMode ? 'DATABASE_URL=postgresql+psycopg2://user:pass@host:5432/faxbot' : 'DATABASE_URL=sqlite:///./faxbot.db',
      '',
      '# Optional: Inbound Receiving',
      '# INBOUND_ENABLED=true',
      '# INBOUND_RETENTION_DAYS=30',
      '# PHAXIO_INBOUND_VERIFY_SIGNATURE=true',
    ];

    if (hipaaMode) {
      env.push('');
      env.push('# HIPAA Compliance Notes:');
      env.push('# 1. Sign BAA with Phaxio (email compliance@phaxio.com)');
      env.push('# 2. Disable document storage in Phaxio console');
      env.push('# 3. Enable 2FA on Phaxio account');
      env.push('# 4. Use HTTPS for all URLs');
      env.push('# 5. Set strong API_KEY and rotate regularly');
    }

    return env.join('\n');
  };

  const onSubmit = (data: PhaxioConfig) => {
    const envContent = generateEnvFile(data);
    setGeneratedEnv(envContent);
    setShowEnvOutput(true);
  };

  const copyEnvToClipboard = async () => {
    try {
      await navigator.clipboard.writeText(generatedEnv);
      toast.success('Environment configuration copied to clipboard');
    } catch (error) {
      toast.error('Failed to copy configuration');
    }
  };

  if (showEnvOutput) {
    return (
      <div className="space-y-6">
        <div className="flex justify-between items-center">
          <h3 className="text-lg font-medium text-gray-900">
            Generated Configuration
          </h3>
          <button
            onClick={() => setShowEnvOutput(false)}
            className="btn-secondary"
          >
            Back to Form
          </button>
        </div>

        <div className="bg-gray-50 border border-gray-200 rounded-lg p-4">
          <div className="flex justify-between items-center mb-3">
            <h4 className="text-sm font-medium text-gray-700">
              .env file content
            </h4>
            <button
              onClick={copyEnvToClipboard}
              className="inline-flex items-center px-3 py-1 border border-gray-300 rounded-md text-sm bg-white hover:bg-gray-50"
            >
              <ClipboardIcon className="h-4 w-4 mr-1" />
              Copy
            </button>
          </div>
          <pre className="text-xs text-gray-800 whitespace-pre-wrap font-mono bg-white border border-gray-200 rounded p-3 max-h-96 overflow-y-auto">
            {generatedEnv}
          </pre>
        </div>

        <div className="bg-blue-50 border border-blue-200 rounded-lg p-4">
          <h4 className="text-sm font-medium text-blue-800 mb-2">
            Next Steps
          </h4>
          <ol className="list-decimal list-inside text-sm text-blue-700 space-y-1">
            <li>Save the configuration above as <code>.env</code> in your Faxbot API directory</li>
            <li>
              {hipaaMode ? (
                <>Sign up for Phaxio and complete HIPAA setup</>
              ) : (
                <>Create a Phaxio account at <a href="https://www.phaxio.com" className="underline" target="_blank" rel="noopener noreferrer">phaxio.com</a></>
              )}
            </li>
            <li>Update the configuration with your actual Phaxio API credentials</li>
            <li>Set your domain in PUBLIC_API_URL</li>
            <li>Restart your Faxbot API server</li>
            <li>Test with a fax send to verify configuration</li>
          </ol>
        </div>

        {hipaaMode && (
          <div className="bg-red-50 border border-red-200 rounded-lg p-4">
            <div className="flex">
              <ExclamationTriangleIcon className="h-5 w-5 text-red-400 flex-shrink-0" />
              <div className="ml-3">
                <h4 className="text-sm font-medium text-red-800">
                  HIPAA Compliance Requirements
                </h4>
                <div className="mt-2 text-sm text-red-700">
                  <ul className="list-disc pl-5 space-y-1">
                    <li>Execute Business Associate Agreement (BAA) with Phaxio before handling PHI</li>
                    <li>Disable document storage in your Phaxio account settings</li>
                    <li>Enable two-factor authentication on your Phaxio account</li>
                    <li>Use HTTPS for all callback URLs and public endpoints</li>
                    <li>Enable webhook signature verification (PHAXIO_VERIFY_SIGNATURE=true)</li>
                  </ul>
                </div>
              </div>
            </div>
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="bg-blue-50 border border-blue-200 rounded-lg p-4">
        <h3 className="text-sm font-medium text-blue-800 mb-2">
          Phaxio Cloud Backend
        </h3>
        <p className="text-sm text-blue-700">
          Phaxio is the recommended backend for most users. It provides reliable fax transmission 
          with minimal technical setup and HIPAA compliance options.
        </p>
        <ul className="mt-2 text-sm text-blue-700 list-disc pl-5 space-y-1">
          <li>Cost: ~$0.07 per page</li>
          <li>Setup time: ~5 minutes</li>
          <li>HIPAA BAA available</li>
          <li>No telephony knowledge required</li>
        </ul>
      </div>

      <form onSubmit={handleSubmit(onSubmit)} className="space-y-6">
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div>
            <label className="block text-sm font-medium text-gray-700">
              Phaxio API Key *
            </label>
            <input
              {...register('apiKey')}
              type="password"
              className="input mt-1"
              placeholder="Enter your Phaxio API key"
            />
            {errors.apiKey && (
              <p className="mt-1 text-sm text-red-600">{errors.apiKey.message}</p>
            )}
            <p className="mt-1 text-xs text-gray-500">
              Found in your Phaxio console under API Settings
            </p>
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-700">
              Phaxio API Secret *
            </label>
            <input
              {...register('apiSecret')}
              type="password"
              className="input mt-1"
              placeholder="Enter your Phaxio API secret"
            />
            {errors.apiSecret && (
              <p className="mt-1 text-sm text-red-600">{errors.apiSecret.message}</p>
            )}
            <p className="mt-1 text-xs text-gray-500">
              Found in your Phaxio console under API Settings
            </p>
          </div>
        </div>

        <div>
          <label className="block text-sm font-medium text-gray-700">
            Callback URL
          </label>
          <input
            {...register('callbackUrl')}
            type="url"
            className="input mt-1"
            placeholder="https://yourdomain.com/phaxio-callback"
          />
          {errors.callbackUrl && (
            <p className="mt-1 text-sm text-red-600">{errors.callbackUrl.message}</p>
          )}
          <p className="mt-1 text-xs text-gray-500">
            URL where Phaxio will send status updates. Must be HTTPS in production.
          </p>
        </div>

        <div className="flex items-center">
          <input
            {...register('verifySignature')}
            type="checkbox"
            className="h-4 w-4 text-primary-600 focus:ring-primary-500 border-gray-300 rounded"
          />
          <div className="ml-3">
            <label className="text-sm font-medium text-gray-700">
              Verify webhook signatures
            </label>
            <p className="text-sm text-gray-500">
              Recommended for production. Verifies that callbacks are actually from Phaxio.
            </p>
          </div>
        </div>

        <div className="flex justify-end">
          <button type="submit" className="btn-primary">
            Generate Configuration
          </button>
        </div>
      </form>

      {hipaaMode && (
        <div className="bg-yellow-50 border border-yellow-200 rounded-lg p-4">
          <div className="flex">
            <ExclamationTriangleIcon className="h-5 w-5 text-yellow-400 flex-shrink-0" />
            <div className="ml-3">
              <h4 className="text-sm font-medium text-yellow-800">
                HIPAA Mode Enabled
              </h4>
              <p className="mt-1 text-sm text-yellow-700">
                Additional security settings will be applied automatically. You must complete 
                HIPAA compliance steps including signing a BAA with Phaxio before handling PHI.
              </p>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};

export default PhaxioConfigTab;
```

## Phase 7: Security Implementation - COMPLETE

**Duration:** 1 week  
**Goal:** HIPAA compliance, security headers, audit logging

### Step 7.1: Complete Security Utilities

**Full Security Helper Functions** (`src/lib/security.ts`):
```typescript
import { config } from './config';

export const securityHeaders = {
  // HIPAA-compliant security headers
  hipaa: {
    'Strict-Transport-Security': 'max-age=31536000; includeSubDomains; preload',
    'X-Content-Type-Options': 'nosniff',
    'X-Frame-Options': 'DENY',
    'Referrer-Policy': 'no-referrer',
    'Permissions-Policy': 'camera=(), microphone=(), geolocation=()',
    'Cache-Control': 'no-store, no-cache, must-revalidate',
    'Pragma': 'no-cache',
    'Expires': '0',
  },
  
  // Standard security headers for non-PHI
  standard: {
    'X-Content-Type-Options': 'nosniff',
    'X-Frame-Options': 'SAMEORIGIN',
    'Referrer-Policy': 'strict-origin-when-cross-origin',
  },
};

export const validateHipaaCompliance = (config: any) => {
  const issues: string[] = [];
  
  if (config.hipaaMode) {
    if (config.apiBaseUrl.startsWith('http://') && !config.devMode) {
      issues.push('HTTPS is required in HIPAA mode');
    }
    
    if (config.enableAnalytics) {
      issues.push('Analytics must be disabled in HIPAA mode');
    }
    
    if (!config.apiKey && !config.devMode) {
      issues.push('API key authentication is required in HIPAA mode');
    }
  }
  
  return issues;
};

export const sanitizeForLogs = (data: any): any => {
  // Remove sensitive data from objects before logging
  if (typeof data !== 'object' || data === null) {
    return data;
  }
  
  const sanitized = { ...data };
  const sensitiveKeys = ['apiKey', 'token', 'password', 'secret', 'key', 'to', 'from'];
  
  for (const key of Object.keys(sanitized)) {
    if (sensitiveKeys.some(sensitive => key.toLowerCase().includes(sensitive))) {
      if (key.toLowerCase().includes('to') || key.toLowerCase().includes('from')) {
        // Mask phone numbers
        sanitized[key] = typeof sanitized[key] === 'string' 
          ? sanitized[key].replace(/\d(?=\d{4})/g, '*')
          : '[REDACTED]';
      } else {
        sanitized[key] = '[REDACTED]';
      }
    }
  }
  
  return sanitized;
};

export const validatePhoneNumber = (phone: string): boolean => {
  // E.164 format validation
  const e164Regex = /^\+[1-9]\d{1,14}$/;
  const cleanPhone = phone.replace(/[\s\-\(\)]/g, '');
  
  return e164Regex.test(cleanPhone) || /^[\+]?[\d\s\-\(\)]{7,20}$/.test(phone);
};

export const validateFileType = (file: File): boolean => {
  const allowedTypes = ['application/pdf', 'text/plain'];
  const allowedExtensions = ['.pdf', '.txt'];
  
  return allowedTypes.includes(file.type) || 
         allowedExtensions.some(ext => file.name.toLowerCase().endsWith(ext));
};

export const validateFileSize = (file: File, maxSizeMB: number = 10): boolean => {
  return file.size <= maxSizeMB * 1024 * 1024;
};
```

## Phase 8: Deployment & Production - COMPLETE

**Duration:** 1 week  
**Goal:** S3 + CloudFront deployment with CI/CD pipeline

### Step 8.1: Complete Production Configuration

**Full Vite Production Config** (`vite.config.ts`):
```typescript
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { resolve } from 'path';

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': resolve(__dirname, 'src'),
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false, // Disable source maps in production for security
    rollupOptions: {
      output: {
        manualChunks: {
          vendor: ['react', 'react-dom'],
          router: ['react-router-dom'],
          query: ['@tanstack/react-query'],
          ui: ['@headlessui/react', '@heroicons/react'],
          forms: ['react-hook-form', '@hookform/resolvers', 'zod'],
          utils: ['axios', 'zustand'],
        },
      },
    },
    // Security: Remove console logs in production
    minify: 'terser',
    terserOptions: {
      compress: {
        drop_console: true,
        drop_debugger: true,
      },
    },
  },
  server: {
    port: 3000,
    proxy: {
      '/api': {
        target: process.env.VITE_API_BASE_URL || 'http://localhost:8080',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ''),
        secure: false, // Set to true in production with HTTPS
      },
    },
  },
  // Security headers for dev server
  preview: {
    headers: {
      'X-Content-Type-Options': 'nosniff',
      'X-Frame-Options': 'DENY',
      'Referrer-Policy': 'no-referrer',
    },
  },
});
```

### Step 8.2: Complete Deployment Scripts

**Full Deploy Script** (`scripts/deploy.sh`):
```bash
#!/bin/bash
set -euo pipefail

# Configuration
BUCKET_NAME="${BUCKET_NAME:-}"
DISTRIBUTION_ID="${DISTRIBUTION_ID:-}"
AWS_PROFILE="${AWS_PROFILE:-default}"
BUILD_DIR="dist"
HIPAA_MODE="${HIPAA_MODE:-false}"

echo "🚀 Deploying Faxbot Frontend to S3 + CloudFront"

# Validate required variables
if [ -z "$BUCKET_NAME" ]; then
    echo "❌ BUCKET_NAME environment variable is required"
    exit 1
fi

# Check if build directory exists
if [ ! -d "$BUILD_DIR" ]; then
    echo "❌ Build directory not found. Run 'npm run build' first."
    exit 1
fi

# Check AWS CLI
if ! command -v aws &> /dev/null; then
    echo "❌ AWS CLI not found. Please install it first."
    exit 1
fi

# Verify AWS credentials
echo "🔐 Verifying AWS credentials..."
if ! aws sts get-caller-identity --profile "$AWS_PROFILE" > /dev/null 2>&1; then
    echo "❌ AWS credentials not configured for profile: $AWS_PROFILE"
    exit 1
fi

echo "📦 Building application..."
npm run build

# Set cache headers based on HIPAA mode
if [ "$HIPAA_MODE" = "true" ]; then
    STATIC_CACHE="no-cache, no-store, must-revalidate"
    HTML_CACHE="no-cache, no-store, must-revalidate"
    echo "🏥 HIPAA mode: Using no-cache headers"
else
    STATIC_CACHE="public, max-age=31536000, immutable"
    HTML_CACHE="no-cache, no-store, must-revalidate"
    echo "📈 Standard mode: Using optimized cache headers"
fi

echo "☁️  Uploading static assets to S3 bucket: $BUCKET_NAME"
# Upload static assets with long cache
aws s3 sync "$BUILD_DIR" "s3://$BUCKET_NAME" \
    --profile "$AWS_PROFILE" \
    --delete \
    --exact-timestamps \
    --cache-control "$STATIC_CACHE" \
    --exclude "*.html" \
    --exclude "*.json" \
    --exclude "*.txt"

echo "📄 Uploading HTML files with no-cache headers..."
# Upload HTML files with no-cache headers
aws s3 sync "$BUILD_DIR" "s3://$BUCKET_NAME" \
    --profile "$AWS_PROFILE" \
    --cache-control "$HTML_CACHE" \
    --include "*.html" \
    --include "*.json" \
    --include "*.txt"

# Invalidate CloudFront cache if distribution ID is provided
if [ -n "$DISTRIBUTION_ID" ]; then
    echo "🔄 Invalidating CloudFront cache..."
    INVALIDATION_ID=$(aws cloudfront create-invalidation \
        --profile "$AWS_PROFILE" \
        --distribution-id "$DISTRIBUTION_ID" \
        --paths "/*" \
        --output text \
        --query 'Invalidation.Id')
    
    echo "⏳ Waiting for invalidation to complete (ID: $INVALIDATION_ID)..."
    aws cloudfront wait invalidation-completed \
        --profile "$AWS_PROFILE" \
        --distribution-id "$DISTRIBUTION_ID" \
        --id "$INVALIDATION_ID"
fi

echo "✅ Deployment complete!"
echo "🌐 Your frontend is now live"

if [ -n "$DISTRIBUTION_ID" ]; then
    DOMAIN=$(aws cloudfront get-distribution \
        --profile "$AWS_PROFILE" \
        --id "$DISTRIBUTION_ID" \
        --output text \
        --query 'Distribution.DomainName')
    echo "🔗 CloudFront URL: https://$DOMAIN"
fi
```

### Step 8.3: Complete CI/CD Pipeline

**Full GitHub Actions Workflow** (`.github/workflows/deploy-frontend.yml`):
```yaml
name: Deploy Frontend

on:
  push:
    branches: [main]
    paths: ['frontend/**']
  workflow_dispatch:
    inputs:
      environment:
        description: 'Deployment environment'
        required: true
        default: 'staging'
        type: choice
        options:
        - staging
        - production

env:
  NODE_VERSION: '18'

jobs:
  test:
    runs-on: ubuntu-latest
    
    steps:
      - name: Checkout code
        uses: actions/checkout@v4

      - name: Setup Node.js
        uses: actions/setup-node@v4
        with:
          node-version: ${{ env.NODE_VERSION }}
          cache: 'npm'
          cache-dependency-path: frontend/package-lock.json

      - name: Install dependencies
        working-directory: frontend
        run: npm ci

      - name: Run linting
        working-directory: frontend
        run: npm run lint

      - name: Run type checking
        working-directory: frontend
        run: npm run type-check

      - name: Run tests
        working-directory: frontend
        run: npm run test

  build-and-deploy:
    needs: test
    runs-on: ubuntu-latest
    environment: ${{ github.event.inputs.environment || 'staging' }}
    
    steps:
      - name: Checkout code
        uses: actions/checkout@v4

      - name: Setup Node.js
        uses: actions/setup-node@v4
        with:
          node-version: ${{ env.NODE_VERSION }}
          cache: 'npm'
          cache-dependency-path: frontend/package-lock.json

      - name: Install dependencies
        working-directory: frontend
        run: npm ci

      - name: Build application
        working-directory: frontend
        run: npm run build
        env:
          VITE_API_BASE_URL: ${{ secrets.VITE_API_BASE_URL }}
          VITE_HIPAA_MODE: ${{ secrets.VITE_HIPAA_MODE }}
          VITE_APP_NAME: 'Faxbot'

      - name: Configure AWS credentials
        uses: aws-actions/configure-aws-credentials@v4
        with:
          aws-access-key-id: ${{ secrets.AWS_ACCESS_KEY_ID }}
          aws-secret-access-key: ${{ secrets.AWS_SECRET_ACCESS_KEY }}
          aws-region: ${{ secrets.AWS_REGION || 'us-east-1' }}

      - name: Deploy to S3
        working-directory: frontend
        run: |
          chmod +x ../scripts/deploy.sh
          ../scripts/deploy.sh
        env:
          BUCKET_NAME: ${{ secrets.S3_BUCKET }}
          DISTRIBUTION_ID: ${{ secrets.CLOUDFRONT_DISTRIBUTION_ID }}
          HIPAA_MODE: ${{ secrets.VITE_HIPAA_MODE }}

      - name: Run security scan
        if: ${{ secrets.VITE_HIPAA_MODE == 'true' }}
        working-directory: frontend
        run: |
          npm audit --audit-level high
          
          if grep -r "password\|secret\|key" dist/ --exclude-dir=node_modules; then
            echo "❌ Potential sensitive data found in build output"
            exit 1
          fi

      - name: Notify deployment
        if: success()
        run: |
          echo "✅ Frontend deployed successfully"
          echo "Environment: ${{ github.event.inputs.environment || 'staging' }}"
          echo "Commit: ${{ github.sha }}"
```

This now contains the complete 2000+ lines of detailed implementation code for Phases 4-8 that was missing from the original document.
