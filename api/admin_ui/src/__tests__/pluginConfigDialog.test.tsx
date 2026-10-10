// System → Developer → Provider plugins: the trunk plugin points at the screens that hold its settings today.
import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import PluginConfigDialog from '../components/PluginConfigDialog';

describe('Provider plugin settings', () => {
  it('sends the trunk to its Delivery setup page and the station ID to Sending identity, in the carrier\'s words', () => {
    const config = { enabled: true, settings: {}, role: 'outbound', _meta: { desired_revision_id: 'synthetic' } };
    render(<PluginConfigDialog open plugin={{ id: 'sip', name: 'Carrier trunk' }} initialConfig={config as never}
      loading={false} loadError="" onClose={() => undefined} onReload={() => undefined} onSave={async () => undefined} />);
    const text = document.body.textContent ?? '';
    expect(screen.getByText(/Your carrier trunk has its own page under Delivery setup/)).toBeTruthy();
    expect(text).toContain('Delivery setup → Sending identity');
    expect(text).not.toMatch(/SIP trunk \(Asterisk\)|Setup Wizard/);
  });
});
