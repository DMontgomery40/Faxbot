# Costs

## Spending

**Costs → Spending** (`#/costs/spending`) shows what sending and receiving cost, by provider and carrier: what was charged, estimates waiting for the carrier, flat plans counted once per 30 days, and calls the carrier billed that Faxbot has no record of. **Check … charges now** asks the carrier at once. See [delivery routes](../operations/delivery-routes.md).

## Prices & plans

**Costs → Prices & plans** (`#/costs/prices`) holds the published price of each provider in use, editable, with its source and date. Faxbot uses them to estimate costs and choose the cheapest route.

## Savings

**Costs → Savings** (`#/costs/savings`) shows what the last 30 days saved, each part marked **Estimate**:

- **Sending together**: calls saved when faxes to the same number shared a call;
- **Direct delivery**: fax calls avoided when a partner accepted the document directly;
- **Case packets**: pages not sent again because a packet listed documents the recipient already had.

Every figure compares what you paid with what the same faxes would have cost the usual way, so it stays an estimate after the carrier reports. A fax that would have gone through a flat plan saves no money. Case packets count from when Faxbot started recording what each packet left out, and the page says from which day.

## Recommendations

**Costs → Recommendations** (`#/costs/recommendations`) will show cheaper routes for the numbers you fax and receiving lines you could share.
