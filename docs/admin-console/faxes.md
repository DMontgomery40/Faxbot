# Faxes

## Received

**Faxes → Received** (`#/faxes/received`) lists received faxes and the work on them in one list. Filters: **All**, **Mine**, **Waiting for an owner**, **Overdue** and **Not delivered** (email delivery failed). Each fax shows when it arrived, who sent it, which number received it, **Received through** the provider or carrier, its cost when the carrier reports it, and its email delivery.

**Import a document** (for people who may import) adds a PDF from another system with where it came from, its number or ID in that system and, optionally, a version, the To and From numbers, when it was received and its pages. It appears in Received like a received fax, in the mailbox for its To number; importing the same number or ID again never adds a second copy (`POST /imports`).

People who handle faxes can **Assign** a fax, **Acknowledge** it and download its evidence; the download is recorded in the fax's history. Received reads at most 200 work items at a time.

The receiving line says how faxes reach Faxbot. Over your own carrier line it says "Received faxes reach Faxbot: ready." or what stops them, with a button to the carrier's page; Phaxio and Sinch show the address to enter in their consoles. See [receiving](../setup/sip-trunk.md) for your own line, and [email delivery](../operations/intake.md).

## Sent

**Faxes → Sent** (`#/faxes/sent`) lists sent faxes with their number, state, **Route** (the provider or carrier that carried it) and **Cost**: what the carrier charged, "In your plan" for a flat plan (as soon as the route is chosen), "… estimate" until the carrier reports, or "Not reported yet".

**Fax details** shows **Delivery attempts** (each try and what the provider reported), **What happened**, the fax's share of a shared call's charge and, when available, why its route was chosen. Faxes held by sending rules appear in **Faxes waiting for you**; approve, refuse or send one by an eligible account. For a fax whose delivery is uncertain, check the available evidence and settle what happened. Faxbot does not send it again unless you choose to do so. **Refresh status** asks the provider again; **Send now** sends a fax that is waiting to go with others. See [sending together](../operations/delivery-routes.md#sending-together).

## Send a fax

**Faxes → Send a fax** (`#/faxes/send`) takes a number and a PDF or TXT document within the upload limit. Before sending it shows the route Faxbot will use and what this fax should cost there, in the route's own unit: "About $0.01 for this 2-page fax ($0.005 a minute, at least 1 minute)." A fax through a flat plan shows no estimate. For a number that sends faxes together, **Send now** sends at once and takes the waiting faxes with it. After sending, **See it in Sent** opens the fax.

The route and estimate need permission to read settings; others see the form without them.

## Forms

**Faxes → Forms** lets you import a fillable PDF or a PDF or SVG template with field positions, inspect the fields and preview the pages. Fill in a registered form to send it. Faxbot sends the filled-in values to a partner that supports forms; otherwise it sends the rendered pages as a fax. **Sent forms** shows the outcome. Check it before retrying if Faxbot could not confirm delivery.

## Expected

**Faxes → Expected** records a fax before it arrives. Add its reference, what you expect and the mailbox that owns it, then add any known sender, number, due time or required details. Faxbot matches received faxes against these records and can propose less certain matches for you to confirm. Use **What is missing** to review overdue or unmatched work and received faxes that answered nothing. If you import open work from another system or declare an outage, the **Import** and **Outages** tabs are available to accounts with the required permissions.
