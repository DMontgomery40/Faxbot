export interface AnalysisSettings {
  enabled: boolean;
  provider: 'openai' | 'openrouter' | 'compatible';
  base_url: string;
  model: string;
  api_key: string;
  interval_hours: number;
  configured: boolean;
}

export interface AnalysisStatus {
  configured: boolean;
  enabled: boolean;
  state: 'not_configured' | 'disabled' | 'idle' | 'queued' | 'running' | 'succeeded' | 'failed';
  message: string | null;
  last_run: {
    id: string;
    started_at: string;
    finished_at: string | null;
    provider: string;
    model: string;
    summary: string | null;
    evidence: Array<Record<string, unknown>>;
    usage: Record<string, unknown>;
    error: string | null;
  } | null;
  next_run_at: string | null;
  stale: boolean;
}
