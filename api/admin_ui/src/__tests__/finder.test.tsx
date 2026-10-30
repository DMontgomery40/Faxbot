import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import type { FinderAnswer } from '../api/types';
import Finder from '../components/shell/Finder';
import Help from '../components/shell/Help';
import LoginScreen from '../components/LoginScreen';
import { resolvesTo } from '../navigation';
import { server } from '../test/server';
import fixture from './finder.json';

// Answers of GET /auth/finder for someone who may see every page, and the lines
// `faxbot find` prints from them (api/tests/test_finder.py keeps both current).
// The console must show the same places, addresses, scopes and commands.
const queries = fixture.queries as Array<{ q: string; response: FinderAnswer; cli: string[] }>;
const client = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });

function serve(answer: FinderAnswer) {
  server.use(http.get('/auth/finder', () => HttpResponse.json(answer as never)));
}

describe('The finder fixture', () => {
  it('names only pages that resolve, with a scope, and no setting value', () => {
    for (const { response } of queries) {
      for (const result of response.results) {
        expect(result.scope).toBeTruthy();
        if (result.address) expect(resolvesTo(result.address.replace('#/', '')), result.address).not.toBeNull();
        expect(Object.keys(result).sort()).toEqual(
          ['address', 'command', 'kind', 'label', 'name', 'place', 'scope']);
      }
    }
  });

  it('reaches the canonical editor for a technical synonym and a task phrase', () => {
    const byQuery = new Map(queries.map((entry) => [entry.q, entry.response.results]));
    expect(byQuery.get('T.38')?.[0]?.address).toBe('#/delivery/trunk');
    expect(byQuery.get('retention')?.some((result) => result.name === 'inbound_retention_days'
      && result.address === '#/admin/retention')).toBe(true);
    expect(byQuery.get('who can send')?.length).toBeGreaterThan(0);
  });
});

describe('Find a page or setting', () => {
  it('shows the same lines the command prints and opens the chosen page from the keyboard', async () => {
    const { response, cli } = queries[0];
    serve(response);
    const onOpen = vi.fn();
    render(<Finder client={client()} onOpen={onOpen} />);
    fireEvent.keyDown(window, { key: 'k', ctrlKey: true });
    const input = await screen.findByTestId('finder-input');
    fireEvent.change(input, { target: { value: 'T.38' } });
    for (const result of response.results) {
      const line = result.name ? `${result.label} (${result.name})` : result.label;
      await waitFor(() => expect(screen.getByText(line)).toBeTruthy());
      expect(cli.some((text) => text.includes(line))).toBe(true);
      if (result.command) expect(cli.some((text) => text.includes(result.command as string))).toBe(true);
    }
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(onOpen).toHaveBeenCalledWith('#/delivery/trunk');
  });

  it('says so plainly when nothing matches, and when the search fails', async () => {
    serve({ q: 'zzzz', results: [] });
    render(<Finder client={client()} onOpen={vi.fn()} />);
    fireEvent.click(screen.getByTestId('finder-open'));
    fireEvent.change(await screen.findByTestId('finder-input'), { target: { value: 'zzzz' } });
    await screen.findByText(/Nothing matches/);
    server.use(http.get('/auth/finder', () => HttpResponse.json({ detail: 'no' } as never, { status: 503 })));
    fireEvent.change(screen.getByTestId('finder-input'), { target: { value: 'again' } });
    await screen.findByText('Could not search. Try again.');
  });
});

describe('Help on every page', () => {
  it('opens guidance and the sign-in recovery steps', () => {
    render(<Help place="Delivery setup › Connections › Carrier trunk" />);
    fireEvent.click(screen.getByTestId('help-open'));
    expect(screen.getByText(/You are on: Delivery setup/)).toBeTruthy();
    expect(screen.getByText(/faxbot admin recover-owner/)).toBeTruthy();
    expect(screen.getByText(/faxbot find WORDS/)).toBeTruthy();
  });

  it('is on the sign-in page before anyone has signed in', () => {
    render(<LoginScreen onPasswordSignIn={vi.fn()} onKeySignIn={vi.fn()} />);
    fireEvent.click(screen.getByTestId('help-open'));
    expect(screen.getByText(/If you cannot sign in/)).toBeTruthy();
  });
});
