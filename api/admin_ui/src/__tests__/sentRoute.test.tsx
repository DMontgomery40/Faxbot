import { describe, expect, it } from 'vitest';
import { routeText } from '../components/JobsList';

describe('Sent route', () => {
  it('names the route that carried the fax, and any route tried before it', () => {
    expect(routeText('humblefax', { state: 'none', summary: null, reported_cost: [], routes: ['humblefax'] })).toBe('HumbleFax');
    expect(routeText('humblefax', { state: 'none', summary: null, reported_cost: [], routes: ['phaxio', 'signalwire'] }))
      .toBe('SignalWire (after Phaxio)');
    expect(routeText('phaxio', { state: 'none', summary: null, reported_cost: [], routes: ['direct'] })).toBe('Direct delivery');
  });

  it('falls back to the accepted provider while no attempt has a route yet', () => {
    expect(routeText('humblefax', undefined)).toBe('HumbleFax');
    expect(routeText('humblefax', { state: 'none', summary: null, reported_cost: [], routes: [] })).toBe('HumbleFax');
  });
});
