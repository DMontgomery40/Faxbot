import { describe, expect, it } from 'vitest';
import { SINCH_PASSWORD_NEEDED, sinchBasicProblem } from '../components/Settings';

describe("Sinch's user name and password for received faxes", () => {
  it('refuses to save the user name without a password, with one sentence', () => {
    expect(sinchBasicProblem({ sinch_inbound_basic_user: 'synthetic-sinch-webhook', sinch_inbound_basic_pass: '' },
      ['sinch_inbound_basic_user'])).toBe(SINCH_PASSWORD_NEEDED);
    expect(SINCH_PASSWORD_NEEDED).toBe('Enter the password Sinch sends as well; Faxbot does not accept the user name without it.');
  });

  it('saves both together, a stored password, clearing both, or changes elsewhere', () => {
    expect(sinchBasicProblem({ sinch_inbound_basic_user: 'synthetic-sinch-webhook',
      sinch_inbound_basic_pass: 'synthetic-webhook-pass' }, ['sinch_inbound_basic_user', 'sinch_inbound_basic_pass'])).toBeNull();
    // A password already saved shows masked and still counts.
    expect(sinchBasicProblem({ sinch_inbound_basic_user: 'synthetic-sinch-webhook', sinch_inbound_basic_pass: '****pass' },
      ['sinch_inbound_basic_user'])).toBeNull();
    expect(sinchBasicProblem({ sinch_inbound_basic_user: '', sinch_inbound_basic_pass: '' },
      ['sinch_inbound_basic_user', 'sinch_inbound_basic_pass'])).toBeNull();
    expect(sinchBasicProblem({ sinch_inbound_basic_user: 'synthetic-sinch-webhook', sinch_inbound_basic_pass: '' },
      ['max_file_size_mb'])).toBeNull();
  });
});
