# Faxes

## Received

**Faxes → Received** (`#/faxes/received`) lists received faxes and the work on them in one list. Filters: **All**, **Mine**, **Waiting for an owner**, **Overdue** and **Not delivered** (email delivery failed). Each fax shows when it arrived, who sent it, which number received it, **Received through** the provider or carrier, its cost when the carrier reports it, and its email delivery.

People who handle faxes can **Assign** a fax, **Acknowledge** it and download its evidence; the download is recorded in the fax's history. Received reads at most 200 work items at a time.

The receiving line says how faxes reach Faxbot. Over your own carrier line it says "Received faxes reach Faxbot: ready." or what stops them, with a button to the carrier's page; Phaxio and Sinch show the address to enter in their consoles. See [receiving](../setup/sip-trunk.md) for your own line, and [email delivery](../operations/intake.md).

## Sent

**Faxes → Sent** (`#/faxes/sent`) lists sent faxes with their number, state, **Route** (the provider or carrier that carried it) and **Cost**: what the carrier charged, "In your plan" for a flat plan (as soon as the route is chosen), "… estimate" until the carrier reports, or "Not reported yet".

**Fax details** shows **Delivery attempts** (each try and what the provider reported), **What happened**, the fax's share of a shared call's charge, and **Confirm receipt** for a fax whose result is uncertain: it settles the fax with the provider's fax ID without sending it again. **Refresh status** asks the provider again; **Send now** sends a fax that is waiting to go with others. See [sending together](../operations/delivery-routes.md#sending-together).

## Send a fax

**Faxes → Send a fax** (`#/faxes/send`) takes a number and a PDF or TXT document within the upload limit. Before sending it shows the route Faxbot will use and what this fax should cost there, in the route's own unit: "About $0.01 for this 2-page fax ($0.005 a minute, at least 1 minute)." A fax through a flat plan shows no estimate. For a number that sends faxes together, **Send now** sends at once and takes the waiting faxes with it. After sending, **See it in Sent** opens the fax.

The route and estimate need permission to read settings; others see the form without them.
