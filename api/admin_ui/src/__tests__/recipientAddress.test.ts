import { describe, expect, it } from 'vitest';
import { recipientAddress, recipientFromAddress } from '../components/delivery/recipientAddress';

describe('recipient addresses', () => {
  it('round trips international numbers and lookup input', () => {
    for (const number of ['+12025550123', '+44 121 234 5678', '01782 684953']) {
      expect(recipientFromAddress(recipientAddress(number))).toBe(number);
    }
  });
  it('does not open a recipient on another page or an empty lookup', () => {
    expect(recipientFromAddress('#/faxes/cases?recipient=%2B12025550123')).toBeNull();
    expect(recipientFromAddress('#/recipients/list')).toBeNull();
    expect(recipientFromAddress('#/recipients/list?recipient=')).toBeNull();
  });
});
