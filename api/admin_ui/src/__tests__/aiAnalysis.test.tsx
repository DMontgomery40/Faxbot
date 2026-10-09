import { describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import { NAVIGATION, type PageContext } from '../navigation';
import { server } from '../test/server';
import { receipt, settingsFixture } from '../test/settingsFixture';

type Json = Record<string, any>;
const idle = { configured: true, enabled: true, state: 'idle', message: null, last_run: null,
  next_run_at: null, stale: false };
function setup(options: { owner?: boolean; settings?: Json; status?: Json; managed?: boolean } = {}) {
  const data = settingsFixture();
  data.analysis = { enabled: true, provider: 'openai', base_url: 'https://api.openai.com/v1', model: 'chosen-model',
    api_key: '***', interval_hours: 24, configured: true, ...options.settings };
  if (options.managed) data._meta.env_managed = ['analysis_api_key'];
  let status = options.status ?? idle;
  const writes: Json[] = [];
  const actions: string[] = [];
  server.use(
    http.get('/admin/settings', () => HttpResponse.json(data)),
    http.put('/admin/settings', async ({ request }) => {
      const body = await request.json() as Json;
      writes.push(body);
      for (const [key, value] of Object.entries(body)) {
        if (key.startsWith('analysis_')) data.analysis[key.slice(9)] = key === 'analysis_api_key' && value ? '***' : value;
      }
      data._meta.desired_revision_id = 'rev-b';
      return HttpResponse.json(receipt('rev-b'));
    }),
    http.get('/analysis', () => HttpResponse.json(status)),
    http.post('/analysis/run', () => { actions.push('run'); status = { ...idle, state: 'queued' }; return HttpResponse.json(status); }),
    http.post('/analysis/test', () => { actions.push('test'); return HttpResponse.json({ ok: true, message: 'Connection works.' }); }),
  );
  const page = NAVIGATION.find((area) => area.id === 'system')?.pages.find((entry) => entry.id === 'analysis');
  const ctx = { client: new AdminAPIClient({ kind: 'key', key: 'synthetic-key' }),
    me: { is_owner: options.owner !== false, principal: { kind: 'user' } },
    permissions: new Set(['settings:read', 'settings:write']), navigate: () => undefined } as unknown as PageContext;
  render(<>{page?.render(ctx)}</>);
  return { writes, actions, setStatus: (next: Json) => { status = next; } };
}

describe('runtime AI analysis', () => {
  it('preserves a saved key when only the model and schedule change, with the loaded revision', async () => {
    const { writes } = setup();
    fireEvent.change(await screen.findByLabelText('Model'), { target: { value: 'another-model' } });
    fireEvent.change(screen.getByLabelText('Hours between analyses'), { target: { value: '0' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save settings' }));
    await waitFor(() => expect(writes).toEqual([{ expected_revision_id: 'rev-a', analysis_model: 'another-model',
      analysis_interval_hours: 0 }]));
    expect((screen.getByLabelText('API key') as HTMLInputElement).value).toBe('');
  });
  it('lets the owner replace or explicitly clear a key without writing its mask', async () => {
    const { writes } = setup();
    fireEvent.change(await screen.findByLabelText('API key'), { target: { value: 'synthetic-replacement' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save settings' }));
    await waitFor(() => expect(writes[0]).toEqual({ expected_revision_id: 'rev-a', analysis_api_key: 'synthetic-replacement' }));
    await screen.findByText('Settings saved.');
    fireEvent.click(screen.getByRole('checkbox', { name: 'Remove saved API key' }));
    fireEvent.click(screen.getByRole('button', { name: 'Save settings' }));
    await waitFor(() => expect(writes[1]).toEqual({ expected_revision_id: 'rev-b', analysis_api_key: '' }));
  });
  it('keeps configuration and paid actions unavailable to a reader', async () => {
    const { writes, actions } = setup({ owner: false });
    expect((await screen.findByLabelText('Model') as HTMLInputElement).disabled).toBe(true);
    expect(screen.queryByRole('button', { name: 'Save settings' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Test connection' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Run analysis' })).toBeNull();
    expect(writes).toEqual([]);
    expect(actions).toEqual([]);
  });
  it('explains missing configuration and does not offer a run', async () => {
    setup({ settings: { api_key: '', model: '', configured: false, enabled: false },
      status: { ...idle, configured: false, enabled: false, state: 'not_configured' } });
    await screen.findByLabelText('Model');
    expect((screen.getByRole('button', { name: 'Run analysis' }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText(/Add your provider, model and API key/)).toBeTruthy();
  });
  it('tests the saved configuration and queues manual analysis', async () => {
    const { actions } = setup();
    fireEvent.click(await screen.findByRole('button', { name: 'Test connection' }));
    await screen.findByText('Connection works.');
    fireEvent.click(screen.getByRole('button', { name: 'Run analysis' }));
    await screen.findByText(/Analysis is queued/);
    expect(actions).toEqual(['test', 'run']);
    expect((screen.getByRole('button', { name: 'Run analysis' }) as HTMLButtonElement).disabled).toBe(true);
  });
  it('shows stale plain-text results and provider failures without hiding the last result', async () => {
    setup({ status: { ...idle, state: 'failed', stale: true, message: 'The provider is unavailable.',
      last_run: { id: 'run-a', provider: 'openai', model: 'chosen-model', started_at: '2026-10-09T01:00:00',
        finished_at: '2026-10-09T01:00:04', summary: '<img src=x onerror=alert(1)> Review the recorded costs.',
        evidence: [], usage: {}, error: 'The provider is unavailable.' } } });
    await screen.findByText(/<img src=x onerror=alert\(1\)> Review the recorded costs/);
    expect(document.querySelector('img[src="x"]')).toBeNull();
    expect(screen.getByText(/older evidence or settings/)).toBeTruthy();
    expect(screen.getAllByText(/provider is unavailable/).length).toBeGreaterThan(0);
    expect(screen.getByText(/Last completed/)).toBeTruthy();
  });
});

it('keeps polling through a temporary status failure and renders the completed result', async () => {
  const api = new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });
  let reads = 0;
  server.use(http.get('/analysis', () => {
    reads += 1;
    if (reads === 1) return HttpResponse.json({ ...idle, state: 'running' });
    if (reads < 4) return new HttpResponse(null, { status: 503 });
    return HttpResponse.json({ ...idle, state: 'succeeded', last_run: { id: 'finished', provider: 'openai',
      model: 'chosen-model', started_at: '2026-10-09T01:00:00', finished_at: '2026-10-09T01:00:04',
      summary: 'Your recorded charges need review.', evidence: [{ tool: 'delivery', label: 'Delivery outcomes',
        data: { sent: 12 }, collected_at: '2026-10-09T01:00:00' }], usage: {}, error: null } });
  }));
  const { AnalysisCard } = await import('../components/AIAnalysis');
  render(<AnalysisCard client={api} pollMs={10} />);
  await screen.findByText('Your recorded charges need review.');
  expect(screen.getByText('Delivery outcomes')).toBeTruthy();
});

it('opens AI settings from the recommendations screen', async () => {
  const { default: Recommendations } = await import('../components/delivery/Recommendations');
  server.use(http.get('/analysis', () => HttpResponse.json({ ...idle, enabled: false, state: 'disabled' })));
  render(<Recommendations client={new AdminAPIClient({ kind: 'key', key: 'synthetic-key' })} />);
  fireEvent.click(await screen.findByRole('button', { name: 'AI analysis settings' }));
  expect(window.location.hash).toBe('#/system/analysis');
});

it('keeps a rejected revision visible and prevents a second blind save', async () => {
  setup();
  server.use(http.put('/admin/settings', () => HttpResponse.json({ detail: 'Revision changed.' }, { status: 409 })));
  const model = await screen.findByLabelText('Model');
  fireEvent.change(model, { target: { value: 'my-new-model' } });
  fireEvent.click(screen.getByRole('button', { name: 'Save settings' }));
  await screen.findByText(/Someone else changed the settings/);
  expect((model as HTMLInputElement).value).toBe('my-new-model');
  expect((screen.getByRole('button', { name: 'Save settings' }) as HTMLButtonElement).disabled).toBe(true);
});


it('uses the chosen provider address and keeps the key out of the unrelated write', async () => {
  const { writes } = setup();
  fireEvent.mouseDown(await screen.findByRole('combobox', { name: 'AI provider' }));
  fireEvent.click(within(await screen.findByRole('listbox')).getByRole('option', { name: 'OpenRouter' }));
  expect((screen.getByLabelText('Service address') as HTMLInputElement).value).toBe('https://openrouter.ai/api/v1');
  fireEvent.click(screen.getByRole('button', { name: 'Save settings' }));
  await waitFor(() => expect(writes[0]).toEqual({ expected_revision_id: 'rev-a', analysis_provider: 'openrouter',
    analysis_base_url: 'https://openrouter.ai/api/v1' }));
});

it('shows an environment-supplied key as read-only and never offers to remove it', async () => {
  const { writes } = setup({ managed: true });
  expect((await screen.findByLabelText('API key') as HTMLInputElement).disabled).toBe(true);
  expect(screen.queryByRole('checkbox', { name: 'Remove saved API key' })).toBeNull();
  fireEvent.change(screen.getByLabelText('Hours between analyses'), { target: { value: '169' } });
  expect((screen.getByRole('button', { name: 'Save settings' }) as HTMLButtonElement).disabled).toBe(true);
  expect(writes).toEqual([]);
});


it.each(['idle', 'succeeded'])('refreshes a %s card, pauses when hidden and catches up on return', async (state) => {
  const { AnalysisCard } = await import('../components/AIAnalysis');
  const client = new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });
  const call = vi.spyOn(client, 'call').mockResolvedValue({ ...idle, state });
  const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible');
  const completed = (summary: string) => ({ ...idle, state: 'succeeded', stale: true,
    last_run: { id: 'scheduled', provider: 'openai', model: 'chosen-model', started_at: '2026-10-09T01:00:00',
      finished_at: '2026-10-09T01:00:04', summary, evidence: [], usage: {}, error: null } });
  vi.useFakeTimers();
  let view: ReturnType<typeof render> | undefined;
  try {
    await act(async () => { view = render(<AnalysisCard client={client} />); });
    call.mockResolvedValue(completed('A scheduled report is ready.'));
    await act(async () => { await vi.advanceTimersByTimeAsync(60000); });
    expect(screen.getByText('A scheduled report is ready.')).toBeTruthy();
    expect(screen.getByText(/older evidence or settings/)).toBeTruthy();
    visibility.mockReturnValue('hidden');
    fireEvent(document, new Event('visibilitychange'));
    call.mockResolvedValue(completed('The newest scheduled report is ready.'));
    await act(async () => { await vi.advanceTimersByTimeAsync(120000); });
    expect(screen.queryByText('The newest scheduled report is ready.')).toBeNull();
    visibility.mockReturnValue('visible');
    await act(async () => { fireEvent(document, new Event('visibilitychange')); });
    expect(screen.getByText('The newest scheduled report is ready.')).toBeTruthy();
  } finally {
    view?.unmount();
    vi.useRealTimers();
    vi.restoreAllMocks();
  }
});
