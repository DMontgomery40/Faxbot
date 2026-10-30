import { beforeEach, expect, it } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import CapabilitySetup from '../components/capabilities/CapabilitySetup';
import type { Capabilities as CapabilitiesView } from '../api/capabilityTypes';
import fixture from './capabilities.json';

const item = (fixture.response as unknown as CapabilitiesView).outcomes.flatMap((outcome) => outcome.capabilities)
  .find((entry) => entry.key === 'dense_pages')!;

beforeEach(() => sessionStorage.clear());

it('recovers review progress after leaving, keeps direct settings, and finishes on status', async () => {
  const first = render(<CapabilitySetup item={item} />);
  fireEvent.click(screen.getByRole('button', { name: 'Guided setup' }));
  expect(screen.getByText(item.setup!.steps[0].sentence)).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: 'Next' }));
  expect(screen.getByText(item.setup!.steps[1].sentence)).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: 'Leave setup' }));
  first.unmount();
  render(<CapabilitySetup item={item} />);
  fireEvent.click(screen.getByRole('button', { name: 'Resume guided setup' }));
  expect(screen.getByText(item.setup!.steps[1].sentence)).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: 'Next' }));
  expect(screen.getAllByRole('link', { name: 'Open direct settings' })[0].getAttribute('href')).toBe('#/delivery/connections');
  for (let index = 2; index < 6; index++) fireEvent.click(screen.getByRole('button', { name: 'Next' }));
  expect(screen.getByText(item.setup!.steps[6].sentence)).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: 'Return to capability status' }));
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  expect(screen.queryByText(/setup complete/i)).toBeNull();
});

it('shows saved route state separately from review progress and tolerates corrupt storage', () => {
  sessionStorage.setItem('faxbot.setup.dense_pages.v1', 'broken');
  render(<CapabilitySetup item={item} />);
  fireEvent.click(screen.getByRole('button', { name: 'Guided setup' }));
  expect(screen.getByText(new RegExp(`${item.setup!.routes[0].label}: `))).toBeTruthy();
  expect(screen.getByText(/review progress.*does not verify/i)).toBeTruthy();
  expect(screen.getByText(item.setup!.cancel)).toBeTruthy();
});

it('reads the same seven setup sentences as the server contract and stores no settings', async () => {
  const view = render(<CapabilitySetup item={item} />);
  fireEvent.click(screen.getByRole('button', { name: 'Guided setup' }));
  for (let index = 0; index < item.setup!.steps.length; index++) {
    expect(screen.getByText(item.setup!.steps[index].sentence)).toBeTruthy();
    if (index < 6) fireEvent.click(screen.getByRole('button', { name: 'Next' }));
  }
  expect(sessionStorage.getItem('faxbot.setup.dense_pages.v1')).toBe('6');
  expect(sessionStorage.length).toBe(1);
  view.unmount();
});
