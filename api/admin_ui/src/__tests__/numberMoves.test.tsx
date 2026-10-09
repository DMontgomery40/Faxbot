import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import AdminAPIClient from '../api/client';
import NumberMoves from '../components/NumberMoves';

const number = '+15550001001';
const advice = { sentence: 'Check your quiet line.', note: 'Advice only.', numbers: [{ number, display: number,
  sentence: 'Find out more before giving it up.', verdict_label: 'Investigate', reasons: ['Only 30 days of records.'],
  evidence: { last_arrival_text: 'No fax has arrived yet.', removed_sentence: 'No saving is known.' },
  dependencies: [{ question: 'broadband', label: 'Does this line also carry broadband?', answer: 'unknown', note: null }] }] };
const plan = { number, state: 'none', sentence: 'No move is planned.', note: 'Faxbot never places the port order.',
  steps: [], accounts: [{key: 'new', label: 'New account'}], origins: [{key: 'old', label: 'Old route'}] };
const setup = () => {
  const call = vi.fn(async (request: {method: string; path: string; body?: unknown}) => {
    if (request.path === '/routing/recommendations/lines') return advice;
    return plan;
  });
  return { client: {call} as unknown as AdminAPIClient, call };
};

describe('number advice and checked moves', () => {
  it('shows evidence and records a dependency answer', async () => {
    const {client, call} = setup(); render(<NumberMoves client={client} canWrite />);
    expect(await screen.findByText('Only 30 days of records.')).toBeTruthy();
    fireEvent.change(screen.getByLabelText('Does this line also carry broadband?'), {target: {value: 'no'}});
    fireEvent.click(screen.getByRole('button', {name: 'Save answer'}));
    await waitFor(() => expect(call).toHaveBeenCalledWith({method: 'POST',
      path: '/routing/numbers/%2B15550001001/dependencies', body: {question: 'broadband', answer: 'no', note: ''}}));
  });
  it('opens a move plan and records the chosen target account', async () => {
    const {client, call} = setup(); render(<NumberMoves client={client} canWrite />);
    fireEvent.click(await screen.findByRole('button', {name: 'Move plan'}));
    expect(await screen.findByText('No move is planned.')).toBeTruthy();
    fireEvent.change(screen.getByLabelText('Move to account'), {target: {value: 'new'}});
    fireEvent.click(screen.getByRole('button', {name: 'Start move plan'}));
    await waitFor(() => expect(call).toHaveBeenCalledWith({method: 'POST',
      path: '/routing/numbers/%2B15550001001/move', body: {to_account: 'new'}}));
  });
  it('gives readers the evidence without write controls', async () => {
    const {client} = setup(); render(<NumberMoves client={client} canWrite={false} />);
    expect(await screen.findByText('Check your quiet line.')).toBeTruthy();
    expect(screen.queryByRole('button', {name: 'Save answer'})).toBeNull();
  });
});

it('records move steps and receipt tests with the correct write operations', async () => {
  const open = {...plan, state: 'open', sentence: 'Move in progress.', steps: [
    {step: 'cutover', label: 'Carrier cutover', state: 'waiting', state_label: 'To do', evidence: [], action_label: 'Record cutover'},
  ]};
  const writes: Array<{method: string; path: string; body?: unknown}> = [];
  const call = vi.fn(async (request: {method: string; path: string; body?: unknown}) => {
    if (request.path === '/routing/recommendations/lines') return advice;
    if (request.method === 'POST') writes.push(request);
    return open;
  });
  render(<NumberMoves client={{call} as unknown as AdminAPIClient} canWrite />);
  fireEvent.click(await screen.findByRole('button', {name: 'Move plan'}));
  fireEvent.click(await screen.findByRole('button', {name: 'Record cutover'}));
  await waitFor(() => expect(writes).toContainEqual({method: 'POST', path: '/routing/numbers/%2B15550001001/move/steps/cutover', body: {state: 'done', note: ''}}));
  await waitFor(() => expect((screen.getByRole('button', {name: 'Forget old carrier learning'}) as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(screen.getByRole('button', {name: 'Forget old carrier learning'}));
  await waitFor(() => expect(writes.some((r) => r.path.endsWith('/move/forget'))).toBe(true));
  fireEvent.change(screen.getByLabelText('Receipt test route'), {target: {value: 'old'}});
  await waitFor(() => expect((screen.getByRole('button', {name: 'Watch for receipt test'}) as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(screen.getByRole('button', {name: 'Watch for receipt test'}));
  await waitFor(() => expect(writes).toContainEqual({method: 'POST', path: '/routing/numbers/%2B15550001001/move/tests', body: {origin: 'old'}}));
  expect((screen.getByRole('button', {name: 'Finish move plan'}) as HTMLButtonElement).disabled).toBe(true);
  await waitFor(() => expect((screen.getByRole('button', {name: 'Abandon move plan'}) as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(screen.getByRole('button', {name: 'Abandon move plan'}));
  await waitFor(() => expect(writes).toContainEqual({method: 'POST', path: '/routing/numbers/%2B15550001001/move/steps/move', body: {state: 'abandoned', note: ''}}));
});
