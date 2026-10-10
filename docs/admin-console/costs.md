# Savings & optimization

## Spending

**Savings & optimization → Spending** (`#/savings/spending`) shows what sending and receiving cost, by provider and carrier: what was charged, estimates waiting for the carrier, flat plans counted once per 30 days, and calls the carrier billed that Faxbot has no record of. **Check … charges now** asks the carrier at once. See [delivery routes](../operations/delivery-routes.md).

## Prices & plans

**Savings & optimization → Prices & plans** (`#/savings/prices`) holds the prices Faxbot uses to estimate costs and rank routes. Add or edit a rate card with **Add rate card** or **Edit**; enter the source and date as well as the rates. Prices and plan coverage depend on the destination, so Faxbot does not use a domestic price when no price applies to an international destination.

The page also shows plan budgets. Set included pages or minutes and any extra-page price for a plan there. An allowance applies only when the destination's tariff is covered by that plan; it does not fill in a missing destination price or cover a separate destination tariff.

## Advice

**Savings & optimization → Facts to establish** (`#/savings/facts`) includes **Compare setup plans**. It compares setup items using amounts you supply; it does not save the inputs, enroll a partner or change a route.

Enter **Whose costs and benefits?**, **Planning period**, a three-letter **Currency** and the **Budget** for additional setup spending. Use the same currency and period for every amount.

Add each **Setup item** with its cost and mark **Already installed** where applicable. Add a **Shared setup cost** when several items need the same setup; Faxbot counts it once. Under **Benefits between items**, enter the expected and cautious change in running costs when both items are present. A negative benefit means higher running costs.

Leave an amount blank when it is unknown; enter `0` when there is none. Faxbot lists missing amounts needed for a comparison. **Expected plan** has the highest expected net benefit. **Cautious plan** has the best lower net benefit across your two scenarios. Both are estimates from your inputs, not measured savings.

To compare from the command line, create a UTF-8 JSON scenario using the fields in the [command reference](../reference/cli.md#faxbot-savings-portfolio), then run:

```bash
faxbot savings portfolio --file scenario.json
```

## Savings results

**Savings & optimization → Savings results** (`#/savings/results`) shows results for the last 30 days. Money figures are estimates: they compare what Faxbot paid with what the same faxes might have cost the usual way. They remain estimates even after a carrier reports its charges.

The page reports money, pages, call time, calls and data in separate units. Some entries are counts rather than estimates; the page marks which figures are estimates. Different results can describe the same delivery, so do not add their counts together or multiply an avoided-call count by a price yourself. That can count an avoided call twice.

Data saved by reuse and patches is a byte count, not a money saving. It may matter for metered data or storage if you have a cost basis for those bytes; Faxbot does not convert it to money. A fax that would have gone through a flat plan saves no money. Case-packet savings count only from when Faxbot started recording what each packet left out; the page gives the date.

## Capabilities

**Savings & optimization → Capabilities** (`#/savings/capabilities`) lists what Faxbot can do, grouped by outcome. Filter by **On**, **Off**, **Ready to turn on**, **Needs something** or **Experimental**. **Ready to turn on** means the capability is off but works on this installation; it does not mean it has been tested on every route or recipient.

Open a capability to see an example, its prerequisites, where to change its setting and what it did here. The evidence label describes how far the capability has been demonstrated: on a live call, in a test lab or with sample data. This evidence is separate from whether Faxbot has recorded its use on your installation. The page reads local settings and stored records; it does not make a network check. It does not show money.

From the command line, use `faxbot savings capabilities --filter ready` to list ready capabilities, or `faxbot savings capabilities show KEY` to see one capability and its setting command. The key is shown in brackets beside each capability name.

## Recommendations

**Savings & optimization → Opportunities** (`#/savings/opportunities`) compares delivered fax costs by number, suggests a cheaper route or a plan that already includes those faxes, and shows whether you could reduce receiving costs by sharing Telnyx channels or removing quiet numbers. Faxbot bases the advice on recorded calls and published prices; it recommends changes but does not change your routes or carrier account. Advice may be unavailable when there is not enough history or price information.
