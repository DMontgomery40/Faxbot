export type FaxStatus = 'queued' | 'in_progress' | 'SUCCESS' | 'FAILED' | 'cancelled';
export interface SendResult { job_id: string; backend: string; provider_sid?: string; estimated_cost?: number; estimated_duration?: number; metadata?: Record<string, any>; }
export interface StatusResult { job_id: string; status: FaxStatus | string; pages?: number; duration?: number; cost?: number; error?: string; error_code?: string; completed_at?: string; raw_response?: Record<string, any> | null; }
export interface PluginManifest { id: string; name: string; version: string; description: string; author: string; categories: string[]; capabilities: string[]; homepage?: string; license?: string; icon?: string; hipaa_compliant?: boolean; requires_baa?: boolean; config_schema?: Record<string, any>; }
export interface PluginDeps { logger: any; storage: any; db: any; events: any; http: any; audit: any; config_dir: string; cache: any; metrics: any; extras?: Record<string, any>; }
export class PluginBase { constructor(deps?: Partial<PluginDeps>); deps: Partial<PluginDeps>; config: Record<string, any>; _initialized: boolean; manifest(): PluginManifest; validateConfig(config: Record<string, any>): void; initialize(config: Record<string, any>): Promise<void>; shutdown(): Promise<void>; healthCheck(): Promise<{ status: string; plugin: string; version: string }>; }
export class FaxPlugin extends PluginBase { sendFax(to: string, filePath: string, options?: Record<string, any>): Promise<SendResult>; getStatus(jobId: string): Promise<StatusResult>; }
export class StoragePlugin extends PluginBase { put(path: string, data: Buffer, metadata?: Record<string, any>): Promise<string>; get(path: string): Promise<Buffer>; delete(path: string): Promise<boolean>; exists(path: string): Promise<boolean>; list(prefix?: string): Promise<string[]>; }
export class AuthPlugin extends PluginBase { authenticate(credentials: Record<string, any>): Promise<Record<string, any>>; validateToken(token: string): Promise<boolean>; refreshToken(refreshToken: string): Promise<Record<string, any>>; }
export function maskPhoneNumber(phone: string): string;
export function hashDocument(data: Buffer): string;
export function generateToken(length?: number): string;
export function redactText(text: string): string;
export function timestamp(): string;

