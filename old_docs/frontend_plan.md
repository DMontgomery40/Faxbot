# Faxbot Frontend Development Plan - Complete Implementation Guide

**Status:** Draft - Work in Progress  
**Target Audience:** Junior developers who need step-by-step guidance  
**Hosting Decision:** S3 + CloudFront (not Netlify due to long-term HIPAA restrictions)

## 🚨 CRITICAL CONTEXT - READ FIRST

**This project has NEVER existed before in history.** There has never been an open-source, self-hostable fax API with AI integration and HIPAA compliance. You cannot make assumptions about patterns or "normal" approaches. Every decision must be based on the actual codebase analysis provided in this document.

**Project Name:** "Faxbot" (never "OpenFax" or any other name)  
**Current Status:** Production-ready API with 4 backends, multi-key auth, inbound receiving, MCP integration  
**Your Goal:** Build a functional web UI that safely exposes the existing API capabilities

## Table of Contents

1. [Understanding the Existing System](#understanding-the-existing-system)
2. [Frontend Architecture Decisions](#frontend-architecture-decisions)
3. [Development Environment Setup](#development-environment-setup)
4. [Implementation Phases](#implementation-phases)
5. [Security Implementation](#security-implementation)
6. [Deployment Guide](#deployment-guide)
7. [Testing Strategy](#testing-strategy)
8. [Maintenance and Scaling](#maintenance-and-scaling)

## Understanding the Existing System

### The Faxbot API You're Building Against

**Core API Endpoints** (from `api/app/main.py`):
```
POST /fax              # Send fax (multipart: to, file)
GET  /fax/{id}         # Check fax status
GET  /health           # Service health check
GET  /fax/{id}/pdf     # Tokenized PDF access (for cloud providers)
POST /phaxio-callback  # Phaxio webhook (status updates)
POST /sinch-inbound    # Sinch webhook (inbound faxes)
POST /phaxio-inbound   # Phaxio webhook (inbound faxes)
```

**Admin Endpoints** (for API key management):
```
POST /admin/api-keys           # Create API key
GET  /admin/api-keys           # List API keys
DELETE /admin/api-keys/{keyId} # Revoke API key
POST /admin/api-keys/{keyId}/rotate # Rotate API key
```

**Inbound Endpoints** (when `INBOUND_ENABLED=true`):
```
GET /inbound                   # List inbound faxes
GET /inbound/{id}              # Get inbound fax metadata
GET /inbound/{id}/pdf          # Download inbound PDF
```

### Authentication System (from `api/app/auth.py`)

**Token Format:** `fbk_live_<keyId>_<secret>`  
**Header:** `X-API-Key: <token>`  
**Scopes:** 
- `fax:send` - Can send faxes
- `fax:read` - Can read fax status
- `keys:manage` - Can manage API keys (admin)
- `inbound:list` - Can list inbound faxes
- `inbound:read` - Can read/download inbound faxes

**Environment Variables for Auth:**
- `API_KEY` - Bootstrap admin key (env variable)
- `REQUIRE_API_KEY=true` - Force authentication (HIPAA mode)
- `REQUIRE_API_KEY=false` - Allow unauthenticated requests (dev mode)

### The Four Backends (Mutually Exclusive)

**Backend Selection:** `FAX_BACKEND` environment variable

1. **Phaxio** (`FAX_BACKEND=phaxio`)
   - Cloud service, ~$0.07 per page
   - Requires: `PHAXIO_API_KEY`, `PHAXIO_API_SECRET`
   - HIPAA: BAA available, webhook HMAC verification
   - Best for: Healthcare users, non-technical users

2. **Sinch** (`FAX_BACKEND=sinch`) 
   - Cloud service, "Phaxio by Sinch"
   - Requires: `SINCH_PROJECT_ID`, `SINCH_API_KEY`, `SINCH_API_SECRET`
   - Similar to Phaxio but different API

3. **SIP/Asterisk** (`FAX_BACKEND=sip`)
   - Self-hosted, requires SIP trunk provider
   - Requires: `ASTERISK_AMI_*` settings, SIP trunk config
   - Best for: Technical users, high-volume, cost-conscious

4. **Test** (`FAX_DISABLED=true`)
   - Development/testing only
   - Simulates all responses, no actual transmission

**CRITICAL:** Your UI must never mix backend instructions. A Phaxio user should never see Asterisk configuration.

### Configuration System (from `api/app/config.py`)

**Key Settings Your UI Needs to Handle:**
```python
# Core
fax_backend: str = "sip"  # phaxio|sinch|sip
fax_disabled: bool = False
max_file_size_mb: int = 10
public_api_url: str = "http://localhost:8080"

# Auth
api_key: str = ""
require_api_key: bool = False
max_requests_per_minute: int = 0

# Security
enforce_public_https: bool = False
phaxio_verify_signature: bool = False  # Should be True in prod
audit_log_enabled: bool = False

# Inbound
inbound_enabled: bool = False
inbound_retention_days: int = 30
inbound_token_ttl_minutes: int = 60

# Storage
storage_backend: str = "local"  # local|s3
s3_bucket: str = ""
s3_region: str = ""
s3_kms_key_id: str = ""
```

**HIPAA vs Non-HIPAA Profiles:**

HIPAA (Healthcare):
```env
REQUIRE_API_KEY=true
ENFORCE_PUBLIC_HTTPS=true
PHAXIO_VERIFY_SIGNATURE=true
AUDIT_LOG_ENABLED=true
STORAGE_BACKEND=s3
S3_KMS_KEY_ID=your_kms_key
```

Non-HIPAA (Development/Business):
```env
REQUIRE_API_KEY=false
ENFORCE_PUBLIC_HTTPS=false
PHAXIO_VERIFY_SIGNATURE=false
AUDIT_LOG_ENABLED=false
STORAGE_BACKEND=local
```

## Frontend Architecture Decisions

### Technology Stack - Why These Choices

**Framework:** React 18 with TypeScript
- **Why:** Mature ecosystem, excellent TypeScript support, familiar to most developers
- **Not Vue/Angular:** React has better HIPAA-compliant component libraries

**Build Tool:** Vite
- **Why:** Faster than Create React App, better dev experience, smaller bundles
- **Configuration:** Zero-config for React + TypeScript

**Styling:** Tailwind CSS + Headless UI
- **Why:** Utility-first, consistent design system, accessible components
- **HIPAA Consideration:** No external font/CSS CDNs, all assets self-hosted

**State Management:** React Query (TanStack Query) + Zustand
- **React Query:** Server state management, caching, background updates
- **Zustand:** Client state (current user, UI state, session data)

**HTTP Client:** Axios
- **Why:** Better error handling than fetch, request/response interceptors
- **Configuration:** Automatic `X-API-Key` header injection

**File Handling:** react-dropzone
- **Why:** Drag-and-drop support, file validation, accessibility
- **Validation:** Client-side PDF/TXT validation, size limits

**Routing:** React Router v6
- **Why:** Standard React routing, supports protected routes

**Form Handling:** React Hook Form + Zod
- **React Hook Form:** Performance, minimal re-renders
- **Zod:** Type-safe validation schemas

### Directory Structure

```
frontend/
├── public/
│   ├── index.html
│   ├── favicon.ico
│   └── robots.txt
├── src/
│   ├── components/          # Reusable UI components
│   │   ├── ui/             # Basic UI elements (Button, Input, etc.)
│   │   ├── forms/          # Form components
│   │   ├── layout/         # Layout components
│   │   └── features/       # Feature-specific components
│   ├── pages/              # Route components
│   │   ├── auth/           # Authentication pages
│   │   ├── dashboard/      # Main dashboard
│   │   ├── send/           # Send fax pages
│   │   ├── admin/          # Admin pages
│   │   ├── inbound/        # Inbound fax pages
│   │   └── config/         # Configuration wizard
│   ├── lib/                # Utilities and configurations
│   │   ├── api.ts          # API client
│   │   ├── auth.ts         # Auth helpers
│   │   ├── config.ts       # App configuration
│   │   ├── types.ts        # TypeScript types
│   │   ├── utils.ts        # Utility functions
│   │   └── validation.ts   # Validation schemas
│   ├── hooks/              # Custom React hooks
│   ├── store/              # Zustand stores
│   ├── styles/             # Global styles
│   ├── App.tsx
│   ├── main.tsx
│   └── vite-env.d.ts
├── .env.example
├── .env.local
├── .gitignore
├── index.html
├── package.json
├── tailwind.config.js
├── tsconfig.json
├── vite.config.ts
└── README.md
```

### API Client Architecture

**Base API Client** (`src/lib/api.ts`):
```typescript
import axios, { AxiosInstance, AxiosRequestConfig } from 'axios';

interface ApiConfig {
  baseURL: string;
  apiKey?: string;
}

class FaxbotApiClient {
  private client: AxiosInstance;
  
  constructor(config: ApiConfig) {
    this.client = axios.create({
      baseURL: config.baseURL,
      timeout: 30000,
      headers: {
        'Content-Type': 'application/json',
      },
    });
    
    // Add API key to all requests if available
    if (config.apiKey) {
      this.client.defaults.headers.common['X-API-Key'] = config.apiKey;
    }
    
    // Response interceptor for error handling
    this.client.interceptors.response.use(
      (response) => response,
      (error) => {
        // Handle common error cases
        if (error.response?.status === 401) {
          // Redirect to login or show auth error
        }
        return Promise.reject(error);
      }
    );
  }
  
  // Health check
  async checkHealth() {
    const response = await this.client.get('/health');
    return response.data;
  }
  
  // Send fax
  async sendFax(to: string, file: File) {
    const formData = new FormData();
    formData.append('to', to);
    formData.append('file', file);
    
    const response = await this.client.post('/fax', formData, {
      headers: {
        'Content-Type': 'multipart/form-data',
      },
    });
    return response.data;
  }
  
  // Get fax status
  async getFaxStatus(jobId: string) {
    const response = await this.client.get(`/fax/${jobId}`);
    return response.data;
  }
  
  // Admin: Create API key
  async createApiKey(data: CreateApiKeyRequest) {
    const response = await this.client.post('/admin/api-keys', data);
    return response.data;
  }
  
  // Admin: List API keys
  async listApiKeys() {
    const response = await this.client.get('/admin/api-keys');
    return response.data;
  }
  
  // Admin: Revoke API key
  async revokeApiKey(keyId: string) {
    const response = await this.client.delete(`/admin/api-keys/${keyId}`);
    return response.data;
  }
  
  // Inbound: List inbound faxes
  async listInboundFaxes(params?: InboundListParams) {
    const response = await this.client.get('/inbound', { params });
    return response.data;
  }
  
  // Inbound: Get inbound fax
  async getInboundFax(id: string) {
    const response = await this.client.get(`/inbound/${id}`);
    return response.data;
  }
  
  // Update API key for subsequent requests
  setApiKey(apiKey: string) {
    this.client.defaults.headers.common['X-API-Key'] = apiKey;
  }
  
  // Remove API key
  clearApiKey() {
    delete this.client.defaults.headers.common['X-API-Key'];
  }
}

export default FaxbotApiClient;
```

### TypeScript Types (from API analysis)

**Core Types** (`src/lib/types.ts`):
```typescript
// API Response Types (matching FastAPI models)
export interface FaxJobOut {
  id: string;
  to: string;
  status: 'queued' | 'in_progress' | 'SUCCESS' | 'FAILED';
  error?: string;
  pages?: number;
  backend: 'phaxio' | 'sinch' | 'sip' | 'disabled';
  provider_sid?: string;
  created_at: string;
  updated_at: string;
}

export interface ApiKeyMeta {
  key_id: string;
  name?: string;
  owner?: string;
  scopes: string[];
  created_at?: string;
  last_used_at?: string;
  expires_at?: string;
  revoked_at?: string;
  note?: string;
}

export interface CreateApiKeyRequest {
  name?: string;
  owner?: string;
  scopes?: string[];
  expires_at?: string;
  note?: string;
}

export interface CreateApiKeyResponse {
  key_id: string;
  token: string;  // Shown only once!
  name?: string;
  owner?: string;
  scopes: string[];
  expires_at?: string;
}

export interface InboundFaxOut {
  id: string;
  fr?: string;
  to?: string;
  status: string;
  backend: string;
  pages?: number;
  size_bytes?: number;
  created_at?: string;
  received_at?: string;
  updated_at?: string;
  mailbox?: string;
}

// Configuration Types
export interface AppConfig {
  apiBaseUrl: string;
  apiKey?: string;
  backend?: 'phaxio' | 'sinch' | 'sip' | 'disabled';
  hipaaMode: boolean;
  inboundEnabled: boolean;
  maxFileSizeMb: number;
}

// UI State Types
export interface User {
  apiKey: string;
  scopes: string[];
  isAdmin: boolean;
}

export interface UIState {
  sidebarOpen: boolean;
  currentPage: string;
  loading: boolean;
  error?: string;
}

// Form Types
export interface SendFaxForm {
  to: string;
  file?: File;
}

export interface ConnectionForm {
  apiBaseUrl: string;
  apiKey: string;
}

// Backend Configuration Types
export interface PhaxioConfig {
  apiKey: string;
  apiSecret: string;
  callbackUrl?: string;
  verifySignature: boolean;
}

export interface SinchConfig {
  projectId: string;
  apiKey: string;
  apiSecret: string;
}

export interface SipConfig {
  amiHost: string;
  amiPort: number;
  amiUsername: string;
  amiPassword: string;
  sipUsername: string;
  sipPassword: string;
  sipServer: string;
  sipFromUser: string;
}
```

## Development Environment Setup

### Step 1: Prerequisites

**Required Software:**
```bash
# Node.js 18+ (use nvm for version management)
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.0/install.sh | bash
nvm install 18
nvm use 18

# Verify versions
node --version  # Should be 18.x.x
npm --version   # Should be 9.x.x or higher
```

**Development Tools:**
- VS Code with extensions:
  - TypeScript and JavaScript Language Features
  - ES7+ React/Redux/React-Native snippets
  - Tailwind CSS IntelliSense
  - Prettier - Code formatter
  - ESLint

### Step 2: Project Initialization

**Create the frontend directory:**
```bash
cd /Users/davidmontgomery/faxbot
mkdir frontend
cd frontend
```

**Initialize with Vite:**
```bash
npm create vite@latest . -- --template react-ts
npm install
```

**Install additional dependencies:**
```bash
# Core dependencies
npm install \
  @tanstack/react-query \
  axios \
  react-router-dom \
  react-hook-form \
  react-dropzone \
  zustand \
  zod \
  @hookform/resolvers

# UI dependencies
npm install \
  @headlessui/react \
  @heroicons/react \
  tailwindcss \
  @tailwindcss/forms \
  clsx \
  tailwind-merge

# Dev dependencies
npm install -D \
  @types/node \
  autoprefixer \
  postcss \
  prettier \
  eslint-config-prettier
```

**Initialize Tailwind CSS:**
```bash
npx tailwindcss init -p
```

**Configure Tailwind** (`tailwind.config.js`):
```javascript
/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        primary: {
          50: '#eff6ff',
          500: '#3b82f6',
          600: '#2563eb',
          700: '#1d4ed8',
        }
      }
    },
  },
  plugins: [
    require('@tailwindcss/forms'),
  ],
}
```

**Add global styles** (`src/styles/globals.css`):
```css
@tailwind base;
@tailwind components;
@tailwind utilities;

/* Custom styles */
@layer base {
  html {
    font-family: system-ui, sans-serif;
  }
}

@layer components {
  .btn {
    @apply px-4 py-2 rounded-md font-medium focus:outline-none focus:ring-2 focus:ring-offset-2;
  }
  
  .btn-primary {
    @apply btn bg-primary-600 text-white hover:bg-primary-700 focus:ring-primary-500;
  }
  
  .btn-secondary {
    @apply btn bg-gray-200 text-gray-900 hover:bg-gray-300 focus:ring-gray-500;
  }
  
  .input {
    @apply block w-full rounded-md border-gray-300 shadow-sm focus:border-primary-500 focus:ring-primary-500;
  }
}
```

### Step 3: Environment Configuration

**Create environment files:**

`.env.example`:
```env
# API Configuration
VITE_API_BASE_URL=http://localhost:8080
VITE_API_KEY=

# App Configuration
VITE_APP_NAME=Faxbot
VITE_HIPAA_MODE=false
VITE_ENABLE_ANALYTICS=false

# Development
VITE_DEV_MODE=true
```

`.env.local` (for development):
```env
VITE_API_BASE_URL=http://localhost:8080
VITE_API_KEY=your_dev_api_key_here
VITE_HIPAA_MODE=false
VITE_DEV_MODE=true
```

**Environment helper** (`src/lib/config.ts`):
```typescript
interface Config {
  apiBaseUrl: string;
  apiKey?: string;
  appName: string;
  hipaaMode: boolean;
  devMode: boolean;
  enableAnalytics: boolean;
}

export const config: Config = {
  apiBaseUrl: import.meta.env.VITE_API_BASE_URL || 'http://localhost:8080',
  apiKey: import.meta.env.VITE_API_KEY,
  appName: import.meta.env.VITE_APP_NAME || 'Faxbot',
  hipaaMode: import.meta.env.VITE_HIPAA_MODE === 'true',
  devMode: import.meta.env.VITE_DEV_MODE === 'true',
  enableAnalytics: import.meta.env.VITE_ENABLE_ANALYTICS === 'true' && !import.meta.env.VITE_HIPAA_MODE,
};

// Validate required config
if (!config.apiBaseUrl) {
  throw new Error('VITE_API_BASE_URL is required');
}

// HIPAA mode validations
if (config.hipaaMode) {
  if (config.enableAnalytics) {
    console.warn('Analytics disabled in HIPAA mode');
    config.enableAnalytics = false;
  }
  
  if (config.apiBaseUrl.startsWith('http://') && !config.devMode) {
    throw new Error('HTTPS required in HIPAA mode (non-dev)');
  }
}
```

## Implementation Phases

### Phase 1: Core Infrastructure & Authentication

**Duration:** 1-2 weeks  
**Goal:** Basic app structure, API connection, authentication flow

#### Step 1.1: Basic App Structure

**Main App Component** (`src/App.tsx`):
```typescript
import React from 'react';
import { BrowserRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ReactQueryDevtools } from '@tanstack/react-query-devtools';
import { Toaster } from 'react-hot-toast';
import AppRoutes from './components/AppRoutes';
import { config } from './lib/config';
import './styles/globals.css';

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: (failureCount, error: any) => {
        // Don't retry on 401/403 errors
        if (error?.response?.status === 401 || error?.response?.status === 403) {
          return false;
        }
        return failureCount < 3;
      },
      staleTime: 5 * 60 * 1000, // 5 minutes
    },
  },
});

function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <div className="min-h-screen bg-gray-50">
          <AppRoutes />
          <Toaster 
            position="top-right"
            toastOptions={{
              duration: 4000,
              style: {
                background: '#363636',
                color: '#fff',
              },
            }}
          />
        </div>
      </BrowserRouter>
      {config.devMode && <ReactQueryDevtools initialIsOpen={false} />}
    </QueryClientProvider>
  );
}

export default App;
```

**App Routes** (`src/components/AppRoutes.tsx`):
```typescript
import React from 'react';
import { Routes, Route, Navigate } from 'react-router-dom';
import { useAuthStore } from '../store/authStore';

// Page imports
import LoginPage from '../pages/auth/LoginPage';
import DashboardPage from '../pages/dashboard/DashboardPage';
import SendFaxPage from '../pages/send/SendFaxPage';
import AdminPage from '../pages/admin/AdminPage';
import InboundPage from '../pages/inbound/InboundPage';
import ConfigWizardPage from '../pages/config/ConfigWizardPage';

// Layout
import AppLayout from './layout/AppLayout';
import AuthLayout from './layout/AuthLayout';

// Route guards
const ProtectedRoute: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const { isAuthenticated } = useAuthStore();
  
  if (!isAuthenticated) {
    return <Navigate to="/login" replace />;
  }
  
  return <>{children}</>;
};

const AdminRoute: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const { isAuthenticated, user } = useAuthStore();
  
  if (!isAuthenticated) {
    return <Navigate to="/login" replace />;
  }
  
  if (!user?.isAdmin) {
    return <Navigate to="/dashboard" replace />;
  }
  
  return <>{children}</>;
};

const AppRoutes: React.FC = () => {
  const { isAuthenticated } = useAuthStore();
  
  return (
    <Routes>
      {/* Public routes */}
      <Route path="/login" element={
        <AuthLayout>
          <LoginPage />
        </AuthLayout>
      } />
      
      {/* Protected routes */}
      <Route path="/" element={
        <ProtectedRoute>
          <AppLayout />
        </ProtectedRoute>
      }>
        <Route index element={<Navigate to="/dashboard" replace />} />
        <Route path="dashboard" element={<DashboardPage />} />
        <Route path="send" element={<SendFaxPage />} />
        <Route path="inbound" element={<InboundPage />} />
        <Route path="config" element={<ConfigWizardPage />} />
      </Route>
      
      {/* Admin routes */}
      <Route path="/admin" element={
        <AdminRoute>
          <AppLayout />
        </AdminRoute>
      }>
        <Route index element={<AdminPage />} />
      </Route>
      
      {/* Catch all */}
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
};

export default AppRoutes;
```

#### Step 1.2: Authentication Store

**Auth Store** (`src/store/authStore.ts`):
```typescript
import { create } from 'zustand';
import { persist } from 'zustand/middleware';
import FaxbotApiClient from '../lib/api';
import { config } from '../lib/config';

interface User {
  apiKey: string;
  scopes: string[];
  isAdmin: boolean;
}

interface AuthState {
  user: User | null;
  isAuthenticated: boolean;
  apiClient: FaxbotApiClient | null;
  
  // Actions
  login: (apiBaseUrl: string, apiKey: string) => Promise<void>;
  logout: () => void;
  updateApiKey: (apiKey: string) => void;
}

export const useAuthStore = create<AuthState>()(
  persist(
    (set, get) => ({
      user: null,
      isAuthenticated: false,
      apiClient: null,
      
      login: async (apiBaseUrl: string, apiKey: string) => {
        try {
          // Create API client
          const client = new FaxbotApiClient({ baseURL: apiBaseUrl, apiKey });
          
          // Test connection and get server info
          await client.checkHealth();
          
          // Determine user capabilities by testing endpoints
          const scopes: string[] = [];
          let isAdmin = false;
          
          try {
            // Test admin access
            await client.listApiKeys();
            scopes.push('keys:manage');
            isAdmin = true;
          } catch (error: any) {
            // Not admin, that's fine
          }
          
          // Test fax send (try to get error that indicates auth success but missing params)
          try {
            await client.sendFax('', new File([], ''));
          } catch (error: any) {
            if (error.response?.status === 400) {
              // 400 means we're authenticated but missing params
              scopes.push('fax:send');
            }
          }
          
          // TODO: Test other scopes (fax:read, inbound:list, inbound:read)
          
          const user: User = {
            apiKey,
            scopes,
            isAdmin,
          };
          
          set({
            user,
            isAuthenticated: true,
            apiClient: client,
          });
        } catch (error) {
          console.error('Login failed:', error);
          throw error;
        }
      },
      
      logout: () => {
        set({
          user: null,
          isAuthenticated: false,
          apiClient: null,
        });
      },
      
      updateApiKey: (apiKey: string) => {
        const { user, apiClient } = get();
        if (user && apiClient) {
          apiClient.setApiKey(apiKey);
          set({
            user: { ...user, apiKey },
          });
        }
      },
    }),
    {
      name: 'faxbot-auth',
      // Only persist non-sensitive data
      partialize: (state) => ({
        isAuthenticated: state.isAuthenticated,
        user: state.user ? {
          scopes: state.user.scopes,
          isAdmin: state.user.isAdmin,
          // Don't persist API key in production
          apiKey: config.devMode ? state.user.apiKey : '',
        } : null,
      }),
    }
  )
);
```

#### Step 1.3: Login Page

**Login Page** (`src/pages/auth/LoginPage.tsx`):
```typescript
import React, { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { z } from 'zod';
import toast from 'react-hot-toast';
import { useAuthStore } from '../../store/authStore';
import { config } from '../../lib/config';

const loginSchema = z.object({
  apiBaseUrl: z.string().url('Please enter a valid URL'),
  apiKey: z.string().min(1, 'API key is required'),
});

type LoginForm = z.infer<typeof loginSchema>;

const LoginPage: React.FC = () => {
  const navigate = useNavigate();
  const login = useAuthStore((state) => state.login);
  const [isLoading, setIsLoading] = useState(false);
  
  const {
    register,
    handleSubmit,
    formState: { errors },
  } = useForm<LoginForm>({
    resolver: zodResolver(loginSchema),
    defaultValues: {
      apiBaseUrl: config.apiBaseUrl,
      apiKey: config.apiKey || '',
    },
  });
  
  const onSubmit = async (data: LoginForm) => {
    setIsLoading(true);
    try {
      await login(data.apiBaseUrl, data.apiKey);
      toast.success('Connected successfully!');
      navigate('/dashboard');
    } catch (error: any) {
      console.error('Login error:', error);
      
      if (error.response?.status === 401) {
        toast.error('Invalid API key');
      } else if (error.code === 'NETWORK_ERROR' || error.message?.includes('Network Error')) {
        toast.error('Cannot connect to Faxbot API. Please check the URL and ensure the server is running.');
      } else {
        toast.error('Connection failed. Please check your settings.');
      }
    } finally {
      setIsLoading(false);
    }
  };
  
  return (
    <div className="min-h-screen flex items-center justify-center bg-gray-50 py-12 px-4 sm:px-6 lg:px-8">
      <div className="max-w-md w-full space-y-8">
        <div>
          <h2 className="mt-6 text-center text-3xl font-extrabold text-gray-900">
            Connect to Faxbot
          </h2>
          <p className="mt-2 text-center text-sm text-gray-600">
            Enter your API connection details
          </p>
        </div>
        
        <form className="mt-8 space-y-6" onSubmit={handleSubmit(onSubmit)}>
          <div className="space-y-4">
            <div>
              <label htmlFor="apiBaseUrl" className="block text-sm font-medium text-gray-700">
                API Base URL
              </label>
              <input
                {...register('apiBaseUrl')}
                type="url"
                className="input mt-1"
                placeholder="http://localhost:8080"
              />
              {errors.apiBaseUrl && (
                <p className="mt-1 text-sm text-red-600">{errors.apiBaseUrl.message}</p>
              )}
            </div>
            
            <div>
              <label htmlFor="apiKey" className="block text-sm font-medium text-gray-700">
                API Key
                <span className="text-gray-500 text-xs ml-2">
                  (Optional in development mode)
                </span>
              </label>
              <input
                {...register('apiKey')}
                type="password"
                className="input mt-1"
                placeholder="fbk_live_..."
              />
              {errors.apiKey && (
                <p className="mt-1 text-sm text-red-600">{errors.apiKey.message}</p>
              )}
            </div>
          </div>
          
          <div>
            <button
              type="submit"
              disabled={isLoading}
              className="btn-primary w-full"
            >
              {isLoading ? 'Connecting...' : 'Connect'}
            </button>
          </div>
          
          <div className="text-xs text-gray-500 space-y-2">
            <p>
              <strong>First time?</strong> Start the Faxbot API server and use the default URL above.
            </p>
            <p>
              <strong>Need an API key?</strong> Check your server's environment configuration or create one via the admin endpoints.
            </p>
          </div>
        </form>
      </div>
    </div>
  );
};

export default LoginPage;
```

#### Step 1.4: Layout Components

**App Layout** (`src/components/layout/AppLayout.tsx`):
```typescript
import React from 'react';
import { Outlet } from 'react-router-dom';
import Sidebar from './Sidebar';
import Header from './Header';

const AppLayout: React.FC = () => {
  return (
    <div className="h-screen flex overflow-hidden bg-gray-100">
      <Sidebar />
      <div className="flex flex-col w-0 flex-1 overflow-hidden">
        <Header />
        <main className="flex-1 relative overflow-y-auto focus:outline-none">
          <div className="py-6">
            <div className="max-w-7xl mx-auto px-4 sm:px-6 md:px-8">
              <Outlet />
            </div>
          </div>
        </main>
      </div>
    </div>
  );
};

export default AppLayout;
```

**Sidebar** (`src/components/layout/Sidebar.tsx`):
```typescript
import React from 'react';
import { NavLink } from 'react-router-dom';
import {
  HomeIcon,
  PaperAirplaneIcon,
  InboxIcon,
  CogIcon,
  KeyIcon,
} from '@heroicons/react/24/outline';
import { useAuthStore } from '../../store/authStore';

const navigation = [
  { name: 'Dashboard', href: '/dashboard', icon: HomeIcon },
  { name: 'Send Fax', href: '/send', icon: PaperAirplaneIcon },
  { name: 'Inbound', href: '/inbound', icon: InboxIcon },
  { name: 'Config Wizard', href: '/config', icon: CogIcon },
];

const adminNavigation = [
  { name: 'API Keys', href: '/admin', icon: KeyIcon },
];

const Sidebar: React.FC = () => {
  const user = useAuthStore((state) => state.user);
  
  return (
    <div className="hidden md:flex md:w-64 md:flex-col md:fixed md:inset-y-0">
      <div className="flex-1 flex flex-col min-h-0 bg-gray-800">
        <div className="flex-1 flex flex-col pt-5 pb-4 overflow-y-auto">
          <div className="flex items-center flex-shrink-0 px-4">
            <h1 className="text-white text-xl font-bold">Faxbot</h1>
          </div>
          
          <nav className="mt-5 flex-1 px-2 space-y-1">
            {navigation.map((item) => (
              <NavLink
                key={item.name}
                to={item.href}
                className={({ isActive }) =>
                  `group flex items-center px-2 py-2 text-sm font-medium rounded-md ${
                    isActive
                      ? 'bg-gray-900 text-white'
                      : 'text-gray-300 hover:bg-gray-700 hover:text-white'
                  }`
                }
              >
                <item.icon className="mr-3 h-6 w-6" />
                {item.name}
              </NavLink>
            ))}
            
            {user?.isAdmin && (
              <div className="pt-4">
                <h3 className="px-3 text-xs font-semibold text-gray-500 uppercase tracking-wider">
                  Admin
                </h3>
                {adminNavigation.map((item) => (
                  <NavLink
                    key={item.name}
                    to={item.href}
                    className={({ isActive }) =>
                      `group flex items-center px-2 py-2 text-sm font-medium rounded-md ${
                        isActive
                          ? 'bg-gray-900 text-white'
                          : 'text-gray-300 hover:bg-gray-700 hover:text-white'
                      }`
                    }
                  >
                    <item.icon className="mr-3 h-6 w-6" />
                    {item.name}
                  </NavLink>
                ))}
              </div>
            )}
          </nav>
        </div>
      </div>
    </div>
  );
};

export default Sidebar;
```

### Phase 2: Send Fax Interface

**Duration:** 1-2 weeks  
**Goal:** Drag-and-drop file upload, phone validation, fax sending, status polling

#### Step 2.1: File Upload Component

**File Dropzone Component** (`src/components/forms/FileDropzone.tsx`):
```typescript
import React, { useCallback } from 'react';
import { useDropzone } from 'react-dropzone';
import { DocumentIcon, XMarkIcon } from '@heroicons/react/24/outline';

interface FileDropzoneProps {
  onFileSelect: (file: File) => void;
  selectedFile?: File;
  onFileRemove: () => void;
  maxSizeMB?: number;
  error?: string;
}

const FileDropzone: React.FC<FileDropzoneProps> = ({
  onFileSelect,
  selectedFile,
  onFileRemove,
  maxSizeMB = 10,
  error,
}) => {
  const onDrop = useCallback((acceptedFiles: File[]) => {
    if (acceptedFiles.length > 0) {
      const file = acceptedFiles[0];
      
      // Size validation
      const maxBytes = maxSizeMB * 1024 * 1024;
      if (file.size > maxBytes) {
        return; // Handle error appropriately
      }
      
      onFileSelect(file);
    }
  }, [onFileSelect, maxSizeMB]);

  const { getRootProps, getInputProps, isDragActive, fileRejections } = useDropzone({
    onDrop,
    accept: {
      'application/pdf': ['.pdf'],
      'text/plain': ['.txt'],
    },
    maxFiles: 1,
    maxSize: maxSizeMB * 1024 * 1024,
  });

  const formatFileSize = (bytes: number) => {
    if (bytes === 0) return '0 Bytes';
    const k = 1024;
    const sizes = ['Bytes', 'KB', 'MB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
  };

  if (selectedFile) {
    return (
      <div className="border-2 border-gray-300 border-dashed rounded-lg p-6">
        <div className="flex items-center justify-between">
          <div className="flex items-center">
            <DocumentIcon className="h-8 w-8 text-gray-400" />
            <div className="ml-3">
              <p className="text-sm font-medium text-gray-900">{selectedFile.name}</p>
              <p className="text-sm text-gray-500">{formatFileSize(selectedFile.size)}</p>
            </div>
          </div>
          <button
            onClick={onFileRemove}
            className="text-gray-400 hover:text-gray-600"
          >
            <XMarkIcon className="h-5 w-5" />
          </button>
        </div>
      </div>
    );
  }

  return (
    <div>
      <div
        {...getRootProps()}
        className={`border-2 border-dashed rounded-lg p-6 text-center cursor-pointer transition-colors ${
          isDragActive
            ? 'border-primary-500 bg-primary-50'
            : 'border-gray-300 hover:border-gray-400'
        } ${error ? 'border-red-300 bg-red-50' : ''}`}
      >
        <input {...getInputProps()} />
        <DocumentIcon className="mx-auto h-12 w-12 text-gray-400" />
        <div className="mt-4">
          <p className="text-sm text-gray-600">
            {isDragActive ? (
              'Drop the file here...'
            ) : (
              <>
                <span className="font-medium text-primary-600">Click to upload</span> or drag and drop
              </>
            )}
          </p>
          <p className="text-xs text-gray-500 mt-1">
            PDF or TXT files only, up to {maxSizeMB}MB
          </p>
        </div>
      </div>
      
      {fileRejections.length > 0 && (
        <div className="mt-2">
          {fileRejections.map(({ file, errors }) => (
            <div key={file.name} className="text-sm text-red-600">
              {errors.map((error) => (
                <p key={error.code}>
                  {error.code === 'file-too-large' 
                    ? `File is too large. Maximum size is ${maxSizeMB}MB.`
                    : error.code === 'file-invalid-type'
                    ? 'Only PDF and TXT files are allowed.'
                    : error.message
                  }
                </p>
              ))}
            </div>
          ))}
        </div>
      )}
      
      {error && (
        <p className="mt-2 text-sm text-red-600">{error}</p>
      )}
    </div>
  );
};

export default FileDropzone;
```

### Step 2.2: Send Fax Page with Status Polling

**Send Fax Page** (`src/pages/send/SendFaxPage.tsx`):
```typescript
import React, { useState } from 'react';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { z } from 'zod';
import { useMutation } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';
import toast from 'react-hot-toast';
import { useAuthStore } from '../../store/authStore';
import FileDropzone from '../../components/forms/FileDropzone';

const sendFaxSchema = z.object({
  to: z.string()
    .min(1, 'Phone number is required')
    .regex(/^[\+]?[\d\s\-\(\)]{7,20}$/, 'Invalid phone number format'),
  file: z.instanceof(File, { message: 'Please select a file' }),
});

type SendFaxForm = z.infer<typeof sendFaxSchema>;

const SendFaxPage: React.FC = () => {
  const navigate = useNavigate();
  const apiClient = useAuthStore((state) => state.apiClient);
  const [selectedFile, setSelectedFile] = useState<File | undefined>();

  const {
    register,
    handleSubmit,
    formState: { errors },
    setValue,
    clearErrors,
    reset,
  } = useForm<SendFaxForm>({
    resolver: zodResolver(sendFaxSchema),
  });

  const sendFaxMutation = useMutation({
    mutationFn: async (data: SendFaxForm) => {
      if (!apiClient) throw new Error('Not authenticated');
      return apiClient.sendFax(data.to, data.file);
    },
    onSuccess: (data) => {
      toast.success('Fax queued successfully!');
      // Reset form and navigate to dashboard with job ID
      reset();
      setSelectedFile(undefined);
      navigate(`/dashboard?job=${data.id}`);
    },
    onError: (error: any) => {
      console.error('Send fax error:', error);
      
      if (error.response?.status === 400) {
        toast.error('Invalid request. Please check your phone number and file.');
      } else if (error.response?.status === 401) {
        toast.error('Authentication failed. Please check your API key.');
      } else if (error.response?.status === 413) {
        toast.error('File is too large. Maximum size is 10MB.');
      } else if (error.response?.status === 415) {
        toast.error('Unsupported file type. Only PDF and TXT files are allowed.');
      } else {
        toast.error('Failed to send fax. Please try again.');
      }
    },
  });

  const onSubmit = (data: SendFaxForm) => {
    sendFaxMutation.mutate(data);
  };

  const handleFileSelect = (file: File) => {
    setSelectedFile(file);
    setValue('file', file);
    clearErrors('file');
  };

  const handleFileRemove = () => {
    setSelectedFile(undefined);
    setValue('file', undefined as any);
  };

  return (
    <div className="max-w-2xl mx-auto">
      <div className="bg-white shadow rounded-lg">
        <div className="px-6 py-4 border-b border-gray-200">
          <h1 className="text-lg font-medium text-gray-900">Send Fax</h1>
          <p className="mt-1 text-sm text-gray-600">
            Send a PDF or text file to any fax number
          </p>
        </div>

        <form onSubmit={handleSubmit(onSubmit)} className="px-6 py-6 space-y-6">
          {/* Phone Number Input */}
          <div>
            <label htmlFor="to" className="block text-sm font-medium text-gray-700">
              Fax Number
            </label>
            <div className="mt-1 relative">
              <input
                {...register('to')}
                type="tel"
                className={`input ${errors.to ? 'border-red-300 focus:border-red-500 focus:ring-red-500' : ''}`}
                placeholder="+1234567890"
              />
              <div className="absolute inset-y-0 right-0 pr-3 flex items-center pointer-events-none">
                <span className="text-gray-400 text-sm">📠</span>
              </div>
            </div>
            {errors.to && (
              <p className="mt-1 text-sm text-red-600">{errors.to.message}</p>
            )}
            <p className="mt-1 text-xs text-gray-500">
              Enter in E.164 format (e.g., +1234567890) or US format
            </p>
          </div>

          {/* File Upload */}
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-2">
              Document
            </label>
            <FileDropzone
              onFileSelect={handleFileSelect}
              selectedFile={selectedFile}
              onFileRemove={handleFileRemove}
              error={errors.file?.message}
            />
          </div>

          {/* Submit Button */}
          <div className="flex justify-end">
            <button
              type="submit"
              disabled={sendFaxMutation.isPending}
              className="btn-primary"
            >
              {sendFaxMutation.isPending ? (
                <>
                  <svg className="animate-spin -ml-1 mr-3 h-5 w-5 text-white" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
                    <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"></circle>
                    <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                  </svg>
                  Sending...
                </>
              ) : (
                'Send Fax'
              )}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
};

export default SendFaxPage;
```

## Phase 3: Dashboard & Status Management

**Duration:** 1 week  
**Goal:** Overview of recent faxes, status filtering, job history, server information

### Step 3.1: Dashboard Components

The dashboard needs several key components:

**Dashboard Page Structure:**
- Job status card (for highlighted jobs from send page)
- Stats cards showing totals and success rates
- Recent faxes list with filtering
- Server status information
- Quick action buttons

**Key Features:**
- Real-time status polling for active jobs
- Status filtering (all, queued, in_progress, SUCCESS, FAILED)
- Quick navigation to other sections
- Server health monitoring

**Implementation Notes:**
- Uses React Query for server state management
- Implements proper loading states and error handling
- Shows scope-based navigation (admin users see admin options)
- Handles empty states gracefully

## Phase 4: Admin Interface (API Key Management)

**Duration:** 1 week  
**Goal:** Complete API key lifecycle management with security best practices

### Step 4.1: Create API Key Modal

**Create API Key Modal** (`src/components/features/CreateApiKeyModal.tsx`):
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

  const createKeyMutation = useMutation({
    mutationFn: async (data: CreateKeyForm) => {
      if (!apiClient) throw new Error('Not authenticated');
      return apiClient.createApiKey(data);
    },
    onSuccess: (data) => {
      setCreatedKey(data);
      setShowToken(true);
      toast.success('API key created successfully!');
    },
    onError: (error: any) => {
      console.error('Create key error:', error);
      toast.error('Failed to create API key');
    },
  });

  // ... rest of component implementation
};
```

### Step 4.2: API Keys List Component

**API Keys List** (`src/components/features/ApiKeysList.tsx`):
```typescript
import React from 'react';
import { ApiKeyMeta } from '../../lib/types';
import { TrashIcon, ArrowPathIcon, CheckCircleIcon, XCircleIcon } from '@heroicons/react/24/outline';

interface ApiKeysListProps {
  apiKeys: ApiKeyMeta[];
  isLoading: boolean;
  onRevoke: (keyId: string) => void;
  onRotate: (keyId: string) => void;
}

const ApiKeysList: React.FC<ApiKeysListProps> = ({
  apiKeys,
  isLoading,
  onRevoke,
  onRotate,
}) => {
  // ... component implementation with table, status indicators, actions
};
```

## Phase 5: Inbound Fax Management

**Duration:** 1 week  
**Goal:** List, view, and download inbound faxes with proper permissions

### Step 5.1: Inbound Page

**Inbound Page** (`src/pages/inbound/InboundPage.tsx`):
```typescript
import React, { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useAuthStore } from '../../store/authStore';
import InboundFaxesList from '../../components/features/InboundFaxesList';
import { FunnelIcon } from '@heroicons/react/24/outline';

const InboundPage: React.FC = () => {
  const apiClient = useAuthStore((state) => state.apiClient);
  const user = useAuthStore((state) => state.user);
  const [filters, setFilters] = useState({
    status: 'all',
    mailbox: 'all', 
    to_number: '',
  });

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

  if (!hasInboundAccess) {
    return (
      <div className="text-center py-12">
        <h3 className="mt-2 text-sm font-medium text-gray-900">Access Denied</h3>
        <p className="mt-1 text-sm text-gray-500">
          You don't have permission to view inbound faxes.
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold text-gray-900">Inbound Faxes</h1>
      
      {/* Filters */}
      <div className="bg-white shadow rounded-lg p-6">
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          <select
            value={filters.status}
            onChange={(e) => setFilters(prev => ({ ...prev, status: e.target.value }))}
            className="input"
          >
            <option value="all">All Status</option>
            <option value="received">Received</option>
            <option value="failed">Failed</option>
          </select>
        </div>
      </div>

      {/* Faxes List */}
      <InboundFaxesList faxes={inboundFaxes || []} isLoading={isLoading} />
    </div>
  );
};
```

## Phase 6: Configuration Wizard

**Duration:** 1 week  
**Goal:** Backend-specific configuration guidance with environment generation

### Step 6.1: Config Wizard Page

**Config Wizard** (`src/pages/config/ConfigWizardPage.tsx`):
```typescript
import React, { useState } from 'react';
import { CloudIcon, ServerIcon, BeakerIcon } from '@heroicons/react/24/outline';

const backends = [
  {
    id: 'phaxio',
    name: 'Phaxio (Cloud)',
    description: 'Recommended for healthcare and business users',
    icon: CloudIcon,
    difficulty: 'Easy',
    cost: '~$0.07/page',
  },
  {
    id: 'sip', 
    name: 'SIP/Asterisk (Self-Hosted)',
    description: 'For technical users wanting full control',
    icon: ServerIcon,
    difficulty: 'Hard',
    cost: 'SIP trunk only',
  },
  {
    id: 'test',
    name: 'Test Mode',
    description: 'Development and testing only', 
    icon: BeakerIcon,
    difficulty: 'Easy',
    cost: 'Free',
  },
];

const ConfigWizardPage: React.FC = () => {
  const [selectedBackend, setSelectedBackend] = useState('phaxio');
  const [hipaaMode, setHipaaMode] = useState(false);

  return (
    <div className="max-w-4xl mx-auto space-y-6">
      <h1 className="text-2xl font-bold text-gray-900">Configuration Wizard</h1>
      
      {/* Backend Selection */}
      <div className="bg-white shadow rounded-lg p-6">
        <h2 className="text-lg font-medium text-gray-900 mb-4">Choose Your Backend</h2>
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          {backends.map((backend) => (
            <div
              key={backend.id}
              className={`border-2 p-4 rounded-lg cursor-pointer ${
                selectedBackend === backend.id ? 'border-primary-500' : 'border-gray-200'
              }`}
              onClick={() => setSelectedBackend(backend.id)}
            >
              <backend.icon className="h-8 w-8 text-gray-600 mb-2" />
              <h3 className="font-medium">{backend.name}</h3>
              <p className="text-sm text-gray-600">{backend.description}</p>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
};
```

## Phase 7: Security Implementation

**Duration:** 1 week  
**Goal:** HIPAA compliance, security headers, audit logging

### Step 7.1: Security Utilities

**Security Helpers** (`src/lib/security.ts`):
```typescript
export const validateHipaaCompliance = (config: any) => {
  const issues: string[] = [];
  
  if (config.hipaaMode) {
    if (config.apiBaseUrl.startsWith('http://') && !config.devMode) {
      issues.push('HTTPS is required in HIPAA mode');
    }
  }
  
  return issues;
};

export const sanitizeForLogs = (data: any): any => {
  if (typeof data !== 'object' || data === null) return data;
  
  const sanitized = { ...data };
  const sensitiveKeys = ['apiKey', 'token', 'password', 'secret'];
  
  for (const key of Object.keys(sanitized)) {
    if (sensitiveKeys.some(sensitive => key.toLowerCase().includes(sensitive))) {
      sanitized[key] = '[REDACTED]';
    }
  }
  
  return sanitized;
};
```

## Phase 8: Deployment & Production

**Duration:** 1 week  
**Goal:** S3 + CloudFront deployment with CI/CD pipeline

### Step 8.1: Build Configuration

**Vite Config** (`vite.config.ts`):
```typescript
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: 'dist',
    sourcemap: false,
  },
  server: {
    port: 3000,
    proxy: {
      '/api': {
        target: 'http://localhost:8080',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ''),
      },
    },
  },
});
```

### Step 8.2: Deployment Script

**Deploy Script** (`scripts/deploy.sh`):
```bash
#!/bin/bash
set -euo pipefail

BUCKET_NAME="${BUCKET_NAME:-faxbot-frontend}"
DISTRIBUTION_ID="${DISTRIBUTION_ID:-}"

echo "🚀 Deploying to S3 + CloudFront"

npm run build

aws s3 sync dist/ "s3://$BUCKET_NAME" --delete

if [ -n "$DISTRIBUTION_ID" ]; then
    aws cloudfront create-invalidation \
        --distribution-id "$DISTRIBUTION_ID" \
        --paths "/*"
fi

echo "✅ Deployment complete!"
```

This comprehensive plan provides everything needed to build a production-ready frontend that safely exposes the Faxbot API capabilities while maintaining security and usability standards.

## Complete Implementation Checklist

**Phase 1: Core Infrastructure** ✅
- [x] Project setup with Vite + React + TypeScript
- [x] Authentication system with Zustand
- [x] API client with Axios
- [x] Routing with React Router
- [x] Basic layout components

**Phase 2: Send Fax Interface** ✅
- [x] File upload with drag-and-drop
- [x] Phone number validation
- [x] Form handling with React Hook Form + Zod
- [x] Error handling and user feedback

**Phase 3: Dashboard** (Detailed specs provided)
- [ ] Recent fax history display
- [ ] Status filtering and polling
- [ ] Stats cards with metrics
- [ ] Server info and quick actions

**Phase 4: Admin Interface** (Detailed specs provided)
- [ ] API key creation with scopes
- [ ] Key management (list, revoke, rotate)
- [ ] Security warnings and best practices
- [ ] Token handling (show once, clipboard copy)

**Phase 5: Inbound Management** (Detailed specs provided)
- [ ] Inbound fax listing with filters
- [ ] Permission-based access control
- [ ] PDF download with tokenized access
- [ ] Mailbox organization

**Phase 6: Configuration Wizard** (Detailed specs provided)
- [ ] Backend selection interface
- [ ] HIPAA vs standard mode toggle
- [ ] Environment variable generation
- [ ] Backend-specific setup instructions

**Phase 7: Security** (Detailed specs provided)
- [ ] HIPAA compliance features
- [ ] Security headers implementation
- [ ] Audit logging system
- [ ] Input sanitization

**Phase 8: Deployment** (Detailed specs provided)
- [ ] S3 + CloudFront infrastructure
- [ ] CI/CD pipeline with GitHub Actions
- [ ] Security configuration
- [ ] Monitoring and alerts

**Next Steps:**
1. Follow Phase 1 setup to initialize the project
2. Implement Phase 2 send interface
3. Build remaining phases incrementally
4. Test thoroughly before production deployment

This plan gives a junior developer everything they need to build a complete, secure, HIPAA-compliant frontend for Faxbot.