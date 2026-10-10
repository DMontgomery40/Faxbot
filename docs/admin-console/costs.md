# Costs

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
faxbot costs portfolio --file scenario.json
```

## Savings

**Savings & optimization → Savings results** (`#/savings/results`) shows what the last 30 days saved, each part marked **Estimate**:

- **Sending together**: calls saved when faxes to the same number shared a call;
- **Direct delivery**: fax calls avoided when a partner accepted the document directly;
- **Case packets**: pages not sent again because a packet listed documents the recipient already had.
- **Pages saved by encoding (experimental)**: pages avoided when an attempt used encoded pages.

Money estimates compare what you paid with what the same faxes would have cost the usual way, so they remain estimates after the carrier reports. Exact byte counts for reuse and patches appear separately and are not added to money saved. A fax that would have gone through a flat plan saves no money. Case packets count from when Faxbot started recording what each packet left out, and the page says from which day.

## Recommendations

**Savings & optimization → Opportunities** (`#/savings/opportunities`) compares delivered fax costs by number, suggests a cheaper route or a plan that already includes those faxes, and shows whether you could reduce receiving costs by sharing Telnyx channels or removing quiet numbers. Faxbot bases the advice on recorded calls and published prices; it recommends changes but does not change your routes or carrier account. Advice may be unavailable when there is not enough history or price information.
