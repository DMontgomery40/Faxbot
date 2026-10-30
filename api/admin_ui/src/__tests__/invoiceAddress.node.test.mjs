// Dependency-free checks: node --experimental-strip-types --test this-file.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { invoiceAddress, invoiceAt } from '../components/delivery/invoiceAddress.ts';

test('invoice records round trip reserved characters without losing filters', () => {
  const id = 'inv/1?x=2 & #é';
  const address = invoiceAddress(id, '#/savings/invoices?account=uk&invoice=old');
  assert.equal(invoiceAt(address), id);
  assert.equal(new URLSearchParams(address.split('?')[1]).get('account'), 'uk');
  assert.equal(address.split('invoice=').length, 2);
});
test('closing returns to the same invoice view and removes only the record', () => {
  assert.equal(invoiceAddress(null, '#/savings/invoices?account=uk&invoice=old'), '#/savings/invoices?account=uk');
  assert.equal(invoiceAddress(null), '#/savings/invoices');
});
test('old invoice addresses work and unrelated screens cannot open invoices', () => {
  assert.equal(invoiceAt('#/costs/invoices?invoice=inv-1'), 'inv-1');
  assert.equal(invoiceAt('#/faxes/sent?invoice=inv-1'), null);
  assert.equal(invoiceAt('#/savings/invoices?invoice='), null);
  assert.equal(invoiceAddress('inv-1', '#/faxes/sent?fax=hidden'), '#/savings/invoices?invoice=inv-1');
});
