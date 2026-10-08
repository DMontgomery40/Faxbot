import { describe, expect, it } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import AdminAPIClient from '../api/client';
import EmailDelivery from '../components/delivery/EmailDelivery';
import { server } from '../test/server';

const client = () => new AdminAPIClient({ kind: 'key', key: 'synthetic-key' });

function failing(status: number) {
  server.use(http.get('/intake/connectors', () => HttpResponse.json({ detail: 'Synthetic failure' }, { status })));
}

// The email delivery list's endpoint exists: only someone without permission sees the section left out, and any
// other failure is said in one sentence.
describe('Email delivery says when it cannot load', () => {
  it('shows the list when it loads', async () => {
    render(<EmailDelivery client={client()} canWrite />);
    expect(await screen.findByText('No email delivery yet')).toBeTruthy();
  });

  it('says a missing list instead of hiding', async () => {
    failing(404);
    render(<EmailDelivery client={client()} canWrite />);
    expect(await screen.findByText('This item no longer exists. Reload and try again.')).toBeTruthy();
    expect(screen.getByText('Email delivery')).toBeTruthy();
  });

  it('says a server failure instead of hiding', async () => {
    failing(500);
    render(<EmailDelivery client={client()} canWrite />);
    expect(await screen.findByText('The request failed. Try again.')).toBeTruthy();
  });

  it('leaves the section out for someone who may not read it', async () => {
    let asked = false;
    server.use(http.get('/intake/connectors', () => {
      asked = true;
      return HttpResponse.json({ detail: 'Forbidden' }, { status: 403 });
    }));
    const { container } = render(<EmailDelivery client={client()} canWrite />);
    await waitFor(() => expect(asked).toBe(true));
    await waitFor(() => expect(container.textContent).toBe(''));
    expect(screen.queryByRole('alert')).toBeNull();
  });
});
