# Savings & optimization

## Spending

**Savings & optimization → Spending** (`#/savings/spending`) shows what sending and receiving cost, by provider and carrier: what was charged, estimates waiting for the carrier, flat plans counted once per 30 days, and calls the carrier billed that Faxbot has no record of. **Check … charges now** asks the carrier at once. See [delivery routes](../operations/delivery-routes.md).

## Prices & plans

**Savings & optimization → Prices & plans** (`#/savings/prices`) holds the prices Faxbot uses to estimate costs and rank routes. Add or edit a rate card with **Add rate card** or **Edit**; enter the source and date as well as the rates. Prices and plan coverage depend on the destination, so Faxbot does not use a domestic price when no price applies to an international destination.

The page also shows plan budgets. Set included pages or minutes and any extra-page price for a plan there. An allowance applies only when the destination's tariff is covered by that plan; it does not fill in a missing destination price or cover a separate destination tariff.

Under **Prices by caller ID**, select **Import a rate deck** to add a carrier's CSV rate deck. Choose the sending card, select the deck layout or let Faxbot recognise it, and record its source and publication or read date. You can also use `faxbot savings rate-rows ROUTE --caller-id-deck FILE --deck-format twilio --source URL --published DATE`. Faxbot uses a lower caller-ID price only after you confirm that your organization holds that number and may send from it on that account. Enter how you verified this; include a source link if available. Mark **This number was bought on this account** only when true. The account's caller ID does not change.

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

The **Fax server renewal** section can compare a renewal with call records and numbers you run through Faxbot in parallel. Select **Import call records** to upload Asterisk Master.csv, Cisco Unified CM, RightFax DocTransport audit log (level 3 or 4), GFI FaxMaker activity export, or a CSV with start, end or duration, direction, channel and number. Enter the system name, licensed channel count if known, and the time zone if the file does not use the server's zone. You can leave an import out of the report without deleting it from history. Select **Enter a renewal** to record the product, renewal date and amount, currency, licensed channels, source and any parallel numbers and start date. Select **Import number routing** to upload a CSV with `number`, `user`, `email` and `cover sheet` columns; only `number` is required. The report shows the numbers still routed through the old server. These entries support planning only: Faxbot does not change a licence, cancel a renewal or contact a vendor.

The **Fax lines in a POTS-replacement order** section compares a quote you enter with your line inventory and the shared-trunk option shown there. To populate the inventory, use **Delivery setup → Number moves**. Enter a quote in the Opportunities section, or choose a published price shown there. These are comparisons based on your inventory and the quote details, not a promised saving or an order to a provider.
